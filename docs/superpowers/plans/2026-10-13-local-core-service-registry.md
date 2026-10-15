# Local Core Service and Project Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execute inline; parallel agents are unnecessary for these sequential interfaces unless the user selects delegation.

**Goal:** Give people and agents one local, recoverable authority for project registration and checkpoint operations.

**Architecture:** A systemd-activated Unix socket serves a versioned JSON API. A user registry locates identity-checked project stores; each store's existing checkpoint database also contains operation and event records. Filesystem work runs outside SQLite transactions, with independent coordination for each project and atomic checkpoint/result/event publication.

**Tech Stack:** Existing Python standard library, SQLite, Linux Unix sockets and peer credentials, systemd user units, and unittest. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-10-09-local-core-service-registry-design.md`.

**Execution status:** Approved 2026-10-13; all eight implementation tasks completed and tested. Independent-review remediation and real user-manager installation/acceptance completed with authorization on 2026-10-15.

## Global Constraints

- Linux first; systemd user services and Unix domain sockets.
- The core does not launch agents in this milestone.
- The core does not expose a network listener or depend on hosted infrastructure.
- Registration does not scan the source tree or make an initial checkpoint.
- The core must never silently create a replacement empty store at another path.
- Unregistering removes the registry entry only. It preserves the project store.
- A state change and its event are committed together; the core publishes the event only after the transaction succeeds.
- Events do not contain prompt text.
- Repair, relocation, and deletion require explicit operations with their own reviewable results.
- Existing `ProjectVault` GUI flows and scheduled backup jobs remain unchanged until separate migration work is approved.
- The new commands do not modify legacy vault records or `.git` data.

## Review Focus

1. A removed mount can expose an empty directory at the same path: validate store identity and mount identity before opening; never initialize it through an ordinary read or write request.
2. A reply can be lost after commit: reusing the same mutation request ID returns the original result and never creates a duplicate checkpoint or registration.
3. Replay can overlap a live event: the durable cursor is authoritative; never lose or duplicate an event at the replay/live boundary.
4. Relink can race with a checkpoint: use the same project coordinator and require the user's confirmed canonical root to match the current request.
5. A database can be corrupt, newer, or read-only: diagnostics remain read-only and return structured failures; operations do not recreate, migrate blindly, or repair it.

## Baseline and working location

Work in the existing clean linked worktree `.worktrees/local-core-design`, branch `codex/local-core-design`, starting at `3e1ddc5`. The root `main` checkout is historical; the newer installed-source baseline and reviewed engine are already present in this worktree.

Fresh verification on 2026-10-13:

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

The engine run passed 49 tests; the top-level run passed 16 tests, including seven Tk tests with desktop-display access. Sandboxed Tk initially could not connect to `:0`; the same tests passed outside the sandbox. That verifies the existing Tk tests, not a complete interactive GUI audit.

## Decisions and interfaces

### Paths and storage validation

`CorePaths.from_environment(env: Mapping[str, str], uid: int) -> CorePaths` selects:

- Socket: `$XDG_RUNTIME_DIR/solace/core.sock`; runtime root must exist, be owned by the current UID, and have no group/other access. Do not fall back to `/tmp`.
- Registry: `$XDG_DATA_HOME/solace/registry.sqlite3`, with default data root `~/.local/share`. XDG paths must be absolute.
- Default store: `<data-root>/solace/projects/<uuid-hex-project-id>`.
- Solace-owned directories use mode `0700`; the socket uses `0600`; registry and project database files use `0600`.

`StoragePolicy.check(path: Path, *, creating: bool) -> StorageLocation` resolves the nearest existing ancestor, parses `/proc/self/mountinfo`, decodes octal path escapes, and uses the longest containing mount path. Store the mount ID, filesystem type, and canonical path. Recheck before operations. Permit local `ext2`, `ext3`, `ext4`, `btrfs`, `xfs`, `f2fs`, and `zfs`; reject read-only mounts and all other types in this release. Explicitly test NFS, CIFS, SSHFS, other FUSE, overlay, and tmpfs rejection. Reject stores equal to or containing the source root. Nested stores are permitted because the engine excludes them by physical identity.

Ordinary local filesystems do not reveal whether a background sync tool manages a directory. Reject known sync roots from configured `sync_roots` plus conventional existing `~/Dropbox`, `~/Google Drive`, and `~/Nextcloud` roots, resolving aliases and checking ancestry. Document that this cannot detect arbitrary sync tooling; users must choose an unsynchronized local path. No claim of universal sync detection. Missing configured paths are failures, not candidates for fallback.

`StorageLocation` records mount ID, filesystem type, canonical path, device/inode identity. A changed mount ID requires explicit validation via relink, even if persistent store identity matches. Device/inode checks guard a single operation; persisted store UUID is the identity across explicit relocation. Same-user hostile modification remains outside the threat model.

### Wire protocol

Use UTF-8 JSON followed by a newline, with a maximum encoded frame of 1 MiB. Reject invalid UTF-8, non-object JSON, non-finite numbers, extra fields, unknown methods, booleans in integer fields, and overlong IDs. Close a connection after invalid framing. Do not log raw requests. Each ordinary client connection has one outstanding request; subscriptions use a dedicated connection.

```json
{"version":1,"id":"request-uuid","method":"project.list","params":{},"actor":"cli"}
{"version":1,"id":"request-uuid","ok":true,"result":{"projects":[]}}
{"version":1,"id":"request-uuid","ok":false,"error":{"code":"STORE_UNAVAILABLE","message":"The registered store is unavailable.","next_step":"Reconnect the drive or explicitly relink this project.","retryable":false}}
```

`actor` is a user-supplied label, not authentication. Limit it to 128 printable characters. Credentials come from Linux `SO_PEERCRED`; reject different UIDs before reading frames. The API version is exactly `1`; unsupported versions return `PROTOCOL_VERSION_UNSUPPORTED`. Responses and event frames have the same version and correlation ID.

Stable initial codes: `INVALID_REQUEST`, `PROTOCOL_VERSION_UNSUPPORTED`, `PERMISSION_DENIED`, `CORE_UNAVAILABLE`, `PROJECT_NOT_FOUND`, `PROJECT_ALREADY_REGISTERED`, `PROJECT_UNAVAILABLE`, `STORE_UNAVAILABLE`, `STORE_IDENTITY_MISMATCH`, `UNSUPPORTED_STORAGE`, `DATABASE_CORRUPT`, `SCHEMA_VERSION_UNSUPPORTED`, `REQUEST_ID_REUSED`, `CONFIRMATION_REQUIRED`, `PROJECT_BUSY`, `CHECKPOINT_NOT_FOUND`, `CAPTURE_CHANGED`, `CORRUPT_OBJECT`, `DESTINATION_EXISTS`, `OPERATION_INTERRUPTED`, `CURSOR_INVALID`, `INTERNAL_ERROR`. Keep the original domain-error classification; unexpected errors return a generic message and log only exception type and operation ID.

### Database ownership and numbering

Registry schema version `1` uses its own `PRAGMA user_version`. Project checkpoint schema stays at engine version `1`; add a singleton `core_metadata` table with a separate `schema_version=1`. Do not repurpose the engine's `PRAGMA user_version`. Only explicit creation initializes schema. Existing-store opens check both versions before changes; future migrations require a separately reviewed migration.

```sql
-- registry.sqlite3
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    root TEXT NOT NULL UNIQUE,
    store TEXT NOT NULL UNIQUE,
    store_uuid TEXT NOT NULL UNIQUE,
    mount_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE requests (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE registration_intents (
    request_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    project_id TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL UNIQUE,
    store TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('preparing','ready','interrupted'))
);

-- existing checkpoints.sqlite3, alongside engine tables
CREATE TABLE core_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    store_uuid TEXT NOT NULL
);
CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','interrupted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    operation_id TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    details_json TEXT NOT NULL
);
```

Connection setup: foreign keys enabled, WAL, synchronous FULL, 30-second busy timeout; short `BEGIN IMMEDIATE` writes. Read-only diagnostics use SQLite URI `mode=ro` and `PRAGMA query_only=ON`, with no schema creation or journal-mode changes.

Registration spans two databases and a directory; do not claim cross-database atomicity. Commit a registry intent first, exclusively create an empty store directory, initialize its identity, then atomically publish the registration and cached result in the registry. After a crash, startup reconciles durable intents against the matching store identity. An absent or mismatched store yields `interrupted`, never an automatically recreated store. Unregistered stores left by a failed add remain intact and are reported by diagnostics. For relink/unregister, the registry transaction is authoritative; registry lifecycle changes are outside the project's checkpoint event stream in this milestone.

Fingerprint = SHA-256 of canonical JSON `{method,params,actor}`; requests require UUID IDs. Retry with identical ID and fingerprint returns the original registration result or operation ID, even if a later unregister removed its registration. Different parameters with the same ID return `REQUEST_ID_REUSED`. Request receipts and events are retained without pruning in version 1. A cursor is an integer in `[0,current_max]`; later cursors return `CURSOR_INVALID`. Unregister ends subscriptions with a structured `PROJECT_NOT_FOUND` response.

### Core and engine interfaces

```python
class Registry:
    def add(self, root: Path, store: Path | None, request_id: str, actor: str) -> dict: ...
    def list(self) -> list[dict]: ...
    def show(self, project_id: str) -> dict: ...
    def relink(self, project_id: str, root: Path, store: Path,
               confirmed_root: str, request_id: str, actor: str) -> dict: ...
    def unregister(self, project_id: str, request_id: str, actor: str) -> dict: ...

class ProjectCatalog:
    def open_existing(self, registration: dict) -> ProjectRepository: ...
    def accept_operation(self, request: dict) -> dict: ...
    def transition(self, operation_id: str, state: str,
                   result: dict | None = None, error: dict | None = None) -> dict: ...
    def get_operation(self, operation_id: str) -> dict: ...
    def events_after(self, cursor: int, limit: int = 256) -> list[dict]: ...

class CoreService:
    def handle(self, request: dict) -> dict: ...
    def close(self) -> None: ...

class CoreClient:
    def request(self, method: str, params: dict, *, request_id: str | None = None) -> dict: ...
    def subscribe(self, project_id: str, after: int = 0): ...
```

Method mapping: `core.status`, `doctor`, `project.add/list/show/relink/unregister`, `checkpoint.create/list/verify/restore/usage`, `operation.show`, `event.subscribe`. Select projects by stable ID in the API. CLI also resolves exact canonical roots through registry lookup, without scanning. No fuzzy prefixes in this milestone.

Extend the engine without breaking current callers:

```python
ProjectRepository.open_existing(project_root: Path, store_dir: Path) -> ProjectRepository
ObjectStore.open_existing(store_dir: Path) -> ObjectStore
ProjectRepository.create_checkpoint(message: str, exclusions: Sequence[str] = (), *,
                                    publication_hook=None) -> Checkpoint
```

Repository `open_existing` requires existing catalog and object directories, checks schema, and opens SQLite using `mode=rw`; it never runs schema initialization or `mkdir`. Existing read methods must reuse that no-create connection policy. `ObjectStore.open_existing` also avoids constructor initialization and ancestor syncing; its writes may create a shard only beneath an already open, identity-checked objects directory using descriptor-relative `mkdir`. They never call `mkdir(parents=True)` on a registered store path. Explicit store creation must create and sync the objects directory even before the first checkpoint. Recheck source/store/mount identity before checkpoint publication; a disappearance or replacement prevents metadata publication. `publication_hook(connection, checkpoint)` is an internal core integration point, invoked inside the same transaction immediately after checkpoint rows are inserted. Its exception rolls back checkpoint rows and event/result rows together. Existing calls with no hook retain their behavior. Objects are written and verified before this transaction as today.

The hook atomically marks the operation succeeded and appends `checkpoint.created` with `checkpoint_id` and counts. Notify subscribers after the transaction context exits successfully. Do not include message text or arbitrary caller details in events. Operation errors store only approved structured error fields.

`checkpoint.create`, `verify`, and `restore` return accepted operation IDs. `checkpoint.list` and `usage` are read requests. A fixed global worker pool limits resource usage; a per-project FIFO coordinator serializes mutations and runs at most one worker per project. Eight worker slots is the initial configurable ceiling. Busy read requests return `PROJECT_BUSY` rather than holding socket workers indefinitely. Registry mutations use a registry lock; relink/unregister acquire the project coordinator before that lock. Queued/running operations block relink/unregister with `PROJECT_BUSY`; clients retry after completion.

Restore is restricted to a new destination, using the engine's existing refusal to overwrite. Persist the canonical destination before starting. Reject destinations overlapping registered stores or source roots. Startup does not infer restore success from destination existence: any nonterminal restore becomes interrupted; preserve destination and staging evidence for explicit review. Do not automatically retry interrupted restore. Create an interruption event, but never remove the destination. Engine cleanup of its own staging during ordinary error handling retains existing behavior.

Startup converts queued/running core-owned operations to interrupted, with one event in the same project transaction. Succeeded checkpoints retain success because checkpoint and operation outcome share a commit. Inaccessible projects are reported as unavailable and reconciled when explicitly made available; startup must not prevent other projects from operating. External agent tasks are not created or modified by this milestone.

### Commands and event delivery

Use a separate module entry point initially: `python -m solace_core.cli`; install a `solace` executable during the separately authorized install step. Route `solace-backup core/project/checkpoint/operation/doctor` to that parser before legacy dispatch. Support `--json` before or after the subcommand. Existing `solace-backup --status`, `vault`, `backup`, and GUI launch behavior remain as-is.

```bash
solace core status [--json]
solace project add PATH [--store PATH] [--request-id UUID] [--json]
solace project list [--json]
solace project show PROJECT [--json]
solace project relink PROJECT --root PATH --store PATH --confirm-root CANONICAL_PATH [--json]
solace project unregister PROJECT [--json]
solace checkpoint create PROJECT MESSAGE [--exclude PATTERN] [--request-id UUID] [--json]
solace checkpoint list PROJECT [--json]
solace checkpoint verify PROJECT CHECKPOINT [--json]
solace checkpoint restore PROJECT CHECKPOINT --to NEW_PATH [--json]
solace checkpoint usage PROJECT [--json]
solace operation show PROJECT OPERATION [--watch] [--json]
solace project show PROJECT --watch [--after SEQUENCE] [--json]
solace doctor [PROJECT] [--json]
```

For relink, human CLI displays the canonical root and prompts for confirmation; the API requires that exact string as `confirmed_root`. Noninteractive clients supply `--confirm-root`. Store relocation means pointing to an already moved matching store; this milestone does not move/copy data. `project add` refuses any existing nonempty destination, including an unregistered previous store; no automatic adoption.

Exit codes: `0` successful response/accepted operation, `2` invalid arguments/confirmation, `3` unavailable core/project/store, `4` integrity/schema failure, `1` other operation failure. Watch exits `1` if its watched operation fails or is interrupted. Human output shows error code, message, and next step; JSON prints only structured output to stdout.

Subscriptions send an acknowledgement, then durable events strictly after the supplied cursor. Replay in batches of 256 rows; an in-memory condition wakes subscribers after commits. Poll once per second as a fallback. Always reread rows `seq > last_sent` under the current project identity; notifications are hints, not event payloads. This removes the replay/live race. Each subscriber has bounded writes and a 30-second send timeout; a slow subscriber disconnects and can resume. Heartbeats carry the last delivered cursor and do not become durable events. On reconnect, the client's saved cursor is authoritative.

## Task 1: Paths, stable errors, framing, and local placement checks

**Files:** Create `solace_core/__init__.py`, `paths.py`, `errors.py`, `protocol.py`, `storage.py`; create `tests/solace_core/__init__.py`, `test_paths.py`, `test_protocol.py`, `test_storage.py`.

**Interfaces:** Produce `CorePaths`, `CoreError(code, message, next_step, retryable=False)`, `StoragePolicy`, `StorageLocation`, `encode_frame(dict) -> bytes`, and `decode_frame(bytes) -> dict`. Inject mountinfo and sync roots into `StoragePolicy` for deterministic tests.

- [x] Write failing tests for runtime ownership/mode, relative XDG paths, mount escapes/longest prefix, unsupported/read-only/sync storage, and frame bounds. Include this concrete protocol regression:

```python
def test_nonfinite_json_is_rejected(self):
    from solace_core.errors import CoreError
    from solace_core.protocol import decode_frame
    with self.assertRaises(CoreError) as caught:
        decode_frame(b'{"version":1,"params":{"after":NaN}}\n')
    self.assertEqual(caught.exception.code, "INVALID_REQUEST")
```

- [x] Run `python -m unittest discover -s tests/solace_core -p 'test_*.py' -v`; confirm missing-module failure.
- [x] Implement the interfaces and exact path/framing/storage decisions above. Base frame encoding on `json.dumps(..., allow_nan=False, ensure_ascii=True)`. Match mount ancestry with `Path.is_relative_to`, not string prefixes.
- [x] Run those tests again; add the regression that `/data/project-two` does not match `/data/project`, and that `True` is rejected as an event cursor.
- [x] Review `git diff --check`; stage only this task's files and commit `feat: define local core protocol and storage policy` when commit access is available.

## Task 2: Explicit store creation and no-create engine access

**Files:** Modify `solace_engine/repository.py`, `solace_engine/object_store.py`; create `solace_core/catalog.py`, `tests/solace_core/test_catalog.py`; extend `tests/solace_engine/test_repository.py`, `test_object_store.py`.

**Interfaces:** Produce `ProjectCatalog` creation/identity/open routines, `ProjectRepository.open_existing`, and `ObjectStore.open_existing`. Do not route ordinary operations through the initializing engine constructors.

- [x] Write failing tests for opening missing catalogs, missing object directories, store UUID mismatch, newer core/engine schemas, corrupt SQLite, and store path replaced by a symlink. Test the no-create guarantee:

```python
def test_existing_open_does_not_recreate_missing_store(self):
    from solace_engine.repository import ProjectRepository
    missing = self.base / "missing-store"
    with self.assertRaises(Exception):
        ProjectRepository.open_existing(self.project, missing)
    self.assertFalse(missing.exists())
```

Use the specific domain exception once defined; do not leave the broad assertion in the final test.
- [x] Run `python -m unittest discover -s tests/solace_core -p 'test_catalog.py' -v` and the new engine test; confirm failures.
- [x] Split initialization from connection policy; set URI `mode=rw` on existing opens. Add core metadata and an empty objects directory to explicitly created catalogs; use exclusive directory creation and sync new directory entries. Read identity through a read-only connection before mutable engine opening. Implement object shard creation relative to the checked existing objects directory, preserving existing constructor behavior for legacy engine callers.
- [x] Run catalog tests and all 49 engine regressions. Check disappearance after validation too: SQLite open must fail without creating a database, object publication must never recreate the absent store root, and pre-publication identity validation must reject a substituted mount or directory. Exercise disappearance between scan and the first object write and immediately before the checkpoint transaction.
- [x] Review the diff and commit `feat: open registered checkpoint stores without initialization`.

## Task 3: Recoverable registration, relink, and unregister

**Files:** Create `solace_core/registry.py`, `tests/solace_core/test_registry.py`; extend `catalog.py` as required by the registration-intent interface.

**Interfaces:** Produce the five `Registry` methods above and `Registry.reconcile_intents() -> list[dict]`. Availability is derived on access, not a stale persisted boolean.

- [x] Write failing lifecycle tests using a temporary data root and an injected local storage policy. Include duplicate roots through aliases, reused request IDs, nonempty store refusal, and this source-scan guard:

```python
def test_add_does_not_scan_or_checkpoint(self):
    from unittest.mock import patch
    from solace_engine.repository import ProjectRepository
    with patch.object(ProjectRepository, "_scan", side_effect=AssertionError("scan")):
        record = self.registry.add(self.project, None, self.request_id, "test")
    repo = ProjectRepository.open_existing(self.project, Path(record["store"]))
    self.assertEqual(repo.list_checkpoints(), [])
    self.assertEqual(list(self.project.iterdir()), [])
```

- [x] Run `python -m unittest discover -s tests/solace_core -p 'test_registry.py' -v`; confirm failure.
- [x] Implement registry SQL, canonical uniqueness, cached mutation receipts, and the registration intent/create/publish sequence. Unregister deletes only the registration row and stores its receipt; never remove files. Relink verifies the original project/store IDs and the exact confirmed root, then updates the registry atomically.
- [x] Inject interruption after intent commit, after store creation, and after final commit. Reconciliation must expose interruption or finish an identity-matching intent without recapturing files. Verify missing roots/stores and remounted paths never create replacements; an unregister retry returns its original result. Test relink to another project's store and unconfirmed roots are refused.
- [x] Run all core tests, review the diff, and commit `feat: add recoverable local project registration`.

## Task 4: Atomic operation and event publication

**Files:** Extend `solace_core/catalog.py`, `solace_engine/repository.py`; create `tests/solace_core/test_operations.py`, `test_events.py`; extend engine tests.

**Interfaces:** Produce catalog operation/event methods and the optional `publication_hook(connection, checkpoint)` engine keyword. State transitions append an event in the same transaction.

- [x] Write failing tests for idempotent acceptance, conflicting request reuse, cursor bounds, ordered sequences, and a hook rollback regression:

```python
def test_failed_publication_hook_rolls_back_checkpoint(self):
    def fail(connection, checkpoint):
        connection.execute("INSERT INTO hook_probe VALUES (?)", (checkpoint.id,))
        raise RuntimeError("injected publication failure")
    with self.repo._connect() as connection:
        connection.execute("CREATE TABLE hook_probe (id TEXT)")
    with self.assertRaises(RuntimeError):
        self.repo.create_checkpoint("capture", publication_hook=fail)
    self.assertEqual(self.repo.list_checkpoints(), [])
    with self.repo._connect() as connection:
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM hook_probe").fetchone()[0], 0)
```

- [x] Run the new tests and confirm missing keyword/method failures.
- [x] Implement operation acceptance, allowed transition checks, event insertion, and post-commit notifications. The checkpoint hook writes operation success and its checkpoint event through the passed connection, never a second connection. Preserve the `CorruptObject` exception classification.
- [x] Inject failure before/after checkpoint inserts, during hook insertion, before commit, and immediately after commit/before notification. Reopen the store: checkpoint, success state, and event must appear together or not at all. Persisted events must omit checkpoint messages and arbitrary prompt-like fields. Test concurrent event writers yield unique increasing sequence numbers.
- [x] Run core and engine suites, review the diff, and commit `feat: publish checkpoint results and durable events atomically`.

## Task 5: Project coordination, operations, and restart recovery

**Files:** Create `solace_core/service.py`, `coordinator.py`, `tests/solace_core/test_service.py`, `test_recovery.py`.

**Interfaces:** Produce `CoreService.handle/close`, per-project FIFO coordinators, dispatch for all non-subscription API methods, and startup operation reconciliation. Catalog/registry interfaces come from Tasks 2–4.

- [x] Write failing dispatch, unknown-method, missing-store, and parallel-project tests. Use threading events to prove a blocked scan in project A does not prevent project B completing; do not use timing-only sleeps:

```python
entered = threading.Event()
release = threading.Event()
def blocked_scan(exclusions):
    entered.set()
    self.assertTrue(release.wait(5))
    return [], []
# Patch A's repository scan; accept A, wait for entered, accept B,
# wait for B's succeeded event, then release A in a finally block.
```

- [x] Run `python -m unittest discover -s tests/solace_core -p 'test_service.py' -v`; confirm failure.
- [x] Implement bounded workers, durable operation acceptance before scheduling, one mutation at a time per project, checkpoint/create/list/verify/restore/usage dispatch, and explicit `PROJECT_BUSY` responses. Validate identity/placement immediately before work; restore accepts only a new nonoverlapping destination. Use fixed safe details for operation records and errors.
- [x] Add subprocess crash tests using controlled pipe barriers and process termination before checkpoint commit and after commit/before reply. Reopen and reconcile: nonterminal operations become interrupted once; committed checkpoint results stay succeeded. Test restore interruption preserves its destination, duplicate requests return the same ID, and missing stores do not block another project. Relink/unregister must be refused while work is queued or running.
- [x] Run all core and engine tests, review lock ordering for deadlocks, and commit `feat: coordinate core operations and recover interrupted work`.

## Task 6: Same-user Unix server and resumable subscriptions

**Files:** Create `solace_core/server.py`, `client.py`, `__main__.py`, `tests/solace_core/test_socket.py`, `test_subscriptions.py`.

**Interfaces:** Produce `CoreClient`, `serve(socket, service)`, and `python -m solace_core --socket PATH` for isolated development. Production takes its socket from systemd; explicit binding is a test/development mode and never bypasses runtime validation.

- [x] Write failing temporary-Unix-socket tests for round trips, concurrent clients, wrong UID refusal using an injected credential reader, malformed/oversized frames, and disconnect after mutation commit. Test partial reads and multiple frames received in one read.
- [x] Run socket/subscription test discovery and confirm failure.
- [x] Implement bounded frame reading, Linux `SO_PEERCRED` validation before request reads, one request per client at a time, request timeouts, and structured error envelopes. No AF_INET/AF_INET6 sockets. Development binding refuses existing paths; shutdown unlinks only the socket inode created by this process.
- [x] Implement event subscription from durable cursor, batched replay, post-commit wakeups, periodic fallback, and heartbeat frames. Exercise a barrier-controlled commit exactly between replay and waiting; assert all sequences arrive once in order. Disconnect/reconnect with cursor, reject future cursors, terminate on unregister, and prove slow subscribers cannot block checkpoint publication or other clients.
- [x] Run all core tests, inspect socket mode, and commit `feat: serve the core over a private Unix socket`.

## Task 7: CLI, legacy routing, and read-only diagnostics

**Files:** Create `solace_core/cli.py`, `diagnostics.py`, `tests/solace_core/test_cli.py`, `test_diagnostics.py`; modify only the early-routing boundary in `solace_backup_cli.py`.

**Interfaces:** Produce `cli.main(argv: Sequence[str] | None = None) -> int`, `diagnostics.inspect(registration: dict | None) -> dict`, and the command grammar/exit codes above. `core.status` may activate the core; it reports the connected service PID, version, and project availability. `doctor` reports connectivity independently and can inspect metadata read-only when the core is unavailable.

- [x] Write subprocess tests with temporary runtime/data roots and a test server. Include JSON flags before/after commands, unavailable core, canonical source-root confirmation, and a read-only diagnostic regression:

```python
before = {p.relative_to(store): p.read_bytes() for p in store.rglob("*") if p.is_file()}
report = inspect(registration)
after = {p.relative_to(store): p.read_bytes() for p in store.rglob("*") if p.is_file()}
self.assertEqual(before, after)
self.assertFalse(report["ok"])  # fixture contains corrupt database bytes
```

Run this byte comparison against an offline fixture; online WAL changes by the active service are not diagnostic writes.
- [x] Run CLI/diagnostics tests; confirm failures.
- [x] Implement argparse commands, shared JSON errors, method mapping, operation/watch rendering, and exact-root confirmation. Route new command names through this parser before importing legacy GUI/vault code. Print accepted operation IDs and inspection guidance; do not claim the requested capture is finished at acceptance.
- [x] Implement doctor with read-only integrity checks, identity/schema/permissions/mount checks, operation summaries, and unavailable-path reporting. Never initialize schemas, run repairs, or create absent paths. Test a newer schema remains byte-identical and legacy dispatch still calls its original handlers without opening a core store.
- [x] Run all core tests plus the two existing baseline commands, review the diff, and commit `feat: expose core commands and read-only diagnostics`.

## Task 8: systemd activation, packaging, and acceptance evidence

**Files:** Create `packaging/systemd/solace-core.socket`, `solace-core.service`, `packaging/install-core.sh`, `docs/local-core.md`, `tests/solace_core/test_activation.py`; update the engineering follow-up register after actual verification.

**Interfaces:** User units activate the server with one AF_UNIX listening descriptor at FD 3. The installer stages an immutable versioned runtime and substitutes absolute Python/runtime paths into service and launcher templates. It must not point production at a writable development worktree. Existing application install and data are untouched.

- [x] Write failing activation tests: pass an inherited listening FD to a subprocess with `LISTEN_PID` set by its entry shim and `LISTEN_FDS=1`; reject missing/multiple/wrong-family descriptors. Assert the process does not bind a TCP socket or unlink the inherited socket on shutdown.
- [x] Run activation tests; confirm failures.
- [x] Implement FD validation and use the inherited socket directly. Build these unit templates, replacing `@PYTHON@` and `@RUNTIME@` only during installation:

```ini
# solace-core.socket
[Unit]
Description=Solace local core socket
[Socket]
ListenStream=%t/solace/core.sock
SocketMode=0600
DirectoryMode=0700
RemoveOnStop=yes
[Install]
WantedBy=sockets.target
```

```ini
# solace-core.service
[Unit]
Description=Solace local project core
Requires=solace-core.socket
After=solace-core.socket
[Service]
Type=simple
ExecStart=@PYTHON@ -m solace_core
WorkingDirectory=@RUNTIME@
UMask=0077
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
RestrictAddressFamilies=AF_UNIX
```

No service `WantedBy`: enable the socket alone. Do not enable lingering. Test paths with spaces and systemd escaping; reject newlines/control characters in install paths. Write logs to the journal, without request bodies or prompt/message contents.
- [x] Prepare an idempotent installer with a preview mode displaying exact files and commands. Preserve pre-existing unit/launcher files in a timestamped backup before replacement; fail rather than overwrite an unknown runtime directory. Installer output provides rollback commands. Live install/enable requires the user's explicit install authorization and any filesystem escalation; plan implementation does not authorize it.
- [x] Run all core/engine/legacy/Tk tests and `git diff --check`. Run `systemd-analyze --user verify` on substituted test units. Perform the inherited-FD subprocess test without modifying user services. Document live systemd testing as pending unless installation is authorized.
- [x] For authorized live acceptance, enable/start only `solace-core.socket`, verify `0600`, trigger `solace core status`, confirm the service becomes active, and use an isolated temporary project to add/create/watch/verify/unregister. Confirm the store survives unregister, restart recovery works, and the process has only Unix listeners. Restore previous units/launcher on rollback; preserve stores. Report installed versus tested-in-subprocess evidence separately.
- [x] Update `docs/local-core.md` with commands, errors, storage limits, interruption recovery, event cursor rules, no automatic migration, and install/rollback instructions; commit `feat: package systemd activation for the local core`.

## Review and handoff

- [x] Review each task's diff and its targeted tests before proceeding to the next task.
- [x] Recheck all eight spec acceptance criteria against the task evidence; annotate unavailable live installation checks explicitly.
- [x] Whole-branch review focuses on store loss/remounts, atomic events, lost-response retries, concurrent subscriptions, and changes to legacy paths. Use a separate reviewer only if the user selects delegation; otherwise record the inline review and its independence limitation.
- [x] Run both existing test discovery commands and the new core suite. These suites are separate because the existing engine tests are not collected by the top-level command.
- [x] Present the reviewed branch, test totals, limitations, and an exact install preview. Release, merge, and installation remain separate authorized actions.

## Spec coverage check

| Requirement | Deliverable and evidence |
|---|---|
| Same-user local socket, systemd activation, no port | Tasks 1, 6, 8; credentials, modes, descriptor and unit checks |
| Registry lifecycle and configured local stores | Tasks 1–3; identity/mount policy, no-scan add, confirmed relink, preserved unregister |
| Concurrent clients and independent projects | Tasks 5–6; barrier-controlled concurrency tests |
| Atomic changes/events and reconnect replay | Tasks 4, 6; transaction failure injection and replay/live race |
| Durable operations and interrupted recovery | Tasks 4–5; subprocess crashes around commit and restore interruption |
| Stable errors, JSON, diagnostics, no prompt logging | Tasks 1, 6–7; malformed-input, error mapping, read-only and log tests |
| Legacy vault and Git compatibility | Tasks 3, 7–8; no registration scan/source writes and unchanged legacy regressions |
| Packaging/install evidence | Task 8; inherited-FD tests first, live activation after installation authorization |

Implementation was approved for inline execution and completed. Author review and test evidence are recorded in `docs/engineering/2026-10-13-local-core-implementation-review.md`. Independent-review fixes, installation, and live acceptance are recorded in `docs/engineering/2026-10-14-local-core-independent-review-and-acceptance.md`. Local integration was authorized; remote publication and further feature work remain separate.
