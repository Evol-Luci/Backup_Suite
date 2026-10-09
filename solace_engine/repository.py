"""Per-project SQLite checkpoint catalog and verified object references."""

import fnmatch
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .errors import CheckpointNotFound, CorruptObject, InvalidProjectPath, RepositoryError
from .models import Checkpoint, FileEntry, StorageUsage, VerificationReport
from .object_store import ObjectStore


_SCHEMA_VERSION = 1


class ProjectRepository:
    """Capture project trees into one project's private checkpoint store."""

    def __init__(self, project_root: Path, store_dir: Path):
        self.project_root = Path(project_root).absolute()
        self.store_dir = Path(store_dir).absolute()
        if not self.project_root.is_dir() or self.project_root.is_symlink():
            raise InvalidProjectPath(f"project directory is unavailable: {project_root}")
        self._project_identity = self.project_root.stat()
        if self.store_dir == self.project_root:
            raise InvalidProjectPath("store directory cannot be the project root")
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.object_store = ObjectStore(self.store_dir)
        self.database_path = self.store_dir / "checkpoints.sqlite3"
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise InvalidProjectPath(f"unsupported checkpoint schema version: {version}")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parent_id TEXT,
                    file_count INTEGER NOT NULL,
                    logical_bytes INTEGER NOT NULL,
                    warnings_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_entries (
                    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    mode INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    file_digest TEXT NOT NULL,
                    PRIMARY KEY (checkpoint_id, path)
                );
                CREATE TABLE IF NOT EXISTS chunk_refs (
                    checkpoint_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (checkpoint_id, path, ordinal),
                    FOREIGN KEY (checkpoint_id, path)
                        REFERENCES file_entries(checkpoint_id, path)
                );
            """)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _excluded(self, path: Path, relative: str, exclusions: Sequence[str]) -> bool:
        if path == self.store_dir or self.store_dir in path.parents:
            return True
        return any(fnmatch.fnmatchcase(relative, pattern) or
                   fnmatch.fnmatchcase(path.name, pattern) for pattern in exclusions)

    def _scan(self, exclusions: Sequence[str]) -> tuple[list[FileEntry], list[str]]:
        entries: list[FileEntry] = []
        warnings: list[str] = []

        def same_identity(first: os.stat_result, second: os.stat_result) -> bool:
            return (first.st_dev, first.st_ino, stat.S_IFMT(first.st_mode)) == (
                second.st_dev, second.st_ino, stat.S_IFMT(second.st_mode))

        def require_identity(first: os.stat_result, second: os.stat_result, relative: str) -> None:
            if not same_identity(first, second):
                raise RepositoryError(f"entry changed during checkpoint capture: {relative}")

        def walk(directory_fd: int, directory: Path):
            with os.scandir(directory_fd) as stream:
                names = sorted(item.name for item in stream)
            for name in names:
                path = directory / name
                relative = path.relative_to(self.project_root).as_posix()
                if self._excluded(path, relative, exclusions):
                    continue
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    target = os.readlink(name, dir_fd=directory_fd).encode("utf-8", "surrogateescape")
                    require_identity(metadata, os.stat(name, dir_fd=directory_fd,
                                                       follow_symlinks=False), relative)
                    digest = self.object_store.put_bytes(target)
                    entries.append(FileEntry(relative, "symlink", metadata.st_mode & 0o777,
                                             len(target), digest, (digest,)))
                elif stat.S_ISDIR(metadata.st_mode):
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directory_fd)
                    try:
                        require_identity(metadata, os.fstat(child_fd), relative)
                        walk(child_fd, path)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW,
                                      dir_fd=directory_fd)
                    try:
                        opened = os.fstat(file_fd)
                        require_identity(metadata, opened, relative)
                        # The proc fd path remains attached to this inode if another
                        # agent replaces the directory entry during chunking.
                        digest, size, chunks = self.object_store.put_file(
                            Path(f"/proc/self/fd/{file_fd}"))
                    finally:
                        os.close(file_fd)
                    entries.append(FileEntry(relative, "file", metadata.st_mode & 0o777,
                                             size, digest, chunks))
                else:
                    warnings.append(f"Skipped unsupported special file: {relative}")

        root_fd = os.open(self.project_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            require_identity(self._project_identity, os.fstat(root_fd), ".")
            walk(root_fd, self.project_root)
        finally:
            os.close(root_fd)
        return entries, warnings

    def _insert_checkpoint(self, connection: sqlite3.Connection, checkpoint: Checkpoint,
                           entries: list[FileEntry]) -> None:
        connection.execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
            (checkpoint.id, checkpoint.message, checkpoint.created_at,
             checkpoint.parent_id, checkpoint.file_count, checkpoint.logical_bytes,
             json.dumps(checkpoint.warnings)),
        )
        for entry in entries:
            connection.execute(
                "INSERT INTO file_entries VALUES (?, ?, ?, ?, ?, ?)",
                (checkpoint.id, entry.path, entry.kind, entry.mode, entry.size,
                 entry.file_digest),
            )
            connection.executemany(
                "INSERT INTO chunk_refs VALUES (?, ?, ?, ?)",
                ((checkpoint.id, entry.path, index, digest)
                 for index, digest in enumerate(entry.chunk_digests)),
            )

    def create_checkpoint(self, message: str, exclusions: Sequence[str] = ()) -> Checkpoint:
        entries, warnings = self._scan(exclusions)
        for entry in entries:
            for digest in entry.chunk_digests:
                self.object_store.read_object(digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute(
                "SELECT id FROM checkpoints ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            checkpoint = Checkpoint(
                id=uuid.uuid4().hex,
                message=message,
                created_at=datetime.now(timezone.utc).isoformat(),
                parent_id=parent[0] if parent else None,
                file_count=len(entries),
                logical_bytes=sum(entry.size for entry in entries),
                warnings=tuple(warnings),
            )
            self._insert_checkpoint(connection, checkpoint, entries)
        return checkpoint

    @staticmethod
    def _checkpoint(row) -> Checkpoint:
        return Checkpoint(row[0], row[1], row[2], row[3], row[4], row[5],
                          tuple(json.loads(row[6])))

    def list_checkpoints(self) -> list[Checkpoint]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, message, created_at, parent_id, file_count, logical_bytes, "
                "warnings_json FROM checkpoints ORDER BY created_at, rowid"
            ).fetchall()
        return [self._checkpoint(row) for row in rows]

    def get_file_entries(self, checkpoint_id: str) -> list[FileEntry]:
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone() is None:
                raise CheckpointNotFound(f"checkpoint does not exist: {checkpoint_id}")
            rows = connection.execute(
                "SELECT path, kind, mode, size, file_digest FROM file_entries "
                "WHERE checkpoint_id = ? ORDER BY path", (checkpoint_id,),
            ).fetchall()
            entries = []
            for path, kind, mode, size, digest in rows:
                chunks = connection.execute(
                    "SELECT digest FROM chunk_refs WHERE checkpoint_id = ? AND path = ? "
                    "ORDER BY ordinal", (checkpoint_id, path),
                ).fetchall()
                entries.append(FileEntry(path, kind, mode, size, digest,
                                         tuple(row[0] for row in chunks)))
        return entries

    def verify_checkpoint(self, checkpoint_id: str) -> VerificationReport:
        entries = self.get_file_entries(checkpoint_id)
        errors = []
        checked = 0
        for entry in entries:
            whole = hashlib.sha256()
            size = 0
            if not entry.chunk_digests:
                errors.append(f"{entry.path}: no object references")
            for digest in entry.chunk_digests:
                checked += 1
                try:
                    chunk = self.object_store.read_object(digest)
                except (CorruptObject, ValueError, OSError) as error:
                    errors.append(f"{entry.path}: {error}")
                    continue
                whole.update(chunk)
                size += len(chunk)
            if whole.hexdigest() != entry.file_digest:
                errors.append(f"{entry.path}: whole-file digest mismatch")
            if size != entry.size:
                errors.append(f"{entry.path}: file size mismatch")
        return VerificationReport(checkpoint_id, not errors, checked, tuple(errors))

    def storage_usage(self) -> StorageUsage:
        """Report referenced logical bytes and unreferenced published object bytes."""
        with self._connect() as connection:
            logical = connection.execute("SELECT COALESCE(SUM(logical_bytes), 0) FROM checkpoints").fetchone()[0]
            referenced = {row[0] for row in connection.execute("SELECT DISTINCT digest FROM chunk_refs")}
        physical = 0
        reclaimable = 0
        for shard in (self.store_dir / "objects").glob("*/*"):
            if not shard.is_file() or len(shard.name) != 64:
                continue
            size = shard.stat().st_size
            physical += size
            if shard.name not in referenced:
                reclaimable += size
        return StorageUsage(logical, physical, reclaimable)
