# Local Checkpoint Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first dependable per-project checkpoint store with verified content-addressed storage and safe restore.

**Architecture:** Add an isolated `solace_engine` Python package. Each project uses an injected local store directory containing its own SQLite metadata database and content-addressed objects; objects are shared within that project only. The first slice exposes a Python API for capture, list, verification, restore into a new destination, and reclaimable-space reporting. It does not yet add the daemon, task manager, GUI, remote replication, Git import/export, or destructive garbage collection.

**Tech Stack:** Python 3.10+ syntax, `sqlite3`, `hashlib.sha256`, `pathlib`, `os`, and `unittest`; implement deterministic FastCDC chunking in the package without a runtime dependency.

**Spec:** `docs/specs/local-first-agent-version-engine.md`

## Global Constraints

- Linux is the first target; keep filesystem operations behind standard Python interfaces where practical.
- A project store is isolated from every other project; checkpoints in one project share objects.
- Store directories are passed to the repository API and remain local; do not put SQLite databases on network filesystems.
- Checkpoints are immutable and become visible only after every referenced object has been written and verified.
- Capture all regular project files by default, including hidden files, with explicit exclusion patterns.
- Preserve symlinks as link values without following them, and preserve the executable bit; do not preserve ownership, timestamps, ACLs, or extended attributes in this slice.
- Store files up to 1 MiB as one object; chunk larger files with FastCDC using 64 KiB minimum, 256 KiB target average, and 1 MiB maximum chunks.
- Restore only into a destination that does not exist; stage beside the destination and rename only after all files are materialized.
- Do not delete unreferenced objects in this slice; report reclaimable bytes for later policy-gated garbage collection.
- Leave the legacy `ProjectVault`, GUI, backup-job behavior, and installed settings untouched.

## Review Focus

- Process interruption during object write: incomplete temporary files must never be treated as valid objects.
- Database failure after object publication: no checkpoint may reference missing objects; unreferenced objects remain safe to report for cleanup.
- Corrupt or missing object during verification/restore: verification must identify the object and restore must not publish a partial destination.
- Malicious or unusual paths and symlinks: relative paths cannot escape the project, and restoring a symlink must not follow its target.
- Concurrent checkpoint requests: SQLite transactions must leave complete checkpoint rows and object references without cross-project visibility.

---

## File structure

- Create `solace_engine/__init__.py` — public exports for the repository API.
- Create `solace_engine/errors.py` — user-actionable storage and checkpoint exceptions.
- Create `solace_engine/models.py` — immutable checkpoint, file-entry, verification, and storage-usage dataclasses.
- Create `solace_engine/chunking.py` — deterministic FastCDC chunk boundaries and streaming file reader.
- Create `solace_engine/object_store.py` — per-project object pathing, atomic writes, and digest verification.
- Create `solace_engine/repository.py` — SQLite schema and checkpoint, verify, restore, and usage operations.
- Create `tests/solace_engine/test_chunking.py` — chunk boundary and reconstruction cases.
- Create `tests/solace_engine/test_models.py` — stable value and error types.
- Create `tests/solace_engine/test_object_store.py` — atomic object writes and content verification.
- Create `tests/solace_engine/test_repository.py` — checkpoint lifecycle, isolation, restore, and usage reporting.

## Task 1: Define the engine API and records

**Files:**
- Create: `solace_engine/__init__.py`
- Create: `solace_engine/errors.py`
- Create: `solace_engine/models.py`
- Create: `tests/solace_engine/test_models.py`

**Interfaces:**
- Produce `Checkpoint(id: str, message: str, created_at: str, parent_id: str | None, file_count: int, logical_bytes: int, warnings: tuple[str, ...] = ())`.
- Produce `FileEntry(path: str, kind: str, mode: int, size: int, file_digest: str, chunk_digests: tuple[str, ...])`.
- Produce `VerificationReport(checkpoint_id: str, ok: bool, checked_objects: int, errors: tuple[str, ...])`.
- Produce `StorageUsage(logical_bytes: int, physical_bytes: int, reclaimable_bytes: int)`.
- Checkpoint creation returns structured warnings for unsupported special files.
- Domain errors: `RepositoryError`, `InvalidProjectPath`, `CheckpointNotFound`, and `CorruptObject`.

- [ ] **Step 1: Write model and error tests**

Create dataclass tests for stable field values, optional parent IDs, and error inheritance.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_models.py' -v`  
Expected: import failures because the package does not exist yet.

- [ ] **Step 3: Add the minimal models and errors**

Create the dataclasses and exception hierarchy exactly as listed in the interface block. Keep serialization out of these model classes.

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_models.py' -v`  
Expected: all model and inheritance tests pass.

- [ ] **Step 5: Commit the API records**

```bash
git add solace_engine/__init__.py solace_engine/errors.py solace_engine/models.py tests/solace_engine/test_models.py
git commit -m "feat: define checkpoint engine records"
```

## Task 2: Implement deterministic content-defined chunking

**Files:**
- Create: `solace_engine/chunking.py`
- Create: `tests/solace_engine/test_chunking.py`

**Interfaces:**
- `iter_file_chunks(path: Path, min_size: int = 65536, average_size: int = 262144, max_size: int = 1048576) -> Iterator[bytes]`.
- `split_bytes(data: bytes, min_size: int = 65536, average_size: int = 262144, max_size: int = 1048576) -> list[bytes]` for deterministic unit tests only.
- For nonempty inputs up to 1 MiB, both APIs return one whole-file chunk; FastCDC splits only larger inputs. The empty-input ruling in the ledger still applies.

- [ ] **Step 1: Write chunking tests**

Cover empty input, files below and at 1 MiB, a file immediately above 1 MiB, multi-chunk reconstruction, deterministic boundaries for fixed content, and bounded chunk sizes except the final chunk.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_chunking.py' -v`  
Expected: import failures because `chunking.py` does not exist yet.

- [ ] **Step 3: Implement deterministic FastCDC**

Use a fixed deterministic gear table, enforce the minimum/average/maximum boundaries from the interface, and stream file data without loading an entire large file into memory.

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_chunking.py' -v`  
Expected: all chunk boundary, repeatability, and reconstruction tests pass.

- [ ] **Step 5: Commit content-defined chunking**

```bash
git add solace_engine/chunking.py tests/solace_engine/test_chunking.py
git commit -m "feat: add deterministic file chunking"
```

## Task 3: Add the per-project atomic object store

**Files:**
- Create: `solace_engine/object_store.py`
- Create: `tests/solace_engine/test_object_store.py`

**Interfaces:**
- Consumes `iter_file_chunks` from Task 2 and `CorruptObject` from Task 1.
- `ObjectStore(store_dir: Path)`.
- `put_bytes(data: bytes) -> str` returns the lowercase SHA-256 digest.
- `put_file(path: Path) -> tuple[str, int, tuple[str, ...]]` returns whole-file digest, byte count, and ordered chunk digests; files at or below 1 MiB use one object.
- `read_object(digest: str) -> bytes` validates the digest before returning bytes.
- `verify_object(digest: str) -> bool` checks presence and digest integrity.
- Produces `ObjectStore` for Task 4.

- [ ] **Step 1: Write object-store tests**

Cover repeat writes reusing the same digest, byte-for-byte reads, malformed digest rejection, tamper detection, and the absence of a published partial object after a simulated interrupted write.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_object_store.py' -v`  
Expected: import failures because `object_store.py` does not exist yet.

- [ ] **Step 3: Implement object writes and verification**

Write to a unique temporary file in the destination filesystem, flush and `fsync` it, verify the digest, then atomically rename it to `<store_dir>/objects/<first-two-hex>/<digest>`. If the object already exists, verify it rather than replacing it.

- [ ] **Step 4: Run the focused tests**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_object_store.py' -v`  
Expected: all deduplication, tamper, and interrupted-write tests pass.

- [ ] **Step 5: Commit the object store**

```bash
git add solace_engine/object_store.py tests/solace_engine/test_object_store.py
git commit -m "feat: add atomic project object store"
```

## Task 4: Capture and list immutable checkpoints

**Files:**
- Create: `solace_engine/repository.py`
- Modify: `solace_engine/__init__.py`
- Create: `tests/solace_engine/test_repository.py`

**Interfaces:**
- Consumes the Task 1 models and the Task 3 `ObjectStore`.
- Produces `ProjectRepository(project_root: Path, store_dir: Path)` and its create/list/verify operations for Task 5.
- `ProjectRepository.create_checkpoint(message: str, exclusions: Sequence[str] = ()) -> Checkpoint`.
- `ProjectRepository.list_checkpoints() -> list[Checkpoint]`.
- `ProjectRepository.verify_checkpoint(checkpoint_id: str) -> VerificationReport`.
- Record each checkpoint's relative file paths, entry kind, executable bit, byte size, whole-file digest, and ordered chunk digests in SQLite tables.

- [ ] **Step 1: Write repository tests**

Cover a simple project with regular files, hidden files, an excluded file, a symlink that points outside the project, an executable file, the store directory nested under the project, and repeated content. Assert that the scan never follows the symlink or recaptures the store, and that two checkpoints share object IDs while retaining separate immutable checkpoint records. Change a file after the first checkpoint and prove the first remains restorable. Create a second project store and prove its objects are not shared with the first. Inject a database failure after object publication and assert no checkpoint is visible and the orphan bytes are reported reclaimable. Add two simultaneous checkpoint calls through separate repository instances and assert both records verify.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_repository.py' -v`  
Expected: import failures because `repository.py` does not exist yet.

- [ ] **Step 3: Implement schema and checkpoint publication**

Use one SQLite database per project store, enable WAL mode, and create versioned tables for checkpoints, file entries, and ordered chunk references. Scan without following symlinks. Write and verify every referenced object before inserting the complete checkpoint in one database transaction. Unsupported special files are skipped and included in a structured warning list.

- [ ] **Step 4: Implement list and verification operations**

List checkpoints by creation time. Verify that each file manifest's chunk sequence reconstructs the recorded whole-file digest and that every referenced object exists and matches its SHA-256 digest.

- [ ] **Step 5: Run the focused tests**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_repository.py' -v`  
Expected: capture, exclusion, symlink, executable-bit, deduplication, listing, and corruption-reporting tests pass.

- [ ] **Step 6: Commit checkpoint capture and verification**

```bash
git add solace_engine/repository.py solace_engine/models.py tests/solace_engine/test_repository.py
git commit -m "feat: capture verified local checkpoints"
```

## Task 5: Restore into a new destination and report usage

**Files:**
- Modify: `solace_engine/repository.py`
- Modify: `tests/solace_engine/test_repository.py`

**Interfaces:**
- Consumes the Task 4 checkpoint schema and list/verify operations.
- `ProjectRepository.restore_checkpoint(checkpoint_id: str, destination: Path) -> None`.
- `ProjectRepository.storage_usage() -> StorageUsage`.
- Restore refuses an existing destination. It verifies all referenced objects first, materializes into a unique sibling staging directory, restores symlinks without following them and the executable bit on regular files, then atomically renames the completed staging directory into place.
- Usage reports logical bytes from the newest checkpoint's file entries (the current captured project state), physical bytes from all stored object files, and reclaimable bytes for valid objects with no SQLite references. This task reports reclaimable bytes but never deletes objects.

- [ ] **Step 1: Write restore and usage tests**

Cover successful content restoration, executable-bit and symlink preservation, refusal to overwrite an existing destination, corrupted-object failure without a published destination, and expected logical/physical/reclaimable byte totals.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python -m unittest discover -s tests/solace_engine -p 'test_repository.py' -v`  
Expected: the new restore and usage cases fail because the methods do not exist.

- [ ] **Step 3: Implement staged restore and usage calculation**

Preflight every object before creating the staging tree. On any write or rename failure, remove only the staging tree created by this operation and leave existing paths untouched.

- [ ] **Step 4: Run all engine tests**

Run: `python -m unittest discover -s tests/solace_engine -v`  
Expected: all engine model, chunking, object-store, checkpoint, verification, restore, and usage tests pass.

- [ ] **Step 5: Commit the engine foundation**

```bash
git add solace_engine tests/solace_engine
git commit -m "feat: add local checkpoint engine foundation"
```

## Follow-up plans

After this engine foundation is reviewed, create separate plans for the per-user core service and project registry, agent task/workspace lifecycle, check and review integration, CLI/GUI surfaces, Git migration/export, and local-folder/Google Drive adapters. Do not extend this plan into those independent subsystems.
