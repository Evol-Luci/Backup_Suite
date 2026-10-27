"""Validated XDG locations; no shared temporary socket fallback."""
from dataclasses import dataclass
from pathlib import Path
import os
import stat
from .errors import CoreError, invalid


@dataclass(frozen=True)
class CorePaths:
    socket: Path
    registry: Path
    projects: Path

    @classmethod
    def from_environment(cls, env=None, uid=None):
        env = os.environ if env is None else env
        uid = os.getuid() if uid is None else uid
        runtime = env.get('XDG_RUNTIME_DIR')
        if not runtime:
            raise CoreError('CORE_UNAVAILABLE', 'XDG_RUNTIME_DIR is unavailable.', 'Run Solace in your logged-in user session.')
        root = Path(runtime)
        if not root.is_absolute():
            raise invalid('XDG_RUNTIME_DIR must be absolute.')
        try:
            meta = root.lstat()
        except OSError as error:
            raise CoreError('CORE_UNAVAILABLE', 'The session runtime directory is unavailable.', 'Run Solace in your logged-in user session.') from error
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid or meta.st_mode & 0o077:
            raise CoreError('PERMISSION_DENIED', 'The session runtime directory is not private to this user.', 'Check its ownership and mode.')
        data = Path(env.get('XDG_DATA_HOME') or str(Path(env.get('HOME', str(Path.home()))) / '.local/share'))
        if not data.is_absolute():
            raise invalid('XDG_DATA_HOME must be absolute.')
        return cls(root / 'solace/core.sock', data / 'solace/registry.sqlite3', data / 'solace/projects')
