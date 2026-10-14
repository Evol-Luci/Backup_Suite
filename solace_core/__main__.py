"""Core process entry; production socket activation is supplied by systemd."""
import argparse
import os
from pathlib import Path
import signal
from .paths import CorePaths
from .registry import Registry
from .service import CoreService
from .server import SocketServer, bind_private_socket


def main():
    parser = argparse.ArgumentParser(prog='solace-core')
    parser.add_argument('--socket', type=Path, help='Explicit development socket beneath XDG_RUNTIME_DIR')
    args = parser.parse_args()
    os.umask(0o077)
    paths = CorePaths.from_environment()
    service = CoreService(Registry(paths.registry))
    listener = None
    owned_identity = None
    try:
        if args.socket is None:
            from .activation import activated_socket
            listener = activated_socket()
        else:
            listener = bind_private_socket(args.socket, Path(os.environ['XDG_RUNTIME_DIR']))
            owned_identity = args.socket.stat()
        server = SocketServer(listener, service)
        signal.signal(signal.SIGTERM, lambda *_: server.close())
        signal.signal(signal.SIGINT, lambda *_: server.close())
        try: server.serve_forever()
        finally: server.close()
    finally:
        if listener: listener.close()
        service.close()
        if owned_identity is not None and args.socket.exists() and os.path.samestat(owned_identity, args.socket.stat()):
            args.socket.unlink()


if __name__ == '__main__':
    main()
