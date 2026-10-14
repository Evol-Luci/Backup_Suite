"""Conservative local-filesystem placement and mount identity checks."""
from dataclasses import dataclass
from pathlib import Path
import re
import os
import json
from .errors import CoreError, invalid

LOCAL_FILESYSTEMS = {'ext2', 'ext3', 'ext4', 'btrfs', 'xfs', 'f2fs', 'zfs'}


@dataclass(frozen=True)
class StorageLocation:
    canonical: Path
    mount_id: int
    filesystem: str
    device: int
    inode: int


class StoragePolicy:
    def __init__(self, mountinfo=None, sync_roots=None):
        self.mountinfo = mountinfo
        if sync_roots is None:
            sync_roots = [Path.home() / name for name in ('Dropbox', 'Google Drive', 'Nextcloud')]
            data = Path(os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local/share'))
            config = data / 'solace/core-config.json'
            if config.exists():
                try:
                    value = json.loads(config.read_text())
                    configured = value.get('sync_roots', [])
                    if (not isinstance(value, dict) or set(value) - {'sync_roots'}
                            or not isinstance(configured, list)
                            or any(not isinstance(p, str) or not Path(p).is_absolute() or '\0' in p for p in configured)):
                        raise ValueError()
                    sync_roots += configured
                except (ValueError, TypeError, AttributeError, OSError) as error:
                    raise invalid('The core configuration must contain only absolute sync_roots paths.') from error
        self.sync_roots = [Path(p).resolve() for p in sync_roots]

    def check(self, path, *, creating=False):
        path = Path(path).resolve()
        existing = path
        if not creating and not path.is_dir():
            raise CoreError('STORE_UNAVAILABLE', 'The configured directory is unavailable.', 'Reconnect storage or explicitly relink.')
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if not existing.is_dir():
            raise CoreError('STORE_UNAVAILABLE', 'A storage ancestor is not a directory.', 'Choose an accessible local directory.')
        if any(path.is_relative_to(root) for root in self.sync_roots):
            raise CoreError('UNSUPPORTED_STORAGE', 'Synchronized storage is unsupported.', 'Choose an unsynchronized local filesystem.')
        text = self.mountinfo if self.mountinfo is not None else Path('/proc/self/mountinfo').read_text()
        candidates = []
        for line in text.splitlines():
            left, right = line.split(' - ', 1)
            fields, tail = left.split(), right.split()
            mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
            if path.is_relative_to(mount):
                candidates.append((len(mount.parts), int(fields[0]), tail[0], fields[5].split(','), tail[2].split(',')))
        if not candidates:
            raise CoreError('UNSUPPORTED_STORAGE', 'Cannot identify the storage filesystem.', 'Choose a supported local filesystem.')
        _, mount_id, filesystem, opts, superopts = max(candidates)
        if filesystem not in LOCAL_FILESYSTEMS or 'ro' in opts or 'ro' in superopts:
            raise CoreError('UNSUPPORTED_STORAGE', 'Storage is unsupported or read-only.', 'Use a writable local ext4, btrfs, xfs, f2fs, or zfs filesystem.')
        meta = existing.stat()
        return StorageLocation(path, mount_id, filesystem, meta.st_dev, meta.st_ino)
