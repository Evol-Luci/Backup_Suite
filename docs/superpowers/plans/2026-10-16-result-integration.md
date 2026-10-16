# Manual Result Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply immutable ready results from child to parent and from root task to original project through an explicitly reviewed, recoverable CLI/API workflow.

**Architecture:** Persist immutable B/R/C/M versions and revisioned review plans in core schema 3. Publish approved file actions through a same-filesystem sibling journal, then atomically publish history, lifecycle changes and the receipt in SQLite. Restart classifies incomplete publication and waits for explicit resume or restoration.

**Tech Stack:** Existing Python standard library, unittest, SQLite with FULL synchronization, Linux descriptor-relative filesystem operations, Unix socket protocol v1, existing content-addressed checkpoint engine. No new runtime dependency.

**Spec:** [Approved design](../specs/2026-10-16-result-integration-design.md), approved 2026-10-16. Planning baseline `763a279`; installed runtime `59f3d8340f868d4a` remains unchanged.

**Status:** Written plan awaiting user review and execution-method choice. No task below has been executed. Worktree: `.worktrees/task-integration-design`, branch `codex/task-integration-design`. Do not treat code examples as installed functionality.

## Global Constraints

- “Support child → parent and root task → original project through the CLI/API.” No arbitrary integration destination.
- “The first integration slice is manual and does not execute configured checks.” Record `checks: not_run_by_solace` and require explicit unchecked acceptance.
- “External task lifecycle remains `created/active/blocked/ready/cancelled`.” Result application is a separate relation.
- “Propose core schema **3**, with an explicit preview/apply upgrade and verified consistent backup.” Keep engine and registry schemas at 1.
- “Opening a store must not silently migrate it.” Preserve task capabilities on schema 2; integration requires 3.
- “retain wire version 1 and the 1 MiB complete-frame bound.”
- “Use lowercase 32-hex IDs and 64-hex digest tokens.” Envelope request IDs retain the existing UUID contract.
- “Path pages default to 128 entries, allow 1–256, and have a 128 KiB byte ceiling with a returned continuation cursor pinned to plan revision.”
- “Limit automatic text merging to files at most 1 MiB and 50,000 lines.” Enforce a two-second deadline per isolated merge worker.
- “Mount-root or unwritable-parent targets are refused before changes” and journals remain private siblings on the target filesystem.
- “Restart never resumes file writes or rolls them back automatically.” Never recursively replace the target root or delete displaced files.
- Preserve `.git` at every depth, `.solace_vault`, exclusions and all known Solace-owned paths. Home is not a Git repository merely because it has an empty `.git` directory.
- No new automatic policy, check runner, forward undo, GUI, rename heuristics, locks for external editors, deeper task nesting, GC, replication or Git synchronization. These remain in the [follow-up register](../../engineering/2026-10-09-review-findings-and-follow-ups.md#proposed-integration-follow-ups-2026-10-16).
- Work only on disposable projects during verification. No user-project application, live schema migration, runtime installation, remote push or release follows from executing this source plan.

## Review Focus

1. A sibling journal can lie inside a registered ancestor project: refuse that placement without creating files (Task 5).
2. A crash can occur after rename but before progress commit: classify actual old/new identities and preserve every ambiguous entry (Tasks 6 and 11).
3. A valid relative symlink can become unsafe after conflict resolution: revalidate the entire candidate link graph, including unchanged links (Tasks 2 and 4).
4. Schema-2 upgrade receipts may be replayed after schema 3 exists: replay the original result before inspecting current upgrade eligibility (Task 1).
5. A single enormous diff or escaped filename can exceed the socket frame: paginate actual encoded bytes without splitting a path record or truncating the durable receipt (Tasks 4 and 9).

## File and interface map

Existing boundaries matter: `TaskCatalog` owns revisioned task records and synchronous replay; `ProjectCatalog` owns operations/events; `ProjectCoordinator` serializes project work but does not currently persist source-root claims. `ProjectRepository._insert_checkpoint` can insert an already verified manifest into the same SQLite transaction. `WorkspacePaths` owns placement checks; its normal workspace policy must never be relaxed to apply to an original source.

Create these focused modules, each with its named owner tests:

| Module | Responsibility | Tests |
| --- | --- | --- |
| `solace_core/integration_types.py` | Immutable typed records and state/error constants | `test_integration_schema.py` |
| `solace_core/integration_schema.py` | Schema-3 DDL only | `test_integration_schema.py` |
| `solace_core/integration_merge.py` | Pure entry/line merge and bounded worker | `test_integration_merge.py` |
| `solace_core/integration_snapshots.py` | Verified immutable checkpoint views, bytes and candidate insertion | `test_integration_prepare.py` |
| `solace_core/integration_plans.py` | Contribution baseline, target eligibility, prepare and draft persistence | `test_integration_prepare.py` |
| `solace_core/integration_review.py` | Resolution, review token, bounded metadata/diff, separate export | `test_integration_review.py` |
| `solace_core/integration_claims.py` | Durable affected-tree/journal ownership and guard adapter | `test_integration_claims.py` |
| `solace_core/integration_publish.py` | Descriptor-held per-file journal actions | `test_integration_publish.py` |
| `solace_core/integration_commit.py` | Single-transaction history, lifecycle and immutable receipts | `test_integration_commit.py` |
| `solace_core/integration_recovery.py` | Restart classification, resume, restore-before, location handoff | `test_integration_recovery.py` |
| `solace_core/integration_operations.py` | Operation acceptance, scheduling and dispatch facade | `test_integration_protocol.py` |
| `solace_core/integration_protocol.py` | Strict method parameter validation | `test_integration_protocol.py` |
| `solace_core/integration_cli.py` | CLI grammar and pinned request construction | `test_integration_cli.py` |
| `tests/solace_core/integration_fixture.py` | Disposable ready-task and plan helpers | Used by integration tests |
| `tests/solace_core/integration_crash_worker.py` | Subprocess-only failpoint injection | `test_integration_crashes.py` |
| `tests/solace_core/test_integration_acceptance.py` | Real socket/CLI round trip | Acceptance |
| `docs/result-integration.md` | User workflow, errors, recovery and limitations | Link/command checks |

Modify existing `schema.py`, `catalog.py`, `tasks.py`, `task_operations.py`, `workspace_paths.py`, `service.py`, `protocol.py`, `cli.py`, `observations.py`, `diagnostics.py`, `maintenance.py` and existing schema-sensitive tests only where the task below names the boundary. Keep `solace_engine` data format unchanged; core adapters call the existing verified object/checkpoint interfaces.

### Shared value contract

Task 1 defines these records; later tasks must use these names. `FileEntry` is the existing frozen engine record. `path` is a slash-separated engine-relative pathname, not a host path. Absent entries are `None`. A `Version` is a verified checkpoint plus its exact profile; manifest digests use canonical JSON of sorted entry metadata and link targets, never checkpoint IDs or timestamps.

```python
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
    identity: Identity
    target: Identity
```

Do not use these dataclasses as public JSON directly: explicit projections omit private identities, full manifests, prompt content and journal internals.

### API contract fixed before implementation

All rows below additionally require `project`. IDs are exact lowercase UUID hex; `revision` and `expected_revision` are positive integers rejecting booleans; `token/approval` are exact SHA256 hex. Request fingerprints cover the complete validated method/params/actor. “Async” returns only `operation_id`; `operation.show` returns bounded outcome summaries.

| Method | Required params beyond project | Optional params | Result / owner |
| --- | --- | --- | --- |
| `integration.prepare` | `task, result, expected_revision` | `expected_target_revision` (required for child; forbidden for root) | Async; plan summary / plans |
| `integration.show` | `plan` | none | Bounded summary, last validation time, review token when prepared / review |
| `integration.diff` | `plan` | `revision, after, limit, path, version, offset` | Metadata page or explicit file-content page / review |
| `integration.resolve` | `plan, revision, path` | Exactly one of `take` or `from_file` | New revision/summary / review |
| `integration.export` | `plan, revision, destination` | none | Async; retained export record / review |
| `integration.approve` | `plan, revision, token, accept_unchecked` | none | Approval token and exact revision/digest / claims |
| `integration.apply` | `plan, revision, approval, writers_paused` | none | Async; receipt summary / operations |
| `integration.receipt` | `receipt` | `after, limit` | Immutable summary and decision page / commit |
| `integration.abandon` | `plan, revision` | none | Retained terminal plan / plans |
| `integration.recovery.show` | `operation` | `after, limit` | Bounded journal/action status / recovery |
| `integration.recovery.resume` | `operation, writers_paused` | none | Async; links original operation / recovery |
| `integration.recovery.restore-before` | `operation, writers_paused` | none | Async; restoration receipt / recovery |

Both recovery mutations additionally allow `target_path, journal_path, confirmed_target, confirmed_journal` as one all-or-none relocation group. They are absolute paths; the two confirmations must exactly equal the supplied paths. A source target must equal the already explicitly relinked registered root. A workspace target must retain its logical workspace/task owner and is adopted only by the durable recovery handoff described in Task 8. These fields relocate a recorded interrupted publication; they cannot select an unrelated application destination or change B/R/C/M. Normal recovery omits all four fields.

`take` accepts `current/incoming/delete`. `path` permits engine-relative names including literal backslashes, rejects absolute names, NUL, empty, dot and dot-dot components. `from_file/destination` are absolute paths and traverse without symlinks. A directory conflict selection applies one complete subtree and removes colliding candidate prefixes in one revision.

Metadata paging: `after` is a nonnegative integer, default 0; `limit` defaults 128, maximum 256. Subsequent pages require `revision` pinned to the first page. The response is `{revision, entries, total, next_after}` plus bounded summary fields. Never advance a cursor on an empty page.

Content mode: `integration.diff` with `path` requires `revision` and `version=base/current/incoming/candidate`; `offset` is a nonnegative byte offset. Return at most 48 KiB raw bytes per page as base64 with `next_offset`, exact size, digest and entry metadata. It is a version-byte view, not an unbounded unified patch. Metadata pages never contain file bytes. Mixing content and metadata cursors is invalid.

Show/operation summaries use at most 48 KiB; complete path-page objects use at most 128 KiB after JSON escaping and base64 encoding. One oversized metadata row returns `RESPONSE_TOO_LARGE` with export guidance. Full immutable data stays in SQLite/CAS; no truncation of receipts at rest.

### Schema-3 persistence contract

Keep schema-2 tables, states and contexts. Add the following tables using `CHECK`, `FOREIGN KEY` and non-null constraints. JSON columns hold canonical JSON. `profile_json` is the incoming task's exact profile; `expected_json` pins physical identities, ownership boundaries, directory inventories and source/target revisions.

```sql
CREATE TABLE integration_plans (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 result_id TEXT NOT NULL REFERENCES checkpoints(id), target_key TEXT NOT NULL,
 target_task_id TEXT REFERENCES tasks(id),
 state TEXT NOT NULL CHECK(state IN
 ('conflicted','prepared','approved','applying','applied','recovery_required','restored','abandoned')),
 current_revision INTEGER NOT NULL CHECK(current_revision>=1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE integration_revisions (
 plan_id TEXT NOT NULL REFERENCES integration_plans(id), revision INTEGER NOT NULL CHECK(revision>=1),
 base_id TEXT NOT NULL REFERENCES checkpoints(id),
 incoming_id TEXT NOT NULL REFERENCES checkpoints(id),
 current_id TEXT NOT NULL REFERENCES checkpoints(id),
 candidate_id TEXT NOT NULL REFERENCES checkpoints(id),
 previous_receipt_id TEXT REFERENCES integration_receipts(id),
 profile_json TEXT NOT NULL, expected_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 review_token TEXT NOT NULL, validated_at TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(plan_id,revision)
);
CREATE TABLE integration_decisions (
 plan_id TEXT NOT NULL, revision INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 path TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT, decision_json TEXT NOT NULL,
 PRIMARY KEY(plan_id,revision,ordinal), UNIQUE(plan_id,revision,path),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_approvals (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 token_sha256 TEXT NOT NULL UNIQUE, binding_json TEXT NOT NULL,
 actor TEXT NOT NULL, accepted_unchecked INTEGER NOT NULL CHECK(accepted_unchecked=1),
 created_at TEXT NOT NULL,
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_claims (
 path TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
 kind TEXT NOT NULL CHECK(kind IN ('target','journal','export')),
 identity_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE integration_journals (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id), plan_id TEXT NOT NULL,
 revision INTEGER NOT NULL, journal_path TEXT NOT NULL UNIQUE, identity_json TEXT,
 target_identity_json TEXT NOT NULL, intent_json TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN
 ('reserved','staged','applying','files_ready','committed','recovery_required','restored','abandoned')),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_actions (
 operation_id TEXT NOT NULL REFERENCES integration_journals(operation_id),
 ordinal INTEGER NOT NULL, path TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('mkdir','move_old','publish_new','rmdir')),
 phase TEXT NOT NULL CHECK(phase IN ('planned','intent','done')),
 old_json TEXT, new_json TEXT, slot TEXT, observed_json TEXT,
 PRIMARY KEY(operation_id,ordinal)
);
CREATE TABLE integration_receipts (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id),
 result_id TEXT NOT NULL REFERENCES checkpoints(id), target_key TEXT NOT NULL,
 operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id),
 before_id TEXT NOT NULL REFERENCES checkpoints(id),
 after_id TEXT NOT NULL REFERENCES checkpoints(id),
 details_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(task_id,result_id,target_key),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE TABLE integration_recoveries (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 original_operation_id TEXT NOT NULL REFERENCES integration_journals(operation_id),
 mode TEXT NOT NULL CHECK(mode IN ('resume','restore-before')),
 result_json TEXT, location_json TEXT NOT NULL
);
CREATE TABLE integration_refs (
 checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id), owner_id TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('base','incoming','current','candidate','before','after','export')),
 created_at TEXT NOT NULL, PRIMARY KEY(checkpoint_id,owner_id,role)
);
CREATE TABLE integration_exports (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
 path TEXT NOT NULL UNIQUE, identity_json TEXT,
 phase TEXT NOT NULL CHECK(phase IN ('reserved','staged','published','interrupted')),
 FOREIGN KEY(plan_id,revision) REFERENCES integration_revisions(plan_id,revision)
);
CREATE INDEX integration_receipts_target ON integration_receipts(task_id,target_key,created_at);
CREATE INDEX integration_actions_phase ON integration_actions(operation_id,phase);
CREATE INDEX integration_claims_operation ON integration_claims(operation_id);
```

SQLite permits a forward reference to `integration_receipts` in the revisions table. Execute DDL statement-by-statement inside the caller's transaction, not `executescript` (which can commit implicitly). Retain draft revisions, approvals, refs and completed journal reservations; only active claim rows are removed after successful apply/restore/eligible abandonment. Completed journal paths remain protected by retained catalog records.

Add SQLite BEFORE UPDATE/DELETE rejection triggers on `integration_receipts`:

```sql
CREATE TRIGGER integration_receipts_no_update BEFORE UPDATE ON integration_receipts
BEGIN SELECT RAISE(ABORT,'integration receipts are immutable'); END;
CREATE TRIGGER integration_receipts_no_delete BEFORE DELETE ON integration_receipts
BEGIN SELECT RAISE(ABORT,'integration receipts are immutable'); END;
```

Resolution appends new revision/decision rows; it never edits an earlier revision. The raw approval token is returned in the body-free private request receipt for exact replay; only its hash is stored in the approvals table. Approval validity also requires the plan's current revision/state, not merely token existence. Trigger bodies contain semicolons: execute each complete trigger as one statement, not by splitting its body on semicolons.

---

### Task 1: Schema 3, compatibility and shared records

**Files:** Create `solace_core/integration_types.py`, `solace_core/integration_schema.py`, `tests/solace_core/test_integration_schema.py`; modify `solace_core/schema.py`, `catalog.py`, `maintenance.py`, `diagnostics.py`, `workspace_paths.py`, `service.py`, `packaging/install-core.py`, `tests/solace_core/test_activation.py` and schema-sensitive existing tests.

**Interfaces:** Define the shared records above and `create_schema_v3(db: sqlite3.Connection) -> None`. Preserve `create_schema_v2` as a frozen construction primitive; `require_tasks` accepts supported versions 2/3. Add `require_integrations(catalog) -> None` (requires exactly 3). `preview_upgrade/apply_upgrade` retain their signatures but target 3 for new requests.

- [ ] **Step 1: Add independent frozen schema-2 fixtures and failing migration tests.** Build frozen v2 by running the baseline schema-2 DDL, not the new default catalog constructor. Preserve a literal historical upgrade receipt with its original fingerprint. Add this assertion to a test that starts from frozen 2 and runs the new upgrade:

```python
def test_v2_to_v3_keeps_original_upgrade_receipt(self):
    from solace_core.schema import apply_upgrade, preview_upgrade, schema_version
    old_request, old_result = self.seed_historical_v2_upgrade_receipt()
    preview = preview_upgrade(self.catalog)
    request = self.request()
    request["params"]["plan_token"] = preview["plan_token"]
    result = apply_upgrade(self.catalog, preview["plan_token"], request)
    self.assertEqual(schema_version(self.catalog), 3)
    self.assertEqual(apply_upgrade(self.catalog, old_request["params"]["plan_token"],
                                  old_request), old_result)
    with readonly_database(Path(result["backup"])) as db:
        self.assertEqual(db.execute(
            "SELECT schema_version FROM core_metadata").fetchone()[0], 2)
```

The new schema test class reuses `test_catalog.CatalogTests.setUp` and `test_task_schema.SchemaTests.request`, then constructs its frozen-v2 `self.catalog` explicitly. `seed_historical_v2_upgrade_receipt` is a test-local helper: insert `task_requests` with a new UUID, `fingerprint("project.upgrade", params, "test")` and `{"schema_version":2,"backup":"/historical/backup"}`. `params` contains `project, preview=False, plan_token="a"*64`. It must not call the new upgrader.

Add tests for direct 1→3 with two existing checkpoints, 2→3 without changing heads/prompts, new stores at 3, schema-2 tasks working without an upgrade, preview making no writes, failed DDL rolling back, consistent private backup, stale preview, and original v2 runtime refusing a v3 catalog. Freeze the old validator in a subprocess fixture from baseline `6af0b2c`; never downgrade the live service. Use an exported fixture source, not a dependency on a deletable worktree.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_schema.py' -v`. Expect schema 2/new integration tables absent. Existing task tests are the parity reference.

- [ ] **Step 3: Implement the DDL and records from the contracts.** Generalize upgrade snapshot to include current schema, target schema and both DDL digests; for schema 2 use explicit `source_head` rather than global latest. Lookup original request in operations/task_requests before preview, eligibility, root availability or backup creation. Reject reused IDs across both namespaces. Backup using SQLite backup API, verify exact original version/integrity, fsync file and receiving directories, then perform all 1→2 transformations if required plus 2→3 DDL in one transaction.

```python
def require_integrations(catalog):
    if schema_version(catalog) != 3:
        raise CoreError("PROJECT_UPGRADE_REQUIRED",
                        "This project needs the integration catalog upgrade.",
                        "Review project upgrade --preview before applying its token.")
```

Replace exact-version-2 feature guards with `version in (2,3)` where they mean task support, including recovery, request lookup, lineage, ownership, maintenance and diagnostics. Keep `version == 2` only where it means an actual migration input. Update creation defaults and intentional “new store” assertions to 3; preserve explicit v2 tests rather than removing version-compatibility assertions. Update installer preview's currently hard-coded supported/created schema fields to `[1,2,3]`/`3` and assert them in the existing isolated installer test. Do not advertise `integrations.v1` until Task 9 wires every method.

- [ ] **Step 4: Run GREEN and schema regression.** Run the new file, `test_task_schema.py`, `test_catalog.py`, `test_task_maintenance.py` and `test_task_lineage.py` separately with unittest discovery. Verify `PRAGMA foreign_key_check` empty and engine/registry versions 1. Inject backup verification and DDL failures; no partial v3 tables or duplicate events may remain.
- [ ] **Step 5: Commit.** Stage only the named files; `git commit -m "feat(core): add explicit integration catalog upgrade"`.

### Task 2: Conservative immutable merge with enforced bounds

**Files:** Create `solace_core/integration_merge.py`, `tests/solace_core/test_integration_merge.py`. Reuse `workspace_trees.validate_workspace_links` without weakening it.

**Interfaces:** `merge_text(base: bytes, current: bytes, incoming: bytes) -> tuple[bytes | None, str | None]` is pure and runs in a worker. `merge_entries(base: tuple[FileEntry,...], current: tuple[FileEntry,...], incoming: tuple[FileEntry,...], *, read_bytes: Callable[[FileEntry],bytes], write_bytes: Callable[[str,bytes,int],FileEntry]) -> MergeOutcome` orchestrates bounded workers and complete-tree validation. No checkpoint or filesystem publication here.

- [ ] **Step 1: Add failing byte-level tests.**

```python
def test_disjoint_crlf_preserves_bytes(self):
    from solace_core.integration_merge import merge_text
    merged, reason = merge_text(b"a\r\nb\r\nc", b"A\r\nb\r\nc", b"a\r\nb\r\nC")
    self.assertEqual((merged, reason), (b"A\r\nb\r\nC", None))

def test_same_position_different_insertions_conflict(self):
    from solace_core.integration_merge import merge_text
    merged, reason = merge_text(b"a\n", b"x\na\n", b"y\na\n")
    self.assertIsNone(merged)
    self.assertEqual(reason, "overlapping_text")
```

Add subtests for equality fast paths, disjoint deletion, adjacent replacement boundaries, EOF insertion, duplicate identical hunk, invalid UTF-8/NUL, 1 MiB/50,000-line exact limits and one-over limits. Manifest tests cover independent mode/content changes, incompatible kind/mode changes, link text, delete/modify, binaries, `a` vs `a/b` and unchanged links becoming unsafe after another path changes.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_merge.py' -v`. Expect missing module.
- [ ] **Step 3: Implement strict byte-preserving hunk merge.** Use `splitlines(keepends=True)` after strict UTF-8/NUL checks; retain original bytes in line arrays. Derive base-relative changes using `difflib.SequenceMatcher(..., autojunk=False)`. Same-range equal replacement coalesces; overlapping ranges or different insertions at the same position conflict. Define boundary insertions conservatively: insertion touching a replacement start/end conflicts unless equality already settled it. Apply accepted hunks in reverse base-offset order.

```python
def scalar_merge(base, current, incoming):
    if current == incoming or incoming == base:
        return current, False
    if current == base:
        return incoming, False
    return None, True
```

Use this equality rule per versioned attribute; merge content only when regular-file kinds are compatible. Build unresolved candidates with the current subtree retained and explicit conflicts, never conflict-marker files. Validate candidate prefix uniqueness and complete link graph before returning. Store generated text through `write_bytes`; incoming/current unchanged entries reuse existing immutable objects.

Run each difficult text calculation in a bounded dedicated process via `multiprocessing.get_context("spawn")`; pass at most three bounded byte values, no file/store handles. Parent waits at most two seconds, terminates and joins on timeout, and closes both pipe ends. Timeout/malformed worker output/worker crash yields a named whole-file conflict. Limit simultaneously live merge processes with a semaphore; waiting must not monopolize unrelated service work.

- [ ] **Step 4: Run GREEN.** Include a worker that deliberately never replies; assert it is terminated, another project responds, and no worker remains. Test malformed output and exact threshold boundaries. Do not make correctness depend on timing a naturally difficult input.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): merge immutable result manifests conservatively"`.

### Task 3: Prepare durable plans against exact contribution ancestry

**Files:** Create `solace_core/integration_snapshots.py`, `integration_plans.py`, `tests/solace_core/integration_fixture.py`, `test_integration_prepare.py`.

**Interfaces:** `SnapshotStore(catalog)` exposes `version(checkpoint_id: str, profile: dict) -> Version`, `entries(version: Version) -> tuple[FileEntry,...]`, `read_bytes(entry: FileEntry) -> bytes`, `write_bytes(path: str, data: bytes, mode: int) -> FileEntry` and `insert(db, entries: tuple[FileEntry,...], *, parent_id: str | None, profile: dict, task_id: str, purpose: str, published: bool=False) -> Version`. `IntegrationPlans(service,catalog)` exposes `prepare(operation_id: str, request: dict) -> PreparedPlan`, `load(plan_id: str, revision: int | None=None) -> PreparedPlan`, `target(db, task_id: str) -> Target`, `baseline(db, task_id: str, result_id: str, target_key: str) -> tuple[str,str | None]` and `abandon(request: dict) -> dict`.

`insert` verifies object existence, chunk concatenation size/digest, metadata and link graph, inserts an engine Checkpoint with an explicit parent, adds an unpublished existing-purpose context (`source` or `workspace`), and returns its Version. It does not advance any head. `read_bytes` verifies every chunk and whole-file digest. Opening immutable objects must not require the source workspace/root to exist.

- [ ] **Step 1: Add a disposable integration fixture based on `task_fixture.TaskCase`.** This concrete helper creates a ready task without integration API coupling:

```python
from pathlib import Path
from task_fixture import TaskCase

class IntegrationCase(TaskCase):
    def ready_task(self, changes, *, parent=None):
        task = self.create_task(**({"parent": parent} if parent else {}))
        started = self.start(task)
        root = Path(started["path"])
        for name, data in changes.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        done = self.wait_operation(self.mutate("task.ready", task)["operation_id"])
        self.assertEqual(done["state"], "succeeded", done)
        return task, done["result"]["checkpoint_id"], root
```

Add test-local `prepare_direct(task,result)`: construct `integration.prepare` request with the current task revision and, for a child, current parent revision; accept it with `catalog.accept_operation`, mark running, then call `IntegrationPlans.prepare`. This avoids exposing incomplete public methods before Task 9.

Add a test with source `a\nb\nc\n`, incoming `A\nb\nc\n`, destination edited to `a\nb\nC\n`. Assert C/M bytes respectively current and merged, plan prepared, destination unchanged and published head unchanged. Also test child target, missing mutable source workspace with immutable R still available, duplicate historical application, no descendant relation, and exact profile mismatch.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_prepare.py' -v`.
- [ ] **Step 3: Implement eligibility, capture and persistence.** Source task must be latest-ready at exact revision; child target must match its recorded parent and pinned revision. For root target, every child must be cancelled or latest-ready applied to that parent. Determine initial B from workspace base; later B from latest same-task/same-target applied result ancestral to R. Reject divergent ancestry even when an older matching ancestor exists beyond a divergent latest contribution. Check for an existing same-result receipt before availability checks.

Capture C with exact exclusions and `protected_excluder` plus all known owned roots; freeze relevant store/root/ancestor identities and directory inventories at replacement prefixes. Capture is an unpublished context with explicit old-head parent. Any scan warning for an in-scope path prevents preparing an apparently complete candidate. Release short capture/task claims on successful prepare; persist failures before releasing, preserving unresolved ownership when necessary.

```python
# Inside the plan publication transaction, after input revalidation:
revision = 1
state = "conflicted" if outcome.conflicts else "prepared"
db.execute(
    "INSERT INTO integration_plans VALUES (?,?,?,?,?,?,?,?,?)",
    (plan_id, task_id, result_id, target.key, target.task_id,
     state, revision, now, now))
```

Persist the full immutable revision, sorted decisions/conflicts, bounded operation result and all B/R/C/M refs atomically; no published head changes. Candidate M has parent C. Retain profile-bound before/after inputs and previous contribution receipt. Abandon only a pre-apply draft; if a journal exists, verify no file action occurred before releasing claims and retain the journal reservation.

- [ ] **Step 4: Run GREEN.** Verify source/target heads and file bytes before/after preparation, then inject failure between C insertion and plan publication. No partial plan or published context may survive; unreachable CAS bytes are allowed. Test stored source-head semantics when task checkpoints are newer globally.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): prepare exact-baseline integration plans"`.

### Task 4: Inspect, resolve and export immutable review versions

**Files:** Create `solace_core/integration_review.py`, `tests/solace_core/test_integration_review.py`; extend `integration_fixture.py`.

**Interfaces:** `IntegrationReview(service,catalog)` exposes `show(plan_id: str) -> dict`, `diff(params: dict) -> dict`, `resolve(request: dict) -> dict` and `export(operation_id: str, request: dict) -> dict`. `review_token(plan: PreparedPlan, expected: dict) -> str` hashes the exact sorted version/target binding. `show` reports last validation time, counts and state, not live freshness. `resolve` appends a revision and invalidates prior approvals.

- [ ] **Step 1: Add failing resolution/paging tests.** With one binary conflict, select incoming at revision 1; assert revision 2, exact incoming bytes, unchanged destination and retained C/R. Resolve a prefix conflict by selecting a complete subtree; assert no `a`/`a/b` collision. Test stale revision, absent conflict, invalid deletion, unsafe candidate links, nofollow edited-file import, source file changing during import, and literal-backslash path handling.

```python
def test_editing_export_does_not_change_candidate(self):
    plan = self.make_binary_conflict()
    before = plan.candidate.manifest_sha256
    destination = self.base / "review"
    self.export_direct(plan, destination)
    (destination / "incoming" / "data.bin").write_bytes(b"external edit")
    self.assertEqual(self.plans.load(plan.id).candidate.manifest_sha256, before)
```

Define `make_binary_conflict` in the fixture using Task 3 direct preparation: baseline `b"\0base"`, incoming `b"\0incoming"`, destination `b"\0current"` at `data.bin`. `export_direct` accepts/runs an export operation with pinned plan revision and destination.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_review.py' -v`.
- [ ] **Step 3: Implement byte-bounded projections and immutable decisions.** Do not deserialize a complete manifest into a socket result. Query sorted decision rows with a cursor; measure the complete escaped response while filling it. Enforce the 128 KiB ceiling on the complete page, and the 48 KiB summary ceiling. Subsequent page requests must pin revision; changed revision returns `INTEGRATION_PLAN_STALE`. Content pages use the explicit version/offset contract, base64-encode up to 48 KiB raw bytes, and return no guessed text encoding.

```python
def within_page_budget(value):
    from solace_core.observations import encoded_size
    return encoded_size(value) <= 128 * 1024
```

For edited-file resolution, traverse absolute ancestors nofollow, require a regular leaf, compare open/fstat/lstat identity, size and timestamps before/after streaming chunks, verify whole-file bytes, then import CAS entries. Reject directories/FIFOs/device nodes; do not alter the imported file or any other hard link. A previously conflicted path may be explicitly re-resolved at the current revision, including after approval; a never-conflicted unrelated path is refused. Applied/restored/abandoned plans cannot be resolved. Apply subtree selections as one complete manifest revision, revalidate every symlink, recompute actions/directory inventory as needed, and append new refs/token/decisions in one transaction.

Export B/C/R/M to `base/current/incoming/candidate` under a fresh private staging sibling of the requested destination. Enforce existing ownership and storage policy, reserve it against concurrent registration/adoption, fsync files and receiving directories, and publish with no-replace rename. Retain export intent across crashes; do not auto-resume export or delete interrupted preparations. Exports contain only captured files, never prompts or implicit check results. All four subtrees are fresh and have independently validated safe links.

- [ ] **Step 4: Run GREEN.** Include 10,000 decisions with long JSON-escaped names and a content object exceeding 1 MiB; reassemble content pages and verify digest. A single huge metadata row yields a structured error without destroying the plan. Test export destination creation race and crash before/after export rename; retained metadata allows inspection and blocks adoption.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): review and resolve retained integration candidates"`.

### Task 5: Bind approval and guard all affected live-tree access

**Files:** Create `solace_core/integration_claims.py`, `tests/solace_core/test_integration_claims.py`; modify `service.py`, `workspace_paths.py`, `tasks.py`, `task_operations.py`, `catalog.py`.

**Interfaces:** `IntegrationClaims(service,catalog)` exposes `approve(request: dict) -> dict`, `reserve(db, operation_id: str, plan: PreparedPlan, *, kind: str="target") -> Journal`, `assert_access(path: Path, *, operation_id: str | None=None) -> None`, `release(db, operation_id: str) -> None` and `validate(plan: PreparedPlan, *, scan: bool) -> dict`. `validate` returns the exact persisted binding after checking revision/head/identities/profile/ownership and, when `scan=True`, current bytes. Changed input raises `INTEGRATION_PLAN_STALE`; it never recomputes M.

- [ ] **Step 1: Add failing approval and ownership tests.** Approve revision 1 with the review token and `accept_unchecked=True`; resolve afterward and assert the old approval cannot apply. Missing/false unchecked acknowledgement must return `INTEGRATION_UNCHECKED`. Changed destination bytes, mode, head, ancestor identity, parent/task revision, newly protected root or latest result invalidates approval/application.

Add two registered roots with an ancestor/descendant relationship. Root integration refuses overlapping registered source roots. Separately place a nonoverlapping source with a parent directory that lies inside another registered source; refusal must precede journal creation. Simulate an unavailable retained catalog; placement fails closed. Cover empty home `.git` versus a real project Git marker without modifying the real home directory.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_claims.py' -v`.
- [ ] **Step 3: Implement exact approval and durable reservations.** Under a service-level placement RLock, acquire only the affected project's coordinator lock, perform the short catalog transaction and release both before long I/O. Never acquire another project's coordinator while holding the placement lock; inspect other catalogs read-only. Every source/store registration, project/workspace relink, workspace/cache/export reservation and integration reservation must take this same placement lock before its per-project lock. This prevents cross-project check-then-register races without blocking unrelated filesystem work.

Scan `known_registrations()`, including retained catalogs, for active claims and retained journal/export paths. No registry schema change is needed. Extend `WorkspacePaths.managed_locations` for schema 3; completed journals remain protected. Refuse journal placement if it is in/above a registered source, store, cache, workspace, existing journal or configured synchronized root; a sibling journal may share the target's workspace parent but cannot overlap the target or other managed entries. Validate local storage, writable parent, matching mount/device and target/ancestor identities before reserving `.solace-integration-<operation-id>`. Record the reservation before creating it; replay can distinguish reserved-only intent.

Approval stores a canonical binding of B/R/C/M, revision, previous contribution receipt, task/target revisions, target/head/profile and identities; return a fresh `secrets.token_hex(32)` with its SHA256 in the approval table. Store a body-free request receipt for exact token replay. Resolution returns plan state to prepared/conflicted, so old approvals remain historical and invalid.

Use task claims for source/parent task revisions and integration claims for actual live-tree paths. Guard affected source `checkpoint.create` and task start/capture/status-refresh, workspace relink/checkpoint/ready, integration prepare/export as applicable, restore destinations, registration and unregister. A previously accepted queued operation must recheck claims at worker entry, not only acceptance. Immutable checkpoint verification/read and metadata inspection can proceed; new reads of an applying live tree cannot. Task inspection returns `availability="recovery_required"` or `"applying"` and `path=None` while claimed; no full-tree hash on ordinary show.

```python
def overlaps(left, right):
    left, right = Path(left), Path(right)
    return left.is_relative_to(right) or right.is_relative_to(left)

# Every access path is checked against persisted claims, including after restart.
# Only the owning operation may pass its operation_id.
for claim in claims:
    if overlaps(candidate, Path(claim["path"])) and claim["operation_id"] != operation_id:
        raise CoreError("INTEGRATION_TARGET_BUSY",
                        "An integration still owns this tree.",
                        "Inspect integration recovery before using its files.", True)
```

- [ ] **Step 4: Run GREEN.** Pause an apply after reservation and test source checkpoint, task start, workspace refresh/relink, unregister and conflicting project registration are refused. Verify a different workspace/project completes, immutable receipt lookup works and no callback deadlocks. Attempt registration while another thread reserves a journal; exactly one may win.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): bind manual approval and integration ownership"`.

### Task 6: Descriptor-held file publication with durable displaced versions

**Files:** Create `solace_core/integration_publish.py`, `tests/solace_core/test_integration_publish.py`. Reuse `workspace_trees._rename_noreplace` (Linux no-replace rename) and existing storage/descriptor helpers without using whole-root restore.

**Interfaces:** `IntegrationPublisher(service,catalog, *, failpoint: Callable[[str],None] | None=None)` exposes `stage(operation_id: str, plan: PreparedPlan) -> Journal`, `publish(journal: Journal) -> None`, `verify_final(journal: Journal) -> None`. A failpoint is an injected test callable, never a request parameter, environment-controlled production feature or public policy bypass. `publish` stops with retained claims/evidence on any uncertain filesystem change.

- [ ] **Step 1: Add failing publication tests.** Preserve the destination root inode and unchanged file inode, Git/vault bytes, excluded descendants and unrelated directories. Apply regular-file addition/replacement/deletion, mode-only replacement and safe link replacement. Assert the old version remains in a private journal slot.

```python
def test_foreign_entry_is_not_overwritten(self):
    plan, journal = self.staged_replacement()
    def interleave(name):
        if name == "old_moved:0":
            (self.root / "file").write_bytes(b"foreign winner")
    self.publisher.failpoint = interleave
    with self.assertRaises(CoreError):
        self.publisher.publish(journal)
    self.assertEqual((self.root / "file").read_bytes(), b"foreign winner")
    self.assertTrue(self.displaced_version_exists(journal, b"original"))
```

Define `staged_replacement` locally by readying `file` with incoming bytes, editing destination to the prepared C, approving/reserving through Task 5, then staging. `displaced_version_exists` inspects only recorded journal slots using nofollow descriptors and verifies bytes/digest; it must not glob/follow untrusted names.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_publish.py' -v`.
- [ ] **Step 3: Implement the action state machine.** Open target, parent, journal and relevant ancestors through descriptor-held nofollow traversal; compare name→descriptor identities before each mutation and after validation. Journal is private mode 0700 with a canonical durable intent and private `new/old` slot directories. Stage regular bytes into exclusive private files, verify CAS content/digest/mode, fsync, then stage symlinks without following them. Never modify the old entry in place.

Ordered actions: remove/move obsolete leaf entries deepest-first; remove only explicitly inventoried empty directories deepest-first; create needed directories shallowest-first; publish new leaves in sorted order. Directory-to-file replacement requires the complete auxiliary inventory to match and explicit children to be moved; `rmdir` is allowed only for the matching now-empty directory. No recursive deletion. Newly created directory modes are 0700; existing directory metadata stays untouched.

For each action: persist `intent` with exact before/after names and identities, fsync relevant intent file/directory and SQL transaction; do the single no-replace move/mkdir/empty rmdir; fsync **both** receiving/removing directory descriptors; then persist `done` with observed identity. Existing-entry displacement is immediately validated against C, including regular bytes/mode or literal link text. A mismatch is retained and causes recovery-required; do not publish a replacement over it.

```python
# Names are engine-validated single leaf components; fds are held and verified.
_rename_noreplace(leaf, slot, target_parent_fd, old_slots_fd)
failpoint("old_moved:" + str(ordinal))
os.fsync(target_parent_fd)
os.fsync(old_slots_fd)
# Verify displaced content through old_slots_fd before publishing new bytes.
# Persist "done" only after all required file/directory syncs succeed.
```

Use failpoint names `journal_reserved`, `journal_staged`, `intent:<n>`, `old_moved:<n>`, `new_published:<n>`, `dir_changed:<n>`, `synced:<n>`, `progress:<n>` and `files_verified`. A normal failure before any changes may release claims after verified abandonment; an uncertain failure keeps them. ENOSPC, fsync failure, unsupported rename semantics or identity loss cannot be reported as success.

- [ ] **Step 4: Run GREEN.** Test symlink ancestor/leaf substitution, malicious journal substitution, equal-byte foreign inode, read-only parent, mount-root, cross-device placement and permission errors. Force fsync failure at every receiving directory boundary and preserve evidence. Verify hardlinked existing files are replaced without modifying the other link's bytes. Test protected/empty subdirectories blocking directory-to-file replacement.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): journal recoverable per-file result publication"`.

### Task 7: Publish integration receipts, history and task lifecycle atomically

**Files:** Create `solace_core/integration_commit.py`, `tests/solace_core/test_integration_commit.py`; modify `tasks.py`, `lineage.py`, `observations.py`.

**Interfaces:** `IntegrationCommit(service,catalog)` exposes `finish(db, journal: Journal, operation_id: str) -> dict`, `receipt(receipt_id: str, *, after: int=0, limit: int=128) -> dict` and `latest_application(db, task_id: str, target_key: str) -> dict | None`. `finish` requires already verified/synced M and runs inside the caller's one write transaction/coordinator lock; no separate commit inside it.

- [ ] **Step 1: Add failing atomicity/lifecycle tests.** Interrupt after file verification before SQL publication: heads remain old; no receipt appears. Inject failure after each SQL publication statement and assert the whole transaction rolls back. On success, before=C follows prior target head, after=M follows C, every protected exclusion is recorded, incoming provenance is a receipt relation, and old ready results remain readable.

For a ready parent receiving a child, assert raw state becomes active and revision increments once; blocked parent remains blocked with its reason; active remains active. Ready child remains ready with a latest-result application relation. Unapplied direct children prevent root integration. Cancellation after integration does not remove bytes or receipts.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_commit.py' -v`.
- [ ] **Step 3: Implement exact single-transaction publication.** Revalidate all pinned metadata and owning claims without recapturing/recomputing M. Publish C/M existing contexts and their before/after refs; source contexts retain `purpose="source"`, workspace contexts `purpose="workspace"`. Set exactly the source or parent workspace head. Clear stale workspace observations. Do not advance the incoming task workspace head when applying to another target.

Append the immutable receipt with approval/check status, B/R/C/M/profile, path decisions, previous contribution receipt, before/after IDs, original apply or recovery operation ID, actor/client label and event ID. `details_json` references immutable decision rows; no duplicate prompt or file bytes. Get the event sequence inside this transaction. Mark journal committed/plan applied, write operation result and release affected task/path claims in the same transaction.

```python
# Conceptual transaction boundary; individual mutations remain explicit/testable.
with coordinator.lock, catalog.connect(write=True) as db:
    result = committer.finish(db, journal, operation_id)
# Notify only after commit; a notification failure cannot overwrite success.
catalog.notify()
```

Represent child/latest-result counts separately from lifecycle counts; retain existing raw states. `aggregate` remains `awaiting_integration` when latest ready contributions are unapplied, and stops counting an already applied latest result as pending. Existing task show/list must remain bounded; do not append full receipt decisions or duplicate full manifests.

- [ ] **Step 4: Run GREEN.** Apply R1, edit destination-only bytes, resume source task, save descendant R2 and apply; R2 contributes only R2−R1. A different request applying an already-applied result returns its existing receipt without undoing later destination edits. Inject post-commit notification failure; replay is the committed success. Verify source usage uses its explicit head and never task-global latest.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): publish result receipts and lifecycle changes atomically"`.

### Task 8: Explicit interruption recovery and durable location handoff

**Files:** Create `solace_core/integration_recovery.py`, `tests/solace_core/test_integration_recovery.py`; modify `catalog.py`, `service.py`, `diagnostics.py`, `integration_claims.py`.

**Interfaces:** `IntegrationRecovery(service,catalog)` exposes `classify_interrupted(db) -> list[dict]`, `show(operation_id: str, *, after: int=0, limit: int=128) -> dict`, `accept(request: dict) -> dict`, `run(operation_id: str, request: dict) -> None` and `reconcile_location(previous: dict, request_id: str) -> None`. Interrupted original operations remain immutable historical outcomes; recovery requests own fresh operation IDs and `integration_recoveries` links.

- [ ] **Step 1: Add failing restart tests.** Interrupt after displacement, restart a new CoreService over the same disposable registry, and assert no file writes occur and claims remain. Metadata status/doctor show recovery-required; normal affected-tree access remains blocked. Explicit resume finishes; explicit restore-before produces C, marks restored and requires a fresh plan for another apply.

Add a mismatch test: create a third-party winner at a published name before restore; restoration refuses and preserves winner, displaced versions and claims. Store/target unavailable yields deferred validation, not empty replacement directories or automatic relink.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_recovery.py' -v`.
- [ ] **Step 3: Implement restart classification and action reconciliation.** Modify generic `recover_interrupted` so it does not release task claims belonging to unresolved integration publication/export. Mark queued/running operations interrupted; classify plans/journals using persisted state without auto-writing live files. Reserved-only/prepared operations can be abandoned only after checking absence of file changes.

For a progress-lagged action, compare recorded expected source/destination/slot identities and versions through held fds. Only one unambiguous old/new state can resume; any absent/replaced/multiple candidate or equal-byte unrecognized inode requires explicit conflict inspection, never inference from existence alone. Staged bytes retain identity in durable intent before publication.

Restore-before reverses the recorded action order using no-replace moves. Move any operation-published new entry into a retained recovery slot before restoring old; never unlink it. Pre-record recovery moves, fsync both sides and persist progress so recovery itself survives a crash. Identity-check created directories and remove only empty operation-owned directories; recreate only explicitly removed C directories. Unchanged/unaffected user entries remain untouched.

```python
# Replays consult the recovery operation first, not the current original state.
existing = catalog.find_operation(request)
if existing is not None:
    return existing
# Then validate original journal, writers_paused, registration and live identities.
```

After resume files=M, call Task 7 commit with the new recovery operation; original interrupted outcome stays intact. After verified restore files=C, atomically publish restoration outcome, set plan/journal restored, release claims and retain all references. Restoration is incomplete-apply recovery, not forward undo of an already successful integration.

Project relink while an available unresolved apply exists remains blocked. For genuinely unavailable moved storage, use the existing explicit project relink handshake: validate candidate source/store UUID and marker, persist previous/new locations in its registry receipt, then gate new work on pending integration reconciliation. Store moves rebase only store-contained refs; an external sibling journal cannot be guessed at a new path. Recovery resume/restore-before accepts the all-or-none relocation group from the API contract. For source targets its target_path must equal the now-registered root. For workspace targets verify the recorded owner, task, immutable journal marker, complete old/new action states, protected boundaries and a matching moved target; refuse paths owned by another task/project. Missing journal or uncertain provenance keeps recovery blocked.

The CLI exposes `--target-path`, `--journal-path`, `--confirm-target`, `--confirm-journal` only on recovery mutations. The recovery request receipt fixes that mapping; require fresh writers-paused acknowledgement. Persist its exact old/new location intent before changing stored paths. Then atomically update journal/action location metadata, affected claims, workspace identity when applicable and reconciliation events. A crash during handoff must be replayable from its recorded mapping before any file recovery starts. Do not rebind an approval for new work or rewrite original request params/fingerprints and historical outcomes. Ordinary restart reports pending handoff; only the explicit recovery request completes it.

- [ ] **Step 4: Run GREEN.** Crash recovery again after its own move and before progress; restart remains non-writing. Verify files=M-before-SQL can finish once, committed-before-reply replays once, restoration produces C, journal never cleaned automatically, and original interrupted operation remains unchanged. Test project/store remount with missing journal and completed handoff followed by crash.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): recover interrupted integration without automatic writes"`.

### Task 9: Strict protocol, dispatch and bounded observations

**Files:** Create `solace_core/integration_protocol.py`, `integration_operations.py`, `tests/solace_core/test_integration_protocol.py`; modify `protocol.py`, `service.py`, `observations.py`, `catalog.py`, `diagnostics.py`.

**Interfaces:** `INTEGRATION_PARAMS: dict[str,tuple[set[str],set[str]]]` and `validate_integration(method: str, params: dict) -> None` implement the API table. `IntegrationOperations(service,catalog)` exposes `dispatch(request: dict, *, registered: bool) -> dict`, `accept(request: dict) -> dict`, `run(operation_id: str, request: dict) -> None`. Long operations use existing FIFO coordinator/executor and catalog operation/event methods; never run arbitrary commands.

- [ ] **Step 1: Add failing protocol/dispatch tests.** For every API row, construct one valid request and mutate every required/optional field to wrong type, missing, extra and out-of-range. Reject booleans as revisions/cursors, malformed IDs/tokens, unsafe relative paths, missing unchecked/writer acknowledgement, mutually exclusive resolution inputs and mixed diff cursor modes.

```python
def test_replay_wins_over_now_stale_revision(self):
    request = self.prepared_approval_request()
    first = self.service.handle(request)
    self.assertTrue(first["ok"], first)
    self.resolve_current_plan_again()
    self.assertEqual(self.service.handle(request), first)
    changed = dict(request, actor="another actor")
    self.assertEqual(self.service.handle(changed)["error"]["code"], "REQUEST_ID_REUSED")
```

`prepared_approval_request` returns the exact Task 5 approval envelope for a conflict-free plan. `resolve_current_plan_again` creates a new revision through explicit resolution; tests may use a conflicted plan resolved once before approval then revise a selected decision. Do not implicitly refresh params while replaying.

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_protocol.py' -v`.
- [ ] **Step 3: Wire all methods behind schema and replay guards.** Add integration parameter sets without colliding with generic absolute `path` validation: integration `path` is engine-relative, `from_file/destination` absolute. Validate integration fields before the generic string fallback can accept booleans or unexpected enums. Keep duplicate-key, NaN and complete-frame checks unchanged.

Dispatch stable project IDs through `_receipt_catalog`. Search operations/task_requests before registration, revisions, tree availability or CAS for mutations. Schema-2 task reads remain supported; new integration needs 3. Metadata/receipt inspection works after unregister while the retained store is readable; new mutations need registration. Accept async operations with claims/context persisted before scheduling, and revalidate ownership on worker entry.

Catch failures by stage: before target mutation can be failed with safe claim release; uncertain publication/recovery retains claims and sets recovery-required. Never route uncertain integration through the generic task error path that drops claims. The response/result/event stores bounded references only, not complete plan snapshots. Add `integrations.v1` and `core_schema_supported=[1,2,3], core_schema_created=3` only now.

- [ ] **Step 4: Run GREEN.** Test every method over `SocketServer` including a large plan/content page, malformed frame and lost reply. Restart, unregister, then replay a completed apply/approval and inspect receipt; no file/CAS availability check precedes the retained receipt. Same ID in a different synchronous/async namespace is refused. Verify no prompt canary occurs in operations/events/task_requests/integration metadata or captured log messages.
- [ ] **Step 5: Commit.** `git commit -m "feat(core): expose bounded integration API with durable replay"`.

### Task 10: CLI workflow and real socket acceptance

**Files:** Create `solace_core/integration_cli.py`, `tests/solace_core/test_integration_cli.py`, `test_integration_acceptance.py`; modify `cli.py` and the integration fixture.

**Interfaces:** `add_parsers(commands) -> None` adds `integration` grammar. `prepare(client,args,project: str,request_id: str | None=None) -> tuple[str,dict]` constructs only the pinned API fields in the table. Reuse `task_cli.wait_for_operation` and `CoreClient`. Explicit request IDs must not fetch a different revision/token on retry.

- [ ] **Step 1: Add failing CLI tests and the full disposable workflow.** Reuse the real Unix socket fixture in `test_task_acceptance.AcceptanceTests` and `run_cli/result` helpers. Assert missing capability explains core upgrade, missing review acknowledgement is actionable, exit status is nonzero on stale/conflicted/interrupted apply, and JSON stdout is one parseable envelope without prose.

Add `completed(*args)` to the integration acceptance class exactly as in existing acceptance: call the CLI with `--wait`, assert operation succeeded and return its result. Then build source→root-task→child, independent current edits, child ready, prepare/show, approve/apply child to parent, parent ready, approve/apply parent to source. Assert exact bytes and root inode, before/after ancestry, prompt privacy and retained Git/vault bytes.

```python
def test_prepare_does_not_apply_and_requires_reviewed_revision(self):
    task, result, _ = self.ready_task({"a.txt": b"incoming\n"})
    prepared = self.completed("integration", "prepare", self.project, task,
                              "--result", result)
    plan_id = prepared["plan_id"]
    shown = self.result("integration", "show", self.project, plan_id)
    self.assertEqual(shown["state"], "prepared")
    self.assertFalse((self.root / "a.txt").exists())
    denied = self.run_cli("integration", "approve", self.project, plan_id,
                         "--revision", str(shown["revision"]),
                         "--token", shown["review_token"], success=False)
    self.assertNotEqual(denied.returncode, 0)
```

- [ ] **Step 2: Run RED.** `python -m unittest discover -s tests/solace_core -p 'test_integration_cli.py' -v` and the acceptance file; expect unknown integration command.
- [ ] **Step 3: Implement command construction without hidden approval.** Support every command in the approved spec, plus explicit deletion `resolve --take delete`. Map `--from-file`→`from_file`, `--to`→`destination` and CLI `--approval`→API `approval`. Export requires `--revision`; show supplies it. Prepare accepts `--expected-revision` and child `--expected-target-revision`; initial interactive/convenience calls may read these exact task revisions, while explicit request-ID calls require the user to provide them. Approve/apply/resolve/export/abandon always require a pinned revision. Never automatically approve a candidate or silently replace an apply token.

Diff supports `--revision --after --limit` for metadata; explicit version bytes use `--path --version --offset` and JSON base64 fields. Receipt/recovery show supports bounded paging. Recovery actions require `--writers-paused`. Add the explicit relocation confirmations defined in Task 8's handoff contract; no arbitrary application target flag is added.

Human output must say Solace did not run checks, report exact conflict/stale/recovery status and a concrete next command. Standard JSON includes typed fields; logs remain body-free. Help distinguishes an immutable saved result, an unpublished integration candidate, and an applied receipt.

- [ ] **Step 4: Run GREEN.** Complete the real socket/CLI round trip including a binary conflict resolved through imported file, repeated result/UUID replay, parent ready invalidation after another child applies and source result updated from R1 to R2. Demonstrate a protected `.git` file and nested vault survive. Verify no installed service or registry path was used.
- [ ] **Step 5: Commit.** `git commit -m "feat(cli): add manual result review and integration workflow"`.

### Task 11: Process-crash, hostile-path and protocol regression gates

**Files:** Create `tests/solace_core/integration_crash_worker.py`, `test_integration_crashes.py`; extend `test_integration_publish.py`, `test_integration_recovery.py`, `test_integration_protocol.py` and `test_integration_acceptance.py`.

**Interfaces:** Crash worker is a script invoked only by tests: `python integration_crash_worker.py FIXTURE_JSON FAILPOINT MODE`. `FIXTURE_JSON` is private, absolute, disposable metadata identifying its test registry/config/request; `MODE` is `apply/resume/restore-before`. It loads a CoreService with the test-only publisher failpoint callable, writes a durable barrier record, then exits via `os._exit(91)` at the exact named point. No production environment failpoint switch.

- [ ] **Step 1: Add RED coverage for every mutation boundary.** Enumerate failpoints for each action ordinal of a multi-file add/delete/type-change fixture, plus final SQL publication and reply loss. Parent runs the worker as a subprocess, confirms return code 91, snapshots source/journal, opens a new service and asserts restart is non-writing.

```python
def test_restart_preserves_files_and_claims_after_displacement(self):
    run = self.crash_at("old_moved:0", mode="apply")
    self.assertEqual(run.returncode, 91)
    before = self.snapshot_recorded_paths()
    self.restart_service()
    self.assertEqual(self.snapshot_recorded_paths(), before)
    report = self.recovery_show()
    self.assertEqual(report["state"], "recovery_required")
    self.assertTrue(report["claims_retained"])
```

Define `crash_at` with `subprocess.run`, the current Python executable, a 20-second deadline and captured bounded output; require the barrier record naming the desired failpoint so a wrong crash cannot pass. `snapshot_recorded_paths` reads only fixture root and recorded owned slots with lstat/nofollow and records names/types/bytes/modes/inodes. `restart_service` closes the old fixture service before creating a new one over its recorded registry and CoreConfig. `recovery_show` reads the original operation through the API.

- [ ] **Step 2: Run RED.** Verify at least one newly added real-process failure reproduces a missing or incorrect recovery behavior before changing product code. Never count an import error or failure to reach the barrier as the expected crash test.
- [ ] **Step 3: Close verified gaps only.** For each reproduced defect, change the owning module and add its invariant assertion. Classify all crash points using the required matrix:

| Window | Assertion after restart | Explicit outcome |
| --- | --- | --- |
| Before journal reservation | No files/head/claim published | New prepare or abandon |
| After reservation/stage, before actions | Files=C; journal retained | Verify then abandon/resume |
| After action intent, before rename | No inferred progress | Identity-based resume or restore |
| After rename, before sync/progress | Both versions retained; uncertainty visible | Explicit verification; no blind overwrite |
| After final target sync, before SQL | Files=M; head old; no receipt | Resume commits once |
| During final SQL | All metadata rolls back together | Resume verifies and commits once |
| After SQL, before response | Head/receipt committed once | Original request returns original operation |
| During recovery action/handoff | No automatic writes; claims retained | New recovery request continues the recorded action |

Also exercise source/store/journal unmount simulation, symlink/FIFO substitution, insufficient space/fsync failures, newly occupied names, protected-prefix inventory changes, mismatched prompt redaction/expiry metadata, huge escaped decision pages, worker timeout, two-project concurrency and schema-1/2/3 compatibility. The expected behavior is preserved versions and explicit errors, not successful integration on every unsupported filesystem.

- [ ] **Step 4: Run GREEN and the complete source gate.**

```bash
python -m unittest discover -s tests/solace_core -p 'test_integration_*.py' -v
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

Keep original 192 behaviors; count and report the new total rather than promising 192 unchanged assertion strings when schema defaults legitimately changed. Record logs and actual counts in a new private evidence directory. No physical power-loss or arbitrary hostile same-user protection claim follows from subprocess tests. Repeat only failed or changed relevant checks, then one final whole-tree gate after remediation.
- [ ] **Step 5: Commit.** `git commit -m "test(core): prove interrupted integration and replay invariants"`.

### Task 12: User documentation, whole-branch review and concrete installer preview

**Files:** Create `docs/result-integration.md` and `docs/engineering/2026-10-16-result-integration-review-and-acceptance.md`; modify `README.md`, `CHANGELOG.md`, `docs/local-core.md`, `docs/task-workspaces.md` and `docs/engineering/2026-10-09-review-findings-and-follow-ups.md`. Inspect `packaging/install-core.py`; change packaging only if a verified missing module/payload issue requires it.

**Interfaces:** Operator documentation names the same CLI/API contract. Acceptance report distinguishes source-tested, previewed, installed and live-accepted evidence. Installer preview is existing `--preview`, never `--apply`.

- [ ] **Step 1: Write the operational guide against the actual CLI.** Include prepare/show/diff/export/resolve/approve/apply/receipt, exact revision/token provenance, `not_run_by_solace`, recovery resume/restore-before and relocation confirmation. Include root-ready child gating, repeated R1/R2 submissions, source-only edits, retained journals, mount-parent limitations, unavailable stores and old runtime refusal of schema 3. Explicitly distinguish incomplete-apply restoration from future forward undo.

```markdown
Approval names one exact candidate revision. If Solace reports a stale plan,
prepare and review a fresh plan; never reuse the old approval token.
Solace does not run configured checks in this release. --accept-unchecked
records that fact; it does not mark tests as passed.
```

Update milestone status truthfully: “implemented and source-tested” until installation is separately approved and actually verified. Keep check runner/automatic policy/undo/GUI and other deferred rows; don't mark them complete because manual integration exists.

- [ ] **Step 2: Obtain independent whole-branch review under the user-selected execution method.** Reviewer reads approved spec, plan, complete diff and test evidence; trace live-tree access guards, replay namespaces, exact ancestry, approval invalidation, journal filesystem sync, SQL atomicity, relocation and prompt retention. Do not choose a new external service or model override without authorization. Record confirmed findings with paths and specific reproduction; fix blockers using failing regression then passing evidence. If no independent reviewer is callable, explicitly report that limitation instead of manufacturing a review.

- [ ] **Step 3: Produce a preview and compare its runtime payload.**

```bash
integration_preview=$(mktemp -d /var/tmp/solace-integration-preview-XXXXXXXX)
chmod 700 "$integration_preview"
python packaging/install-core.py --preview --preview-dir "$integration_preview"
```

Record the actual generated files, proposed runtime digest, module payload inclusion, current installed runtime and rollback chain. Check the pinned rollback helper at `/home/lucievol/.local/share/solace/core-rollback-tools/e77eb01bf4e57e51/rollback-core.py` still exists and matches its recorded hash; do not copy rollback commands from deleted worktrees. Preview may create its owned evidence directory, but must not switch service units/runtime links or migrate projects.

- [ ] **Step 4: Verify docs and final source state.** Validate local links, fenced shell syntax and commands against actual `--help`. Ensure every API row has validator/dispatch/test owners, all source gates are green after blocker remediation, working tree contains only intended changes and every deferred item remains tracked. Record exact commit, checks, known limits, installer preview and a **not installed** status in the acceptance report. Do not run installed acceptance without a subsequent concrete installation approval.

- [ ] **Step 5: Commit and hand off.** `git commit -m "docs: document reviewed manual integration and install preview"`. Present the source result, test counts, independent-review outcome, runtime preview and rollback limits. A future install changes new-store default to schema 3; existing user projects remain 1/2 until explicit per-project preview/apply. Request only the remaining concrete install action, not another approval of already authorized source work.

## Dependency and requirement coverage

Execute Tasks 1–12 in order. Task 2 can be read independently, but Tasks 3–9 share persistence/ownership contracts; keep public capability disabled until the whole dispatch slice exists. Every task has a RED→GREEN gate and a focused commit. The full branch is not releasable while publication/recovery is incomplete.

| Approved specification section | Owning tasks / evidence |
| --- | --- |
| 1. Manual scope and truthful checks | 5, 9, 10, 12; explicit unchecked tests |
| 2. Journal approach and visibility limits | 6, 8, 11; root inode and crash matrix |
| 3. B/R/C/M, exact targets and incremental ancestry | 3, 7, 10; R1/R2 and divergence tests |
| 4. Prepare/conflicts/resolution/export | 2, 3, 4; byte-preserving merge, immutable revisions and bounded worker |
| 5. Approval/lifecycle | 5, 7, 10; stale token, ready-parent and applied-result counts |
| 6. Protected paths/ownership | 3, 5, 6; inventories, unavailable catalogs and access guards |
| 7. Per-file durable publication | 5, 6, 7, 11; no-replace, receiving-dir sync, final SQL |
| 8. Crash/replay/recovery/relocation | 8, 9, 11; non-writing restart and durable handoff |
| 9. Immutable receipts/retention/privacy | 1, 4, 7, 9, 11; SQL triggers, refs and prompt canary |
| 10. Schema/API/CLI bounds | 1, 9, 10; frozen migration/replay and real socket pages |
| 11. Acceptance and installation boundary | 10, 11, 12; full suite, reviewer and preview |
| 12. Written decisions and deferred work | 12; deferred register preserved |

## Plan review and execution handoff

Self-review must check spec coverage, signature/field consistency, SQL migration/replay behavior, placeholder absence, all five Review Focus tests, local links and fenced code syntax. It does not claim that the planned software or tests already exist.

No new execution method was supplied with the design approval. Present this committed plan for review and offer:

- **Subagent-driven:** fresh implementer and review per task, plus whole-branch review; more independent checks, more context cost.
- **Inline execution:** implement sequentially in this session with checkpoints and independent whole-branch review.

Recommend subagent-driven for this plan because the per-file recovery and cross-method claims affect user files and warrant independent review between tasks. Do not spawn agents while merely writing/reviewing this plan. After the user chooses and approves the plan, use the corresponding execution skill and preserve that choice.



