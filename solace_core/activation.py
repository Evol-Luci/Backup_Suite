"""Validate systemd's single inherited Unix listening descriptor."""
import os
import socket
from .errors import CoreError


def activated_socket(env=None, *, fd=3):
    env=os.environ if env is None else env
    def refuse():
        return CoreError('CORE_UNAVAILABLE','The core requires one systemd Unix socket.','Start solace-core.socket or use --socket for isolated development.')
    if env.get('LISTEN_PID') != str(os.getpid()) or env.get('LISTEN_FDS') != '1':
        raise refuse()
    try:
        listener=socket.socket(fileno=os.dup(fd))
    except OSError as error:
        raise refuse() from error
    if (listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM
            or not listener.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN)):
        listener.close();raise refuse()
    return listener
