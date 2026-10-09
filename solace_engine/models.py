"""Immutable value records used by the local checkpoint engine."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Checkpoint:
    id: str
    message: str
    created_at: str
    parent_id: str | None
    file_count: int
    logical_bytes: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileEntry:
    path: str
    kind: str
    mode: int
    size: int
    file_digest: str
    chunk_digests: tuple[str, ...]


@dataclass(frozen=True)
class VerificationReport:
    checkpoint_id: str
    ok: bool
    checked_objects: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class StorageUsage:
    logical_bytes: int
    physical_bytes: int
    reclaimable_bytes: int
