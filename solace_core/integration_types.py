"""Private immutable integration records; public responses use explicit projections."""
from dataclasses import dataclass
from pathlib import Path
from solace_engine.models import FileEntry

@dataclass(frozen=True)
class Identity:
    path: str
    mount_id: int
    device: int
    inode: int

@dataclass(frozen=True)
class Version:
    checkpoint_id: str
    manifest_sha256: str
    profile_json: str

@dataclass(frozen=True)
class MergeConflict:
    path: str
    reason: str
    base: FileEntry | None
    current: FileEntry | None
    incoming: FileEntry | None

@dataclass(frozen=True)
class MergeOutcome:
    entries: tuple[FileEntry, ...]
    conflicts: tuple[MergeConflict, ...]

@dataclass(frozen=True)
class Target:
    key: str                  # "source" or "workspace:" + workspace ID
    task_id: str | None        # parent task for workspace target
    revision: int | None      # source target uses expected source head
    head: str | None
    identity: Identity

@dataclass(frozen=True)
class PreparedPlan:
    id: str
    revision: int
    state: str
    task_id: str
    result_id: str
    target: Target
    base: Version
    incoming: Version
    current: Version
    candidate: Version
    conflicts: tuple[MergeConflict, ...]

@dataclass(frozen=True)
class Journal:
    operation_id: str
    plan_id: str
    revision: int
    path: Path
    identity: Identity | None  # None only while reserved and not yet created.
    target: Identity
