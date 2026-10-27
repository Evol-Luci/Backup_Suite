"""Permission-restricted Unix request server and durable event streaming."""
from pathlib import Path
import os
import socket
import stat
import struct
import threading
from .errors import CoreError, classify
from .protocol import MAX_FRAME, decode_frame, encode_frame, response, validate_request


def peer_uid(connection):
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))[1]


def bind_private_socket(path, runtime):
    path, runtime = Path(path).absolute(), Path(runtime).resolve()
    meta = runtime.stat()
    if meta.st_uid != os.getuid() or meta.st_mode & 0o077 or not path.is_relative_to(runtime):
        raise CoreError('PERMISSION_DENIED', 'The development socket must be in a private runtime directory.', 'Select a path beneath XDG_RUNTIME_DIR.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CoreError('PERMISSION_DENIED', 'A socket parent is not private.', 'Check directory ownership and permissions.')
        if parent == runtime: break
    if path.exists() or path.is_symlink():
        raise CoreError('CORE_UNAVAILABLE', 'The socket path already exists.', 'Connect to the existing core or inspect its service state.')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path));path.chmod(0o600);listener.listen(64)
        return listener
    except BaseException:
        listener.close()
        raise


class SocketServer:
    def __init__(self, listener, service, *, credential_reader=peer_uid, clients=64):
        self.listener, self.service = listener, service
        self.credential_reader = credential_reader
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(clients)
        self.lock = threading.Lock()
        self.connections = set()
        self.threads = set()

    def serve_forever(self):
        self.listener.settimeout(.2)
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set(): break
                raise
            if not self.slots.acquire(blocking=False):
                connection.close();continue
            with self.lock:
                self.connections.add(connection)
                thread = threading.Thread(target=self._client, args=(connection,), daemon=True)
                self.threads.add(thread)
            thread.start()

    def close(self):
        self.stop.set()
        with self.lock:
            connections, threads = list(self.connections), list(self.threads)
        for connection in connections:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        for thread in threads:
            if thread is not threading.current_thread(): thread.join(3)

    def _send(self, connection, value):
        connection.sendall(encode_frame(value))

    def _client(self, connection):
        stream = None
        try:
            connection.settimeout(30)
            if self.credential_reader(connection) != os.getuid():
                self._send(connection, response(None, error=CoreError('PERMISSION_DENIED', 'This core is private to its user.', 'Connect through your own user session.')))
                return
            stream = connection.makefile('rb')
            while not self.stop.is_set():
                data = stream.readline(MAX_FRAME + 1)
                if not data: return
                try:
                    request = decode_frame(data)
                except CoreError as error:
                    self._send(connection, response(None, error=error));return
                result = self.service.handle(request)
                try:self._send(connection, result)
                except CoreError as error:
                    if error.code!='INVALID_REQUEST':raise
                    self._send(connection,response(request.get('id'),error=CoreError(
                        'RESPONSE_TOO_LARGE','The response exceeds the protocol frame limit.',
                        'Use a narrower query or the documented pagination options.')))
                    return
                if result['ok'] and request['method'] == 'event.subscribe':
                    self._subscribe(connection, request)
                    return
        except (OSError, ValueError):
            pass  # Disconnects are ordinary; do not log request content.
        except CoreError as error:
            try: self._send(connection, response(None, error=error))
            except OSError: pass
        finally:
            if stream is not None: stream.close()
            connection.close()
            with self.lock:
                self.connections.discard(connection)
                self.threads.discard(threading.current_thread())
            self.slots.release()

    def _subscribe(self, connection, request):
        project, cursor = request['params']['project'], request['params'].get('after', 0)
        while not self.stop.is_set():
            try:
                catalog = self.service.catalog(project)
                with catalog.condition:
                    events = catalog.events_after(cursor)
                    if not events:
                        catalog.condition.wait(1)
                if not events:
                    self._send(connection, dict(version=1,id=request['id'],heartbeat=cursor))
                for event in events:
                    self._send(connection, dict(version=1,id=request['id'],event=event))
                    cursor = event['seq']
            except Exception as error:
                self._send(connection, response(request['id'], error=classify(error)))
                return
