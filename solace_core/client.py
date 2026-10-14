"""Human and agent clients share the same authenticated Unix protocol."""
from contextlib import contextmanager
from pathlib import Path
import socket
import uuid
from .errors import CoreError, classify
from .paths import CorePaths
from .protocol import MAX_FRAME, encode_frame, decode_frame, response
from .server import peer_uid
import os


class CoreClient:
    def __init__(self, socket_path=None, actor='cli', timeout=30):
        self.socket_path = Path(socket_path) if socket_path else CorePaths.from_environment().socket
        self.actor, self.timeout = actor, timeout

    @contextmanager
    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream = None
        try:
            connection.settimeout(self.timeout)
            connection.connect(str(self.socket_path))
            if peer_uid(connection) != os.getuid():
                raise CoreError('PERMISSION_DENIED', 'The socket belongs to another user.', 'Connect to your own core service.')
            stream = connection.makefile('rb')
            yield connection, stream
        except OSError as error:
            raise CoreError('CORE_UNAVAILABLE', 'The local core socket is unavailable.', 'Start the installed solace-core.socket user unit or inspect core diagnostics.', True) from error
        finally:
            if stream is not None: stream.close()
            connection.close()

    def _request(self, method, params, request_id):
        return dict(version=1,id=request_id or str(uuid.uuid4()),method=method,params=params,actor=self.actor)

    @staticmethod
    def _receive(stream, request_id):
        data = stream.readline(MAX_FRAME + 1)
        if not data:
            raise CoreError('CORE_UNAVAILABLE', 'The core disconnected before a response.', 'Retry with the same mutation request ID.', True)
        value = decode_frame(data)
        if value.get('version') != 1 or value.get('id') != request_id:
            raise CoreError('PROTOCOL_VERSION_UNSUPPORTED', 'The core returned an incompatible response.', 'Use matching client and core versions.')
        return value

    def request(self, method, params, *, request_id=None):
        request = self._request(method, params, request_id)
        try:
            with self.connect() as (connection, stream):
                connection.sendall(encode_frame(request))
                return self._receive(stream, request['id'])
        except Exception as error:
            return response(request['id'], error=classify(error))

    def subscribe(self, project_id, after=0):
        request = self._request('event.subscribe', dict(project=project_id,after=after), None)
        with self.connect() as (connection, stream):
            connection.sendall(encode_frame(request))
            acknowledgement = self._receive(stream, request['id'])
            if not acknowledgement.get('ok'):
                raise CoreError(**acknowledgement['error'])
            while True:
                value = self._receive(stream, request['id'])
                if 'event' in value:
                    yield value['event']
                elif value.get('ok') is False:
                    raise CoreError(**value['error'])
