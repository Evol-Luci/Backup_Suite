"""Verified immutable checkpoint views, independent of live source locations."""
from dataclasses import asdict
import hashlib
import json
import os
import stat
import re
import uuid

from solace_engine.errors import RepositoryError, CorruptObject
from solace_engine.models import Checkpoint, FileEntry
from solace_engine.object_store import ObjectStore
from solace_engine.repository import ProjectRepository
from .errors import CoreError, classify
from .integration_types import Version
from .workspace_trees import _manifest_digest, validate_workspace_links, tree_directory


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def entry_json(entry):
    return asdict(entry) if entry is not None else None


def entry_from_json(value):
    if value is None:
        return None
    return FileEntry(**dict(value, chunk_digests=tuple(value['chunk_digests'])))


class SnapshotStore:
    def __init__(self, catalog):
        self.catalog = catalog
        catalog.validate(require_root=False)
        self.objects = ObjectStore.open_existing(catalog.store)
        # The engine insertion primitive needs an existing repository handle,
        # but never reads its project root. Use the store's surviving parent,
        # never the mutable task workspace or registered source.
        self.repository = ProjectRepository.open_existing(catalog.store.parent, catalog.store)

    def iter_bytes(self, entry: FileEntry):
        """Yield bounded blocks; content is verified only after full exhaustion.

        Verify each CAS object as well as the whole file without allocating an
        entire object, even if a previous caller stored a large single chunk.
        Consumers must exhaust this iterator before publishing any staged bytes.
        """
        try:
            self._metadata(entry)
            whole = hashlib.sha256(); size = 0
            for digest in entry.chunk_digests:
                path = self.objects._object_path(digest)
                try:
                    with tree_directory(path.parent) as parent:
                        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                        try:
                            before = os.fstat(fd)
                            if not stat.S_ISREG(before.st_mode):
                                raise CorruptObject('CAS object is not a regular file')
                            chunk = hashlib.sha256()
                            while True:
                                data = os.read(fd, 64*1024)
                                if not data: break
                                chunk.update(data); whole.update(data); size += len(data)
                                yield data
                            after = os.fstat(fd)
                            attributes = ('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')
                            if (chunk.hexdigest() != digest
                                    or any(getattr(before, key) != getattr(after, key) for key in attributes)):
                                raise CorruptObject('CAS object digest or identity changed')
                        finally:
                            os.close(fd)
                except OSError as error:
                    raise CorruptObject('CAS object is unavailable or unsafe') from error
            if size != entry.size or whole.hexdigest() != entry.file_digest:
                raise CorruptObject('whole-file size or digest failed verification')
        except (RepositoryError, ValueError, OSError) as error:
            raise classify(error) from error

    def read_bytes(self, entry: FileEntry) -> bytes:
        """Complete value for bounded merge inputs or symlink targets."""
        return b''.join(self.iter_bytes(entry))

    @staticmethod
    def _metadata(entry):
        ProjectRepository._validate_restore_entries((entry,))
        if (type(entry.mode) is not int or not 0 <= entry.mode <= 0o777
                or type(entry.size) is not int or entry.size < 0
                or not isinstance(entry.file_digest, str)
                or re.fullmatch('[0-9a-f]{64}', entry.file_digest) is None
                or not entry.chunk_digests):
            raise CoreError('INTEGRATION_MANIFEST_INVALID', 'Invalid immutable entry metadata.',
                            'Preserve the checkpoint and inspect its manifest.')

    def _verify(self, entries):
        try:
            ProjectRepository._validate_restore_entries(entries)
            if len({entry.path for entry in entries}) != len(entries):
                raise ValueError('duplicate path')
            targets = {}
            for entry in entries:
                if entry.kind == 'symlink':
                    targets[entry.path] = self.read_bytes(entry)
                else:
                    for _ in self.iter_bytes(entry):
                        pass
            validate_workspace_links(entries, targets)
            return _manifest_digest(entries, targets)
        except (RepositoryError, ValueError) as error:
            raise CoreError('INTEGRATION_MANIFEST_INVALID', 'Invalid immutable manifest or link graph.',
                            'Preserve all versions and inspect the invalid entries.') from error

    def _load(self, db, checkpoint_id, profile):
        row = db.execute('SELECT c.file_count,c.logical_bytes,x.profile_json,c.warnings_json FROM checkpoints c '
                         'JOIN checkpoint_context x ON x.checkpoint_id=c.id WHERE c.id=?', (checkpoint_id,)).fetchone()
        if row is None:
            raise CoreError('CHECKPOINT_NOT_FOUND', 'The checkpoint has no retained context.', 'Select a saved task result.')
        if json.loads(row[3]):
            raise CoreError('INTEGRATION_CAPTURE_INCOMPLETE', 'The immutable input contains in-scope capture warnings.',
                            'Save a complete task result before preparing integration.')
        if json.loads(row[2]) != profile:
            raise CoreError('INTEGRATION_PROFILE_MISMATCH', 'The checkpoint uses a different capture profile.',
                            'Use a result and contribution baseline with exactly the same profile.')
        entries = tuple(FileEntry(r[0], r[1], r[2], r[3], r[4], tuple(c[0] for c in db.execute(
            'SELECT digest FROM chunk_refs WHERE checkpoint_id=? AND path=? ORDER BY ordinal', (checkpoint_id, r[0]))))
            for r in db.execute('SELECT path,kind,mode,size,file_digest FROM file_entries WHERE checkpoint_id=? ORDER BY path', (checkpoint_id,)))
        if (row[0], row[1]) != (len(entries), sum(e.size for e in entries)):
            raise CoreError('INTEGRATION_MANIFEST_INVALID', 'Checkpoint totals do not match its manifest.',
                            'Preserve the checkpoint store and inspect its metadata.')
        version = Version(checkpoint_id, self._verify(entries), canonical(profile))
        return version, entries

    def version(self, checkpoint_id: str, profile: dict) -> Version:
        with self.catalog.connect() as db:
            return self._load(db, checkpoint_id, profile)[0]

    def entries(self, version: Version) -> tuple[FileEntry, ...]:
        with self.catalog.connect() as db:
            verified, entries = self._load(db, version.checkpoint_id, json.loads(version.profile_json))
        if verified != version:
            raise CoreError('INTEGRATION_MANIFEST_INVALID', 'The immutable version no longer matches its manifest.',
                            'Preserve the store and inspect the retained plan.')
        return entries

    def write_bytes(self, path: str, data: bytes, mode: int) -> FileEntry:
        digest = self.objects.put_bytes(data)
        entry = FileEntry(path, 'file', mode, len(data), digest, (digest,))
        self._metadata(entry)
        return entry

    def insert(self, db, entries: tuple[FileEntry, ...], *, parent_id: str | None,
               profile: dict, task_id: str, purpose: str, published: bool = False) -> Version:
        if purpose not in ('source', 'workspace') or published:
            raise CoreError('INVALID_REQUEST', 'Integration snapshots must be unpublished source/workspace contexts.',
                            'Publish history only through the integration success transaction.')
        if parent_id is not None and db.execute('SELECT 1 FROM checkpoints WHERE id=?', (parent_id,)).fetchone() is None:
            raise CoreError('CHECKPOINT_NOT_FOUND', 'The explicit snapshot parent does not exist.', 'Inspect the target history.')
        entries = tuple(sorted(entries, key=lambda entry: entry.path))
        digest = self._verify(entries)
        checkpoint = Checkpoint(uuid.uuid4().hex, 'Integration snapshot', self.catalog._now(), parent_id,
                                len(entries), sum(e.size for e in entries))
        self.repository._insert_checkpoint(db, checkpoint, entries)
        workspace = db.execute('SELECT id FROM workspaces WHERE task_id=?', (task_id,)).fetchone() if purpose == 'workspace' else None
        db.execute('INSERT INTO checkpoint_context VALUES (?,?,?,?,?,0)',
                   (checkpoint.id, purpose, task_id if purpose == 'workspace' else None,
                    workspace[0] if workspace else None, canonical(profile)))
        return Version(checkpoint.id, digest, canonical(profile))
