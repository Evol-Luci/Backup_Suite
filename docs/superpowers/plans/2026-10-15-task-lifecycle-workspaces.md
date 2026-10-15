# Parent/Child Tasks and Isolated Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user selected inline/native execution; preserve that selection. Obtain independent whole-branch review before installation.

**Goal:** Record parent and direct-child requests, prepare isolated workspaces with exact ancestry, and save durable ready results for subsequent review and integration.

**Architecture:** Extend each project's existing SQLite catalog with tasks, workspace identities, explicit checkpoint ownership, prompts, and publication intents. Keep the registry as the location authority and the core as the only coordinated writer. Build verified immutable materialized bases from the chunk store, then reflink or copy into new mutable workspaces; publish database state only after filesystem verification.

**Tech Stack:** Existing Python standard library, SQLite, Linux `FICLONE` and `renameat2(RENAME_NOREPLACE)`, Unix API version 1, argparse, systemd user service, and unittest. No new runtime dependencies.

**Spec:** [Approved subsystem design](../specs/2026-10-15-task-lifecycle-workspaces-design.md).

**Status:** Written plan awaiting user review. No product implementation or installed-runtime changes are authorized by this document alone.

## Global Constraints

- “Linux first, existing Python standard-library core, SQLite, local Unix API, and CLI.”
- “The first slice supports root tasks and their direct children.”
- “Creating a record does not scan files or create a workspace.”
- “This milestone ends at saved results and an honest terminal status of work awaiting review/integration.”
- “There is no `complete` or `integrated` transition in this slice.”
- “External tools such as Codex and Claude execute the requests.” Solace does not launch, stop, or sandbox agents.
- “Maintain separate source and workspace heads.”
- “This filtered task baseline does not advance the manual source head.”
- “User exclusions are copied into the task's effective profile at creation, inherited by its children, and shown by task inspection.”
- “Never use hardlinks.”
- “A missing/remounted/replaced workspace is unavailable; it is never silently recreated at the same path.”
- “Only relative symlinks whose resolution remains inside the workspace are allowed for materialized tasks.”
- “Workspace deletion and cache garbage collection are explicitly deferred.”
- “Limit prompts to 128 KiB UTF-8 and also enforce the existing complete-frame bound.”
- “Approved project default: expire prompt text after 30 days.”
- “`none` never persists the body.”
- “Expired bodies are hidden from every prompt read immediately, even if cleanup is delayed by unavailable storage.”
- “The checkpoint engine's schema numbering remains separate.”
- “A runtime rollback never silently rolls back project databases or erases task/workspace references.”
- “Existing 122 tests remain green”; preserve legacy GUI, vault, checkpoint, registration, socket, and receipt behavior.
- No merge/apply, configured checks, project-source replacement, Git history backend/import, agent launcher, GUI redesign, network listener, object deletion, or automatic installation.

## Review Focus

1. A stale retry arrives after prompt deletion or a task transition: replay the original body-free receipt before checking revisions; never restore deleted text (Tasks 2, 7).
2. Another process replaces a workspace ancestor, inserts a symlink, or wins the destination name during preparation: fail publication without following it or overwriting anything (Tasks 3, 4).
3. A child capture is newer than source and sibling checkpoints: every head, usage report, and next parent still follows its explicit owner (Tasks 1, 5).
4. Power/process loss occurs between rename and SQLite commit, with the store unavailable on restart: preserve external state and evidence, defer recovery, and require explicit validation before adoption (Task 6).
5. An unsupported reflink error differs from an I/O/capacity failure, or the cache was modified: fallback only for unsupported cloning, verify all bytes/modes, and never clone corrupt bases (Task 4).

## Working location and baseline

Continue in `/home/lucievol/Projects/Backup_Suite/.worktrees/task-workspace-design`, branch `codex/task-workspace-design`. Its reviewed product baseline is local `main` at `40c8d22`; commits `af5643e` and `7f7b779` contain the design. Preserve the original checkout, other worktrees, installed core, installer backup, and disposable acceptance evidence.

The existing baseline has 57 core, 49 engine, and 16 top-level tests. At execution start run all three discovery commands in Task 10 and record fresh counts. Do not describe historical counts as a fresh pass. There are no `__init__.py` files in test subdirectories; run discovery separately to avoid import shadowing.

## File responsibilities

| File | Responsibility |
| --- | --- |
| `solace_engine/repository.py` | Explicit-parent capture, optional protected scanner predicate, reusable read-only manifest comparison; keep direct legacy defaults. |
| `solace_core/schema.py` | Core schema 2 DDL, compatible version checks, consistent-backup migration and preview token. |
| `solace_core/lineage.py` | Source head, checkpoint context/ref writes and source-only listing/usage. |
| `solace_core/tasks.py` | Task creation, revisions, claims, synchronous receipts, lifecycle and parent summaries. |
| `solace_core/prompts.py` | Exact prompt validation, effective retention, read/redaction/purge and policy revisions. |
| `solace_core/workspace_paths.py` | Configuration, placement, directory identities, descriptor-held paths and no-create validation. |
| `solace_core/workspace_trees.py` | Safe link graph, verified cache, reflink/copy materialization, no-write comparisons and no-replace publication. |
| `solace_core/task_operations.py` | Durable start/capture/ready/status/relink/recovery workflows and operation claims. |
| `solace_core/maintenance.py` | Bounded prompt maintenance scheduling; no retention bypass. |
| `solace_core/catalog.py` | Version-aware catalog access and shared transaction/event primitives. |
| `solace_core/service.py` | Dispatch, per-project admission, receipt-before-precondition ordering, lifecycle guards. |
| `solace_core/protocol.py`, `cli.py` | Strict typed parameter validation, capability checks and terminal/JSON commands. |
| `solace_core/diagnostics.py`, `packaging/install-core.py` | Read-only schema/availability diagnosis and runtime rollback compatibility notices. |
| `tests/solace_core/task_fixture.py` | Temporary stores, explicit workspace paths, request helpers and bounded waits; no production-only test hooks. |
| `tests/solace_core/test_task_*.py`, `tests/solace_engine/test_lineage.py` | Targeted contract, concurrency, crash, privacy and CLI tests. |
| `docs/task-workspaces.md` | Operator and agent handoff, migration/recovery grammar and honest limitations. |

## Fixed data and transaction contracts

All new IDs are lowercase UUID hex strings (32 characters); request IDs retain the existing UUID envelope. Times are timezone-aware UTC ISO strings. Booleans never satisfy integer validators. A revision is an integer >=1, increased once per changed task or policy transaction. JSON below means a `dict[str, object]` containing JSON-serializable values. Public projections never expose stored request fingerprints or prompt bodies except `task.prompt.show`.

Engine schema stays `PRAGMA user_version=1`; registry schema stays 1; `core_metadata.schema_version` becomes 2. Existing engine/core tables remain. Execute each DDL statement with `db.execute` inside the migration transaction, **not** `executescript`, which can commit implicitly. SQL uses foreign keys and no cascade deletion:

```sql
CREATE TABLE source_head (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 checkpoint_id TEXT REFERENCES checkpoints(id)
);
CREATE TABLE project_prompt_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 revision INTEGER NOT NULL CHECK(revision>=1),
 policy TEXT NOT NULL CHECK(policy IN ('days','retain','none')),
 days INTEGER,
 CHECK((policy='days' AND days IS NOT NULL AND days>0) OR (policy<>'days' AND days IS NULL))
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY,
 parent_id TEXT REFERENCES tasks(id),
 title TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('created','active','blocked','ready','cancelled')),
 revision INTEGER NOT NULL CHECK(revision>=1),
 profile_json TEXT NOT NULL,
 block_reason TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX tasks_parent ON tasks(parent_id);
CREATE TABLE task_prompts (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 policy TEXT NOT NULL CHECK(policy IN ('days','retain','none')),
 days INTEGER,
 status TEXT NOT NULL CHECK(status IN ('missing','available','discarded','deleted','expired')),
 body TEXT,
 expires_at TEXT,
 redacted_at TEXT,
 CHECK((policy='days' AND days IS NOT NULL AND days>0) OR (policy<>'days' AND days IS NULL)),
 CHECK((status='available' AND body IS NOT NULL AND policy<>'none')
    OR (status<>'available' AND body IS NULL))
);
CREATE TABLE workspaces (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 path TEXT NOT NULL UNIQUE,
 mount_id INTEGER NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
 base_checkpoint TEXT NOT NULL REFERENCES checkpoints(id),
 head_checkpoint TEXT NOT NULL REFERENCES checkpoints(id),
 observed_at TEXT, observed_json TEXT
);
CREATE TABLE checkpoint_context (
 checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
 purpose TEXT NOT NULL CHECK(purpose IN ('source','task_base','child_base','workspace','ready')),
 task_id TEXT REFERENCES tasks(id),
 workspace_id TEXT,
 profile_json TEXT NOT NULL,
 published INTEGER NOT NULL CHECK(published IN (0,1))
);
CREATE TABLE task_checkpoint_refs (
 task_id TEXT NOT NULL REFERENCES tasks(id),
 checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
 role TEXT NOT NULL CHECK(role IN ('base','head','result')),
 created_at TEXT NOT NULL,
 PRIMARY KEY(task_id,checkpoint_id,role)
);
CREATE TABLE task_requests (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE task_claims (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 operation_id TEXT NOT NULL REFERENCES operations(id)
);
CREATE TABLE workspace_intents (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 task_id TEXT NOT NULL REFERENCES tasks(id), workspace_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('start','relink')),
 phase TEXT NOT NULL CHECK(phase IN ('reserved','staged','published','committed','abandoned')),
 staging_path TEXT, final_path TEXT NOT NULL,
 staging_identity_json TEXT, final_identity_json TEXT,
 base_checkpoint TEXT REFERENCES checkpoints(id),
 expected_json TEXT NOT NULL,
 recovery_operation_id TEXT REFERENCES operations(id)
);
CREATE UNIQUE INDEX workspace_intents_live ON workspace_intents(workspace_id)
 WHERE phase NOT IN ('committed','abandoned');
CREATE TABLE workspace_caches (
 checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
 cache_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
 identity_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 logical_bytes INTEGER NOT NULL CHECK(logical_bytes>=0),
 created_at TEXT NOT NULL
);
```

`checkpoint_context.workspace_id` intentionally has no foreign key: a reserved workspace ID owns its source baseline before its workspace row can exist. Resolve it through the intent until committed; never show a pending checkpoint as a usable workspace. All other references above are real foreign keys. `task_checkpoint_refs` are append-only retention/history references, including older heads and every saved result. `workspaces.head_checkpoint` is the current head. No reference or checkpoint deletion is implemented.

Profile JSON is canonical `{"version":1,"protected":[".git",".solace_vault","solace_owned_roots"],"exclusions":[...]}`. Reject duplicate/empty/NUL exclusion strings; preserve their order. Children inherit the entire profile and cannot override exclusions. Protected rules inspect every relative path component and canonical/physical Solace roots, independent of user glob rules. `.git` files and directories are both excluded. Ordinary dotfiles are included. New source captures record profiles in `checkpoint_context` as `{"version":1,"protected":[],"exclusions":[...]}`.

Availability is computed from path/mount/device/inode validation; never conflate it with external task state. Cached observation is explicitly dated and may be stale. It names the compared checkpoint; if that differs from the current head, return `stale: true` rather than presenting its clean flag as current. Inspection returns `workspace: null` until a successful start, plus a pending operation ID where applicable. Intent paths are exposed only by explicit recovery inspection, labelled unusable.

## Public API and CLI contract

Wire envelope remains version 1. Extend `core.status` with `capabilities: ["tasks.v1","workspaces.v1","project-upgrade.v1","prompt-retention.v1"]`, `core_schema_supported: [1,2]`, `core_schema_created: 2`. Each project view/doctor includes its actual schema and whether task upgrade is required. New CLI checks capabilities before new methods; missing capability returns actionable `CORE_UPGRADE_REQUIRED`, not an unexplained unknown method. Existing methods still work with older cores.

Every mutation checks a matching receipt first, under the project lock. Check **both** `operations.request_id` and `task_requests.request_id`; the same request ID cannot be reused across these namespaces with other arguments. Hash the complete original validated params, method and actor; persist only the hash and safe result. Never reconstruct a prompt from a receipt. For schema-1 catalogs, only the existing operation receipt table can be queried; do not query nonexistent task tables before upgrade. New successful synchronous mutations save their body-free receipt in the same transaction as state/event. Async acceptance inserts operation, intent when relevant, claims and queued event in one transaction. Admission failures are not cached.

| Method | Required params in addition to `project` | Optional params | Result |
| --- | --- | --- | --- |
| `project.upgrade` | `preview` bool | `plan_token` string for apply only | Preview JSON or applied schema/backup receipt |
| `project.prompt-policy.show` | none | none | policy, days or null, revision |
| `project.prompt-policy.set` | `policy`, `expected_revision` | `days` iff days policy | policy, days, revision |
| `task.create` | `title` | `parent`, `expected_parent_revision`, `prompt`, `prompt_policy`, `prompt_days`, `exclusions` | task_id, revision=1 |
| `task.list` | none | `parent` | tasks array of body-free views |
| `task.show` | `task` | none | body-free task view |
| `task.start` | `task`, `expected_revision` | `from_checkpoint`, `expected_parent_revision` | operation_id |
| `task.block` | `task`, `expected_revision`, `reason` | none | task_id, state, revision |
| `task.resume`, `task.cancel` | `task`, `expected_revision` | none | task_id, state, revision |
| `task.ready` | `task`, `expected_revision` | none | operation_id |
| `task.prompt.show` | `task` | none | status, expires_at, body only when available |
| `task.prompt.delete` | `task`, `expected_revision` | none | task_id, revision |
| `workspace.status` | `task` | `refresh` bool, default false | dated comparison, or operation_id if refreshing |
| `workspace.checkpoint` | `task`, `expected_revision`, `message` | none | operation_id |
| `workspace.relink` | `task`, `expected_revision`, `path`, `confirmed_path`, `checkpoint`, `writers_paused` true | none | operation_id |
| `workspace.recovery.show` | `operation` | none | body-free intent/identities/phase/availability |
| `workspace.recovery.resume`, `workspace.recovery.abandon` | `operation`, `task`, `expected_revision` | none | operation_id for resume; receipt for abandon |
| `workspace.staging.cleanup` | `operation`, `preview` bool | `plan_token` for apply only | candidate identity/token or cleanup receipt |
| `checkpoint.list` (existing) | none | `all_workspaces` bool | checkpoints, owner/context for published entries |

Task view keys: `id,parent_id,title,state,revision,profile,block_reason,created_at,updated_at,prompt,workspace,pending_operation,results,children,aggregate`. `prompt` is `{status,policy,days,expires_at}`; `workspace` contains `id,path,base_checkpoint,head_checkpoint,availability,last_observed`; `results` lists immutable checkpoint IDs in creation order. `children` counts all five states. Aggregate precedence: own cancelled -> cancelled; own/child blocked -> blocked; children created/active -> waiting_for_children; own ready with >=1 ready child -> awaiting_integration; otherwise own state. Child results are counts/references, never combined files.

Prompt policy is `days|retain|none`; `days` requires a positive integer, other modes forbid it. Default is days=30. Empty prompt is valid exact text; omitted prompt is missing. Title is a separate printable 1–200 character string, never derived from prompt; block reason is 1–1024 characters with no NUL. Do not echo supplied prompt in validation errors. Prompt UTF-8 encoding must be strict (reject lone surrogates), <=131072 bytes; the existing encoded 1048576-byte frame cap applies independently, including JSON escaping overhead.

All API task/operation/checkpoint IDs must be exact IDs belonging to this project. Source/store/workspace paths are absolute. Expected revisions are required on all task mutations except create; child create/start also require `expected_parent_revision`. Root create/start forbids it. Child create permits parent created/active/blocked; child start requires an active/blocked prepared parent. Resume a ready parent before adding/starting more work. Reject grandchildren. A child explicit baseline must be published, referenced in its parent's workspace history, and have the inherited profile; sibling-only and source-only checkpoints are ineligible. To reuse preserved cancelled work, root `task.start --from CP` accepts a published checkpoint referenced by a cancelled task in this project with exactly the new task's profile. It reuses immutable content and preserves original provenance; it does not reopen the cancelled task. Without `--from`, root start captures current source.

The CLI resolves project aliases as before. For task mutations it reads current revisions unless `--revision N` / `--parent-revision N` was supplied, then sends those exact revisions once; never silently retries conflicts. JSON/API clients can pin revisions directly. These are snapshots, not locks.

```text
solace project upgrade PROJECT --preview
solace project upgrade PROJECT --apply --plan-token TOKEN
solace project prompt-policy show PROJECT
solace project prompt-policy set PROJECT days --days 30 [--revision N]
solace project prompt-policy set PROJECT retain|none [--revision N]
solace task create PROJECT TITLE [--parent ID] [--parent-revision N]
    [--prompt-file PATH] [--prompt-retention retain|none|DAYS] [--exclude GLOB ...]
solace task start PROJECT TASK [--from CHECKPOINT] [--revision N] [--parent-revision N] [--wait]
solace task show PROJECT TASK [--watch]
solace task list PROJECT [--parent ID]
solace task block PROJECT TASK --reason TEXT [--revision N]
solace task resume|ready|cancel PROJECT TASK [--revision N] [--wait for ready]
solace task prompt show PROJECT TASK
solace task prompt delete PROJECT TASK [--revision N]
solace workspace status PROJECT TASK [--refresh] [--wait]
solace workspace checkpoint PROJECT TASK MESSAGE [--revision N] [--wait]
solace workspace relink PROJECT TASK --path ABS --confirm-path ABS
    --checkpoint CP --writers-paused [--revision N] [--wait]
solace workspace recovery show PROJECT OPERATION
solace workspace recovery resume|abandon PROJECT OPERATION --task TASK [--revision N] [--wait for resume]
solace workspace staging cleanup PROJECT OPERATION --preview
solace workspace staging cleanup PROJECT OPERATION --apply --plan-token TOKEN
solace checkpoint list PROJECT [--all-workspaces]
```

Common `--json`, `--socket`, `--request-id` remain supported anywhere. Long operations return accepted operation IDs by default. `--wait` polls `operation.show` with a bounded interval and reports terminal outcome; timeout leaves the operation running and prints its ID. `--watch` uses durable event cursors and refetches body-free task views, never raw requests. Prompt show writes the exact body without adding a newline in human mode; JSON mode encodes it normally. Prompt files are read as bytes then strict UTF-8, preserving CRLF and final-newline absence. No stdin prompt or implicit editor in this slice.

New stable errors: `PROJECT_UPGRADE_REQUIRED`, `CORE_UPGRADE_REQUIRED`, `UPGRADE_PREVIEW_STALE`, `TASK_NOT_FOUND`, `TASK_REVISION_CONFLICT`, `TASK_STATE_INVALID`, `TASK_BUSY`, `TASK_CHILDREN_PENDING`, `TASK_DEPTH_UNSUPPORTED`, `BASELINE_INELIGIBLE`, `WORKSPACE_UNAVAILABLE`, `WORKSPACE_IDENTITY_MISMATCH`, `WORKSPACE_SYMLINK_UNSAFE`, `CACHE_CORRUPT`, `RECOVERY_REQUIRED`, `RECOVERY_PREVIEW_STALE`, `PROMPT_INVALID`, `PROMPT_UNAVAILABLE`. Preserve existing structured errors, no body-bearing exceptions. A revision conflict directs the caller to inspect and decide again.

## Task 1: Explicit ancestry, schema 2 and reviewed migration

**Files:** Create `solace_core/schema.py`, `solace_core/lineage.py`, `tests/solace_engine/test_lineage.py`, `tests/solace_core/test_task_schema.py`; modify `solace_engine/repository.py`, `solace_core/catalog.py`, `solace_core/service.py`, `tests/solace_core/test_catalog.py` and the existing capture-wrapper tests in `tests/solace_core/test_service.py`.

**Interfaces:**
- Engine: `create_checkpoint(message: str, exclusions=(), *, parent_id=LATEST_PARENT, excluded=None, publication_hook=None) -> Checkpoint`. Module sentinel `LATEST_PARENT` retains legacy global-latest behavior only when omitted. Explicit `None` means no parent; a string must identify an existing checkpoint in this store before scanning and again in the publication transaction. `excluded(relative: str, metadata: os.stat_result) -> bool` augments existing exclusions without following links.
- `create_schema_v2(db: sqlite3.Connection) -> None` executes the DDL above and inserts singleton defaults. `schema_version(catalog) -> int` reads without changing files.
- `preview_upgrade(catalog) -> JSON`; `apply_upgrade(catalog, plan_token: str, request: JSON) -> JSON`, called under the project coordinator with no pending operations.
- `Lineage(catalog).source_head(db=None) -> str | None`; `.publish_source(db, checkpoint, exclusions: list[str]) -> None`; `.published(all_workspaces: bool=False) -> list[JSON]`; `.source_usage() -> JSON`.

- [ ] Write RED ancestry and migration tests. Use an engine temporary repository fixture from `tests/solace_engine/test_repository.py`; a focused new test body is:

```python
a = repo.create_checkpoint('source', parent_id=None)
b = repo.create_checkpoint('child', parent_id=a.id)
c = repo.create_checkpoint('sibling', parent_id=a.id)
self.assertEqual((b.parent_id, c.parent_id), (a.id, a.id))
self.assertEqual(repo.create_checkpoint('root', parent_id=None).parent_id, None)
with self.assertRaises(CheckpointNotFound):
    repo.create_checkpoint('wrong', parent_id='0' * 32)
```

Create a frozen schema-1 fixture using the current core DDL, not a production flag that creates old schemas. Put two checkpoints in it. Assert preview changes no file bytes/sidecar names; apply preserves engine rows and bytes, initializes source head to the latest existing checkpoint by `(created_at,rowid)`, backs up a readable schema-1 catalog, and emits exactly one upgrade event. A forced exception after the first new table must leave version 1 and no new tables. Change event maximum after preview and assert stale apply refuses before backup/schema writes. Schema 3 remains unsupported; adjust the existing test that currently treats schema 2 as unsupported. Preserve read-only/offline no-sidecar tests.

- [ ] Run `python -m unittest discover -s tests/solace_engine -p test_lineage.py -v` and `python -m unittest discover -s tests/solace_core -p test_task_schema.py -v`; expect missing keyword/module failures.
- [ ] Implement the interfaces. Upgrade token is SHA256 of canonical JSON containing DDL digest, project/store UUIDs, store canonical path/mount, versions, latest checkpoint ID/count and maximum event sequence. Validate it again immediately before migration; refuse pending operations. Use SQLite backup into an exclusively created `store/schema-backups/<request-id>/checkpoints.sqlite3`, set directory mode0700/database mode0600, fsync file/directory, verify backup integrity/version, then `BEGIN IMMEDIATE`, execute DDL, import existing checkpoint contexts as published source entries with `profile_json={"version":1,"protected":[],"exclusions":null,"origin":"legacy_unknown"}` (do not invent historical exclusion rules), set head/default policy, update core version and insert migration event plus safe request receipt, commit. Failed backup/migration preserves its evidence and schema 1; no automatic database restore. New stores create version 2 directly. `validate()` accepts core 1 or 2, task entry points require 2; engine version remains 1.

```python
# Source capture in a schema-2 catalog; work/scan stays outside write tx.
head = Lineage(catalog).source_head()
def publish(db, cp):
    Lineage(catalog).publish_source(db, cp, exclusions)
    catalog.complete_checkpoint(db, operation_id, cp)
repo.create_checkpoint(message, exclusions, parent_id=head, publication_hook=publish)
```

Source-only listing/usage reads recorded head and published context, not global latest. Existing physical object-byte counts remain labelled as CAS file bytes. Report retained references and `reclaimable_bytes: null` for schema 2; no claim of safe collection. Keep schema-1 legacy checkpoint operations unchanged. Update test monkeypatch capture wrappers to forward the new keywords without weakening crash assertions.

- [ ] Run both new suites and all existing core/engine suites. Expect explicit ancestry, failed migration rollback, read-only preview and legacy tests to pass. Verify the schema-1 receipt path still opens after unregister.
- [ ] Commit only named changed files: `feat: add explicit checkpoint lineage and reviewed core schema upgrade`.

## Task 2: Task records, revisions, prompts and safe receipts

**Files:** Create `solace_core/tasks.py`, `solace_core/prompts.py`, `tests/solace_core/task_fixture.py`, `tests/solace_core/test_task_records.py`, `tests/solace_core/test_task_prompts.py`; modify `solace_core/catalog.py` for transaction-local event/receipt helpers.

**Interfaces:** `TaskCatalog(catalog, *, now=None)` defaults to an aware UTC clock. `.create(request: JSON) -> JSON`, `.show(task_id: str) -> JSON`, `.list(parent: str|None=None) -> list[JSON]`, `.lookup_request(request, db) -> JSON|None`, `.claim(db, operation_id: str, revisions: dict[str,int]) -> None`, `.release(db, operation_id) -> None`, `.transition(request: JSON) -> JSON`. `PromptStore(catalog, *, now=None)` exposes `.policy() -> JSON`, `.set_policy(request) -> JSON`, `.read(task_id) -> JSON`, `.delete(request) -> JSON`, `.purge_expired(*, limit: int=100) -> int`. Internal `.insert(db, task_id, supplied: str|None, policy: str, days: int|None, created_at: str) -> None` and `.metadata(db, task_id) -> JSON` use the same effective-policy/expiry calculation. All mutations use the caller's coordination lock; SQL helpers use the passed connection, never open nested writers.

- [ ] Write RED tests with `unittest.TestCase` and temporary catalog fixtures. `task_fixture.py` supplies `CatalogCase.setUp` with `self.catalog`, `self.tasks=TaskCatalog(self.catalog)`, and `request(method, **params)` returning a fresh version-1 request. It also supplies `TaskCase` using the existing `ServiceTests.setUp/call/wait_operation` methods by assignment (do not inherit/re-run their test methods), an isolated configured workspace root, `create_task(title='work', **params) -> str`, `show(task) -> JSON`, `start(task, **params) -> JSON` that waits and asserts succeeded, and `mutate(method,task,**params)` that supplies the inspected revision. `TaskCase.request` is the same envelope helper as `CatalogCase.request`. Its setup closes the service constructed by `ServiceTests.setUp` and reopens `CoreService(self.registry, config=CoreConfig(sync_roots=(), workspace_root=self.base/'workspaces'))`; pass a private data root/config explicitly, never use the real installed workspace directory. API helpers become usable after Task 5 dispatch; early tests use `CatalogCase` and direct methods.

```python
request = self.request('task.create', title='Improve validation', prompt='é\r\nexact')
first = self.tasks.create(request)
self.assertEqual(self.tasks.create(request), first)
view = self.tasks.show(first['task_id'])
self.assertEqual(view['revision'], 1)
self.assertNotIn('é\r\nexact', json.dumps(view))
self.assertEqual(PromptStore(self.catalog).read(first['task_id'])['body'], 'é\r\nexact')
```

Assert create never calls engine scan/materialization; root/child creation and grandchild refusal; child inherits exact profile; creating child bumps parent revision; stale parent revision commits nothing; prompt `None` vs empty; 131072-byte boundary vs 131073; NUL/lone surrogate rejection; frozen default30days/task overrides; `none` body absent from database/API/events/receipt. A repeated create request after prompt delete returns the original ID without a new body. Cross-table request-ID reuse conflicts, including an existing checkpoint operation ID. Use a unique multi-line canary to search `task_requests`, operations, events and logger capture; don't require forensic absence from SQLite free pages.

- [ ] Run `python -m unittest discover -s tests/solace_core -p 'test_task_records.py' -v` and `python -m unittest discover -s tests/solace_core -p 'test_task_prompts.py' -v`; expect missing TaskCatalog/PromptStore behavior.
- [ ] Implement exact validators and transactions. Record creation inserts task, effective prompt record, receipt and `task.created` event together. Root profile comes from protected defaults plus explicit exclusions; child inherits and forbids overrides. Child topology change updates parent revision in the same transaction. Initial prompt state is missing if omitted, discarded if supplied under none, otherwise available. Redaction clears body and records reason/ID only. Delete increments task revision; repeat same request replays before revision checks. A new delete on already-redacted content can return an unchanged revision with a receipt and no duplicate event. Reads compute expiration immediately with the injected production clock; cleanup is independent. Policy set uses a separate singleton revision and affects future creates only. Never put body in result messages or fallback titles.

```python
# Every successful synchronous mutation follows this order inside one tx.
cached = tasks.lookup_request(request, db)
if cached is not None:
    return cached
# Check version, state, claims and expected revision, then update + event.
# Persist result fields from the method's allowlist, never original params.
```

- [ ] Run the two focused suites plus operation/catalog suites; expect rollback, expiry and replay tests to pass without prompt data in secondary records.
- [ ] Commit named files: `feat: record revisioned tasks and retained private prompts`.

## Task 3: Workspace placement, protected capture and no-write comparison

**Files:** Create `solace_core/workspace_paths.py`, begin `solace_core/workspace_trees.py`, create `tests/solace_core/test_task_paths.py`, `tests/solace_core/test_task_comparison.py`; modify `solace_core/storage.py`, `solace_core/paths.py`, `solace_core/registry.py`, `solace_core/service.py`, `solace_engine/repository.py`.

**Interfaces:** Frozen dataclass `CoreConfig(sync_roots: tuple[Path,...], workspace_root: Path)` exposes `.load(data_root: Path) -> CoreConfig` and has `sync_roots: tuple[Path,...]`, `workspace_root: Path`; accepts only JSON keys `sync_roots` and `workspace_root`. Default workspace root is `<data-root>/solace/workspaces`. `WorkspacePaths(registry, config, policy).reserve(project_id: str, task_id: str, operation_id: str) -> JSON` selects but does not create final paths; `.validate(path: Path, *, expected: JSON|None=None, creating: bool=False) -> JSON` returns `{path,mount_id,device,inode}` for existing directories. `.open_checked(path, expected)` is a context manager yielding an open directory descriptor. `directory_identity(path: Path, policy: StoragePolicy) -> JSON` checks only local directory identity (also usable inside a validated store for cache paths). `protected_excluder(root: Path, profile: JSON, owned_roots: list[Path]) -> Callable[[str,os.stat_result],bool]` binds the capture root explicitly. `compare_tree(repo: ProjectRepository, checkpoint: str, root: Path, profile: JSON) -> JSON` returns `{checkpoint_id,observed_at,clean,added,removed,changed,logical_bytes}`; `changed` items are `{path,reasons}` with content/mode/type/link_target reasons.

- [ ] Write RED path and comparison tests. Temporary source contains normal `.env`, nested `.git` directory, nested `.git` pointer file, `.solace_vault`, a nested store, executable, binary and relative symlink. Assert protected capture includes `.env` and skips all metadata/root entries. Reject workspace roots inside/equal to/containing registered sources or stores, under a Git-controlled ancestor (a `.git` directory **or file**, even if source is unregistered), known sync roots, unsupported/read-only mounts, relative config and symlink leaf. Cross-project roots and later registration must respect existing workspace/cache boundaries. Under the same project lock, a placement valid at acceptance can become invalid before publication; test a replaced parent directory with a sentinel file and require failure with sentinel unchanged.

```python
before = {p.relative_to(self.catalog.store): p.read_bytes()
          for p in (self.catalog.store/'objects').rglob('*') if p.is_file()}
with patch.object(repo.object_store, 'put_bytes', side_effect=AssertionError('write')):
    report = compare_tree(repo, checkpoint.id, workspace, profile)
self.assertEqual(report['changed'], [{'path': 'run', 'reasons': ['mode']}])
self.assertEqual(before, {p.relative_to(self.catalog.store): p.read_bytes()
                         for p in (self.catalog.store/'objects').rglob('*') if p.is_file()})
```

Also block `put_file` in that test. Cover additions/removals/type changes/link target changes, CRLF and large chunked files, concurrent file replacement -> `CAPTURE_CHANGED`, missing/replaced/remounted workspace -> unavailable without mkdir. Do not recursively resolve symlinks while comparing literal link text.

- [ ] Run focused discovery for `test_task_paths.py` and `test_task_comparison.py`; expect missing path/comparison helpers.
- [ ] Implement strict shared config parsing rather than separate incompatible config readers. Extend existing XDG paths compatibly; no runtime fallback. Create only validated private Solace root/project parent directories with mode0700, using nofollow descriptor traversal and exclusive leaf creation. Hold/recheck ancestor descriptors and mount identities across preparation/publication; do not use a string-only `.resolve()` check followed by unchecked path mutation. Extend `CoreService(registry, *, workers=8, config: CoreConfig|None=None)` compatibly; absent config loads the current XDG data-root config, and tests inject one. Pass that same configuration to storage/workspace validation. Existing registration/add/relink must reject overlap with workspace/cache roots; a new source must not swallow a tracked workspace. Preserve nested stores for ordinary source capture. Exclusion checks use relative components plus physical identities and path ancestry; do not rely only on fnmatch's treatment of `**`.

```python
parts = relative.split('/')
if '.git' in parts or '.solace_vault' in parts:
    return True
# Also exclude configured/canonical Solace-owned roots and their identities.
```

Produce `ProjectRepository.inspect_tree(exclusions=(), *, excluded=None) -> tuple[list[FileEntry],list[str]]`: use empty `chunk_digests` for observed entries because only whole-file digests are compared. Implement a shared scanner mode that hashes regular bytes without ObjectStore writes and uses existing descriptor/mtime/ctime race checks. Do not reuse `_scan` as-is: it writes objects. Compare modes for regular files; compare symlink target bytes with surrogateescape and ignore unenforceable Linux symlink permission changes. Make cached status reads non-scanning and clearly dated. Keep empty-directory omission explicit.

- [ ] Run new path/comparison suites plus existing capture safety/storage tests. Expect zero object/catalog changes from direct comparison, exact diff reasons and unchanged sources.
- [ ] Commit named files: `feat: validate workspace placement and compare trees without capture`.

## Task 4: Verified immutable bases and isolated materialization

**Files:** Extend `solace_core/workspace_trees.py`; create `tests/solace_core/test_task_materialization.py`; reuse engine verified-object and safe-restore primitives without changing legacy restore behavior.

**Interfaces:** `validate_workspace_links(entries: list[FileEntry], targets: dict[str,bytes]) -> None`; `BaseCache(catalog, paths).ensure(repo, checkpoint: str) -> JSON` returns `{cache_id,path,identity,manifest_sha256,logical_bytes}` only after full verification and cache catalog commit. `WorkspaceBuilder(catalog, paths).prepare(repo, checkpoint: str, intent: JSON) -> JSON` returns staged identity/manifest after cloning/copying; `.publish(intent: JSON, staged: JSON) -> JSON` returns final identity after no-replace rename and directory fsync. `clone_regular(source_fd: int, destination_fd: int) -> str` returns `reflink` or `copy`. All result paths are internal until Task 5's success transaction.

- [ ] Write RED tests using a real temporary repository with binary >1MiB, executable, dotfile and internal links. Materialize two destinations, modify the same file in one and assert sibling/cache/source bytes unchanged and regular `(st_dev,st_ino)` pairs distinct. Force FICLONE to return `EOPNOTSUPP`, `ENOTTY`, `EXDEV`, `EINVAL`, `ENOSYS` and verify copy path; inject `EIO`, `ENOSPC`, `EDQUOT`, `EACCES` and require failure, not fallback. Verify truncated fallback does not publish. A changed cache byte/mode/identity raises `CACHE_CORRUPT`, leaves final absent and preserves evidence. Existing final directories and broken symlinks must survive untouched.

```python
for target in [b'/outside', b'../../outside', b'bridge/file']:
    with self.subTest(target=target):
        # bridge -> ../outside is present in this checkpoint's target map.
        with self.assertRaises(CoreError) as caught:
            validate_workspace_links(entries, {'link': target, 'bridge': b'../outside'})
        self.assertEqual(caught.exception.code, 'WORKSPACE_SYMLINK_UNSAFE')
```

Use matching entries for each target map. Also reject self/cyclic chains and path components that are symlink parents, accept relative internal and dangling internal links, reject root escape before a subsequent lexical re-entry. Inject ancestor swap and destination-name race immediately before rename; require no write into the substituted directory.

- [ ] Run `python -m unittest discover -s tests/solace_core -p test_task_materialization.py -v`; expect missing builder/cache implementation.
- [ ] Implement bases at `<store>/workspace-cache/<checkpoint-id>/<cache-id>/tree`, metadata in the catalog. Cache IDs are UUIDs, names contain no title/prompt. Build in an exclusive private staging directory from verified chunk objects, validate manifest paths and link graph before writing, hash the complete reconstructed files, then seal regular files to `original_mode & ~0o222` and directories0500. Symlinks retain exact bytes. The canonical manifest digest includes path/kind/original mode/size/file digest/target bytes. Verify sealed cache against that transformed mode expectation on **every** reuse; never treat read-only mode as proof of integrity. Publish no-replace and fsync; only then insert `workspace_caches`. Orphan cache staging/finals are evidence, not automatically adopted or deleted. Corrupt caches hard-fail with guidance; explicit rebuild is not needed in this slice.

```python
try:
    fcntl.ioctl(destination_fd, 0x40049409, source_fd)  # Linux FICLONE
except OSError as error:
    if error.errno not in {errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV,
                           errno.EINVAL, errno.ENOSYS}:
        raise
    # New, exclusively owned destination only: truncate/reset offsets, copy,
    # hash/check size, restore original mode, fsync, then reverify full tree.
```

Never use `os.link`, `copytree(copy_function=os.link)`, or expose the cache tree to an agent. Restore manifest modes in mutable clones; compare full tree before publication. Resolve symlink graph component by component with visited-link detection, never host `Path.resolve()` through link targets. Revalidate source/store/cache/staging/destination-parent identities, then rename using held directory descriptors with RENAME_NOREPLACE; a changed named ancestor aborts rather than publishing into a detached directory. Fsync both affected parent directories. No broad exception cleanup that removes uncertain files.

- [ ] Run materialization, path, comparison and existing engine restore suites; expect identical verified manifests for reflink/copy and no hardlinks or overwrites.
- [ ] Commit named files: `feat: materialize verified isolated workspaces with safe copy fallback`.

## Task 5: Durable start, workspace capture and ready publication

**Files:** Create `solace_core/task_operations.py`, `tests/solace_core/test_task_operations.py`, `tests/solace_core/test_task_lineage.py`; modify `solace_core/tasks.py`, `solace_core/service.py`, `solace_core/protocol.py`, `solace_core/lineage.py`.

**Interfaces:** `TaskOperations(service, catalog)` exposes `.accept(request: JSON) -> JSON` for task.start/task.ready/workspace.checkpoint/workspace.status(refresh=true)/workspace.relink and later recovery.resume, `.run(operation_id: str, request: JSON) -> None`, `.complete_capture(db, operation_id: str, checkpoint: Checkpoint, *, ready: bool) -> None`. Acceptance returns `{operation_id}`; final start result is `{task_id,workspace_id,path,base_checkpoint,head_checkpoint,revision,warnings}`; capture result includes `checkpoint_id,task_id,workspace_id,revision,file_count,logical_bytes`. TaskCatalog owns request/claim/revision SQL; builder owns filesystem work; Lineage owns context/head references. Wire TaskCatalog create/list/show and prompt/policy methods, plus strict API validators for methods implemented so far, so service tests exercise real admission. Task 7 completes the transition and relink guards; do not advertise capabilities until Task 9 completes the surface.

- [ ] Write RED API tests using `TaskCase`. Populate source, create source checkpoint, parent, two children. Start each at distinct parent revisions; edit child A, ready it, then start/capture child B. Assert B's baseline is captured from the parent, never A; each next parent matches its workspace head. Root start uses source head as explicit parent and leaves source head/usage unchanged. Explicit `from_checkpoint` accepts parent history and rejects sibling/source/other-store IDs. Assert profile/protected metadata disclosure and source Git/vault byte identity.

```python
child = self.create_task(parent=parent, expected_parent_revision=self.show(parent)['revision'])
started = self.start(child, expected_parent_revision=self.show(parent)['revision'])
path = Path(started['path']); (path/'shared.txt').write_text('child only')
old = self.show(child)
accepted = self.mutate('task.ready', child)
outcome = self.wait_operation(accepted['operation_id'])
self.assertEqual(outcome['state'], 'succeeded')
self.assertEqual(self.show(child)['state'], 'ready')
self.assertNotEqual(self.show(child)['workspace']['head_checkpoint'], old['workspace']['head_checkpoint'])
self.assertEqual((self.root/'shared.txt').read_text(), 'source')
```

Patch the real capture publication hook to throw after writing task result/head/event but before commit. Assert no new cp/context/result/head/event/success exists and previous task state remains active/blocked. Queue two conflicting revisions concurrently: exactly one claim/admission, no hidden second head. Child start claims both child and parent; block/ready/cancel/new child creation cannot bypass either claim. Replay still returns accepted ID while claimed, after completion and after source/workspace becomes unavailable. A task show during preparation has no usable path. Root `--from` preserves a cancelled task's eligible checkpoint and rejects active-task-only, unpublished, mismatched-profile and other-project checkpoints.

- [ ] Run discovery for `test_task_operations.py` and `test_task_lineage.py`; expect missing task operation dispatch.
- [ ] Implement admission under coordinator lock: receipt lookup, schema requirement, registered project, revision/state/parent/profile checks, current root/store/workspace identities, then one transaction for queued operation, claims and intent. Reserve deterministic final `<workspace-root>/<project-id>/<task-id>` and operation-specific sibling `.staging-<operation-id>`; forbid existing leaf. Persist expected task/parent revisions, old heads, source/store/parent identities and profile in `expected_json`, no prompt/title. Insert intent before creating staging; record exclusive staging identity before populating. Source/parent files need paused writers for a stable capture; changing files return CAPTURE_CHANGED rather than an application-consistency promise.

Root start without `--from` captures a **prepared, unpublished** baseline with explicit source head, fixed profile and reserved workspace ID. Root explicit `--from` reuses an eligible cancelled-task checkpoint without recapture or changing its provenance. Default child start captures a prepared checkpoint of parent's workspace with its previous head; explicit child baseline reuses an eligible published parent checkpoint. The prepared checkpoint/context and `intent.base_checkpoint` commit together; they are retained internal artifacts, not advertised results. This preliminary immutable capture is distinct from final workspace publication and does not advance any visible head.

After builder verifies and renames, persist published identity/phase if possible. Revalidate identities/revisions/claims, then one final transaction inserts workspace, publishes prepared context, appends child base/head refs, updates parent head/ref/revision for a new child-baseline capture, increments child revision/state active, marks intent committed, completes operation/event, releases claims. Filesystem rename and this transaction are separate; Task 6 handles the gap. A parent's baseline has owner parent/reserved workspace; a default child-start checkpoint is owned by the **parent** workspace and referenced as child's base. Child's first ready/manual checkpoint parents that shared baseline and is owned by child.

```python
repo = ProjectRepository.open_existing(workspace_path, catalog.store)
message = p['message'] if method == 'workspace.checkpoint' else f'Ready {task_id}'
repo.create_checkpoint(message, profile['exclusions'],
    parent_id=old_head, excluded=protected_rule,
    publication_hook=lambda db, cp: operations.complete_capture(
        db, operation_id, cp, ready=(method == 'task.ready')))
```

Ready/manual task capture scans outside transactions, then publishes checkpoint/context/head refs/result refs (ready only)/task revision/operation/event/released claims in the engine hook's transaction. No prompt-derived message. Manual capture accepts active/blocked; ready accepts active/blocked with no pending children. Resume retains all result refs. Status refresh uses no-write comparison, saves only observation/outcome/event and releases claim without changing lifecycle/revision. Ordinary status/show uses dated cached observation. Worker failure releases claims unless unresolved start/relink publication needs explicit recovery; interruptions preserve them for Task 6.

- [ ] Run new operation/lineage suites and all core/engine suites; require distinct workspaces and transactional ready failure proof before commit.
- [ ] Commit named files: `feat: publish isolated task starts and atomic ready results`.

## Task 6: Explicit recovery, deferred storage and guarded staging cleanup

**Files:** Extend `solace_core/task_operations.py`, `solace_core/catalog.py`, `solace_core/service.py`, `solace_core/protocol.py`; create `tests/solace_core/test_task_recovery.py` and `tests/solace_core/task_crash_worker.py`.

**Interfaces:** `TaskOperations.recover_interrupted() -> list[str]`, `.inspect_recovery(operation_id: str) -> JSON`, `.abandon(request: JSON) -> JSON`, `.cleanup_staging(request: JSON) -> JSON`. Resume goes through `.accept/.run` with a new operation ID linked via `workspace_intents.recovery_operation_id`; the original remains interrupted forever. Cleanup preview/apply uses one method and a token. Recovery retries when original store returns or validated project relink succeeds; reads/receipt lookup do not recreate anything.

- [ ] Write RED subprocess crash tests, terminating with `os._exit` at actual patched function boundaries, never a public production crash flag. Test: intent committed/no staging; owned staging created/partially copied; immediately after no-replace rename before phase write; after phase write before final transaction; inside final transaction before commit; commit succeeded/reply lost; ready hook before/after commit. Inspect SQLite after restart, not only operation responses. For the rename gap compare the final directory to the previously persisted staging `(mount,dev,ino)` and baseline; do not trust existence. Assert active/blocked states and old heads preserved unless committed, replay original request yields same operation, and final paths remain hidden until successful explicit resume.

```python
self.service.close()
with self.crashed_start('after_rename') as evidence:
    evidence.store.rename(evidence.offline_store)
    self.service = self.reopen_service()
    service = self.service
    self.assertFalse(service.recovery[-1].get('recovered', False))
    evidence.offline_store.rename(evidence.store)
    intent = self.call('workspace.recovery.show', project=self.project,
                       operation=evidence.operation)
    self.assertEqual(intent['operation_state'], 'interrupted')
    self.assertFalse(intent['workspace_advertised'])
    self.assertTrue(evidence.final_path.exists())
```

Define `crashed_start(phase)` and `reopen_service()` on the recovery test class using the worker subprocess/temporary fixture, with explicit fields shown here; bound subprocess waits/timeouts and always close services. Also test mismatched final inode, moved mount, modified final content, missing cache, stale task revision, unavailable source, ordinary capture interruptions releasing claims, repeated resume/abandon receipts, abandoned preserved files, and cleanup preview token invalidated by staging replacement or symlink insertion.

- [ ] Run `python -m unittest discover -s tests/solace_core -p test_task_recovery.py -v`; expect missing explicit recovery and intent-aware reconciliation.
- [ ] Replace generic task-operation startup recovery with intent-aware reconciliation. Mark abandoned queued/running operations interrupted once; leave external state unchanged. Captures/status with no unresolved filesystem publication release claims. Uncommitted start/relink intent retains claims and RECOVERY_REQUIRED to prevent fresh mutation; read/status/recovery inspection remains usable. Deferred unavailable catalogs stay in pending recovery until accessible. Task claims/revisions are examined durably, not inferred from thread cleanup.

On resume admission, transfer existing intent claims from the interrupted owner to the new recovery operation in the same queued transaction; reconcile linked recovery operations through their original intent if they also crash. Refuse simultaneous resume/abandon. Resume never repeats the original scan/clone automatically. If a complete recorded staging tree exists, verify identity/baseline and publish it no-replace. For start, if matching final exists, verify its recorded staging/final identity and complete baseline manifest before adopting. For relink, revalidate the recorded candidate identity and selected checkpoint, recompute the dirty report and preserve its writer-pause acknowledgement; dirty content is permitted and never adopted as a new head. If neither complete tree exists, refuse resume with guidance to abandon and use a new start request. External edits before recovery adoption fail comparison; keep evidence. Source/store and all relevant parent identities must match or be explicitly relinked; do not bypass revision checks. New recovery success transaction commits original intent/task/workspace/head changes and recovery outcome atomically; original interrupted outcome remains immutable. `task.start` retry continues returning the original interrupted ID, directing the caller to recovery.

Abandon appends an event, marks intent abandoned, completes its body-free receipt and releases claims without changing task lifecycle or deleting any files. If a renamed final remains, a later start cannot overwrite it; explicit operator relocation or a new task is needed. For relink abandon preserves old workspace identity. No cleanup command can delete final paths or caches.

Cleanup preview only accepts an abandoned intent's **unpublished staging path** with recorded identity. Token hashes operation/staging canonical path/mount/device/inode and descriptor-scanned entry identities/types. Apply rechecks token, no in-process operation, no active claim and no published reference. Delete only through held nofollow directory descriptors, refuse changed entries, never follow links, and fsync parent. Partial deletion due a race is reported precisely, not retried recursively. A path lacking recorded identity is preserved for manual inspection.

- [ ] Run crash/recovery, operation, receipt, socket and independent-review regression suites. Require actual process-loss evidence on both sides of filesystem/database publication.
- [ ] Commit named files: `feat: reconcile interrupted workspace publication without guessing success`.

## Task 7: Lifecycle summaries, relink and project lifecycle guards

**Files:** Extend `solace_core/tasks.py`, `solace_core/task_operations.py`, `solace_core/service.py`, `solace_core/protocol.py`; create `tests/solace_core/test_task_lifecycle.py` and `tests/solace_core/test_task_concurrency.py`.

**Interfaces:** `TaskCatalog.transition(request)` implements the public state table; `.assert_project_unregisterable(db) -> None`; `.summary(db, task_id: str) -> JSON`; `.assert_revision(db, task_id: str, expected: int) -> JSON` returns the existing row or raises a stable conflict. `TaskOperations` adds workspace.relink admission/run. Synchronous guards and all async completion hooks consume the same claims/revisions, never divergent state rules.

- [ ] Write RED tests for every allowed/forbidden edge: created->active via start only, active->blocked, blocked/ready->active via resume, active/blocked->ready via capture, any noncancelled->cancelled if unclaimed and children cancelled; cancelled has no reopening. Parent ready refuses created/active/blocked children; cancelled children do not block, ready children remain independent results. Parent summaries distinguish own state, child counts and awaiting_integration. Resuming ready preserves prior results and next capture's ancestry. Two same-revision clients racing block/resume/ready/cancel get one success and one TASK_REVISION_CONFLICT or TASK_BUSY, never lost state.

```python
request = self.request('task.resume', project=self.project, task=task,
                       expected_revision=self.show(task)['revision'])
first = self.service.handle(request)
self.assertTrue(first['ok'], first)
self.mutate('task.block', task, reason='waiting')
self.assertEqual(self.service.handle(request)['result'], first['result'])
self.assertEqual(self.show(task)['state'], 'blocked')
```

Use a ready task for the first resume. Test parent/child start vs parent ready/cancel, child create vs parent ready, project unregister during queued/interrupted/active work, and reads immediately after commit but before worker cleanup. Unregister with all cancelled tasks preserves files/store and permits retained receipt/operation/task inspection without new writes or prompt body exposure through generic views. Missing/corrupt schema-2 store must not let unregister bypass unknown task references.

Relink tests require blocked/ready, exact canonical `confirmed_path`, `writers_paused=true`, no claim, eligible task checkpoint, safe placement and link graph. Report dirty added/changed/removed paths while recording the new identity; don't claim verified provenance. A failed relink preserves old workspace. Task start/capture/status must reject replaced workspace identity until relink succeeds.

- [ ] Run focused lifecycle/concurrency discovery; expect missing transition/guard/relink cases.
- [ ] Implement lifecycle SQL/event/receipt in one transaction and increment each changed row once. Parent child-count changes are observable from revisions when topology/head changes; do not increment every ancestor for unrelated child text or observations. Parent ready/cancel rechecks child records and claims at admission and publication. Unregister refuses any noncancelled tasks or unresolved claims/intents; for unavailable/unknown schema stores fail closed with reconnect/doctor guidance rather than the old permissive unknown-task unregister path. Project relink remains the recovery route: validate the new existing store UUID/schema, reject durable owned work unless this is the explicit return of an unavailable store, then reconcile before accepting new work. Never resurrect an unregistered project to replay receipts.

Relink performs read-only comparison outside write transactions while claiming task, persists candidate path/identity/selected checkpoint/old identity in a relink intent, then revalidates and commits new workspace path/mount/device/inode plus dated dirty report/task revision/outcome/event/release. No file move or restore occurs. Keep base/head unchanged: selected checkpoint is a comparison reference, not a request to rewrite history. Interrupted relink requires explicit resume/abandon; resume rechecks acknowledgement/request and current candidate identity. Reusing the same request replays its original operation.

- [ ] Run lifecycle/concurrency/recovery and existing registry/review regression suites; require no unregister/relink escape from durable task ownership.
- [ ] Commit named files: `feat: enforce task lifecycle and preserve workspace ownership across relink`.

## Task 8: Prompt maintenance, immediate expiry and privacy across replay

**Files:** Create `solace_core/maintenance.py`, `tests/solace_core/test_task_maintenance.py`; extend `solace_core/prompts.py`, `solace_core/service.py`, `tests/solace_core/test_task_prompts.py`.

**Interfaces:** `PromptMaintenance(service, *, interval_seconds: float=60.0, batch_size: int=100)` exposes `.start() -> None`, `.run_once() -> None`, `.close() -> None`. It uses a daemon thread and `threading.Event.wait`, closes/join on service shutdown, never occupies project worker slots indefinitely. `PromptStore.purge_expired(limit=100)` is idempotent and returns logical body count removed.

- [ ] Write RED fake-clock tests at expiry-1microsecond, exact expiry and expiry+1. Immediate reads must hide expired body while purge is deliberately unavailable; metadata reports expired. Startup and each run remove <=100 bodies per project in a short coordinated transaction with events containing ID/reason only; remaining bodies are still unreadable. A policy change leaves existing expiry unchanged. New prompt delete after expiry and request replay after delete cannot restore text. Unavailable stores do not prevent other projects' purge; returning storage is purged on the next successful catalog reconciliation or maintenance turn. Maintenance shuts down without touching files after close.

```python
clock.value = expires_at
with patch.object(prompts, 'purge_expired', side_effect=OSError('offline')):
    result = prompts.read(task)
self.assertEqual(result['status'], 'expired')
self.assertNotIn('body', result)
self.assertNotIn(canary, json.dumps(self.show(task)))
```

Use a small callable fake UTC clock assigned to the existing constructor clock interface; do not patch wall time globally. Capture request handling, worker errors and maintenance logger output with the canary in prompt; assert no raw params/body/fingerprint in logs/events/watch/operation receipts. Explicit prompt.show is the only body-bearing response and is never durably cached.

- [ ] Run focused maintenance/prompt discovery; expect missing periodic/startup purge.
- [ ] Implement startup purge after schema-2 recovery, and once-per-60-second bounded sweeps of registered schema-2 catalogs using project coordination. Skip schema 1 without migration. Skip unavailable stores and retain a retry indication without logging body/error strings from requests. Expiry transaction sets status expired/body NULL/redacted_at and emits one `task.prompt.redacted` event per changed task. Increment task revision on effective stored redaction, but never change external state/head or release a live operation claim; if a task claim is active, defer that task's physical purge while reads still redact immediately. Do not make expired prompt deletion a prerequisite to operation replay. Retired stores do not get background writes; explicit prompt reads still apply expiry. Document logical-erasure limits, backup retention and external copies.

- [ ] Run maintenance/prompt/lifecycle/concurrency suites with injected clock and existing shutdown tests. Expect exact expiry and no extra event on repeat sweep.
- [ ] Commit named files: `feat: expire retained prompts without leaking them through replay or logs`.

## Task 9: Complete CLI, capability negotiation and operator guidance

**Files:** Modify `solace_core/cli.py`, `solace_core/protocol.py`, `solace_core/service.py`, `solace_core/diagnostics.py`, `packaging/install-core.py`, `tests/solace_core/test_cli.py`; create `tests/solace_core/test_task_cli.py`, `tests/solace_core/test_task_protocol.py`, `docs/task-workspaces.md`; update `docs/local-core.md`.

**Interfaces:** All public methods/grammar defined above, no aliases to nonexistent completion/merge actions. Existing `CoreClient` request/subscription contracts remain unchanged. CLI capability check caches only per invocation; schema checks come from current project state. Doctor reads schema/version/task counts/availability and reports rollback incompatibility without creating catalogs or expiring prompts itself.

- [ ] Write RED protocol tests covering exact required/optional keys, wrong types/nulls/bools, extra fields, malformed IDs, missing parent revision, forbidden child exclusions, absolute-path checks, exact confirmation, positive days, invalid UTF-8/lone surrogate and full encoded frame overflow. Assert body absent from all error messages. Table-drive one valid request per new method and one invalid permutation per parameter class.

Write CLI socket/subprocess tests for create/start/two children/block/resume/ready/status/list/watch/prompt/delete/policy/upgrade/recovery; use temporary XDG roots/socket/registry, never the installed service. Preserve bytes:

```python
prompt_path.write_bytes('é\r\nno final newline'.encode('utf-8'))
created = run_cli('task', 'create', project, 'work', '--prompt-file', str(prompt_path), '--json')
shown = run_cli_bytes('task', 'prompt', 'show', project, created['task_id'])
self.assertEqual(shown, prompt_path.read_bytes())
```

Define `run_cli` and `run_cli_bytes` in `test_task_cli.py` using `subprocess.run(..., check=True, capture_output=True, env=isolated_env, timeout=15)` against a local test server; first decodes JSON, second returns stdout bytes. Against a fake old core lacking capabilities, new commands return CORE_UPGRADE_REQUIRED and old checkpoint commands still succeed. Stale CLI revision returns conflict without retry. `--wait` timeout shows durable operation ID; `--watch` replays events without leaking prompt. Invalid prompt never appears in stderr.

- [ ] Run CLI/protocol focused discovery; expect missing parser/method/compatibility behavior.
- [ ] Complete strict typed validators, CLI parser and human/JSON rendering. Print workspace path only for committed available workspace, with protected exclusions, empty-directory limit and external-writer warning where handoff matters. Ready shows “saved; awaiting review/integration”, not source-updated. Storage output separates retained CAS bytes, workspace logical bytes, sealed cache logical bytes and `unique_physical_bytes: null` / `shared_extent_accounting: unavailable`; use cached observations for unrefreshed workspace sizes. Report missing observations/availability rather than inventing zeros. Source usage remains source-head-specific. Include exact base/head/purpose/owner/profile for all-workspaces history.

Document migration preview/token/apply, schema backup location, runtime rollback compatibility, task revisions and no automatic retries, paused-writer responsibility, symlink/placement failures, explicit recovery/resume/abandon, guarded staging cleanup, no delete/GC and prompt logical retention. Installer preview adds core schema support/create versions and an unconditional notice that runtime rollback does not downgrade databases; it must not scan or upgrade project catalogs as a side effect. The new doctor shows schema-2 references; a rolled-back old runtime reports unsupported schema and preservation guidance rather than pretending it can read new task tables. Neither performs repair writes. Add installer-preview regression to `test_task_cli.py` using the script's existing default preview mode with temporary `HOME`, `XDG_DATA_HOME` and `XDG_CONFIG_HOME` (the installer has no prefix flag).

- [ ] Run focused CLI/protocol plus existing CLI/socket/activation tests. Validate examples against `--help`; preview installation in a temporary prefix and verify the installed runtime remains untouched.
- [ ] Commit named files: `feat: expose task workspace workflows through compatible local CLI and API`.

## Task 10: Whole-branch verification, independent review and install gate

**Files:** Create `tests/solace_core/test_task_acceptance.py` and `docs/engineering/2026-10-15-task-workspaces-review-and-acceptance.md`; modify only defect-owning production/tests/docs files if verification reveals a specific failure.

**Interfaces:** The complete public CLI/API above, real Unix socket and temporary source/stores/workspaces. Acceptance is scripted, repeatable and restricted to disposable fixtures. Installation remains a separate explicitly approved action after review; this task prepares the reviewable result and commands.

- [ ] Write an end-to-end acceptance test using a real temporary server and CLI: source with Git pointer/vault/hidden/binary/executable/internal link; manual source checkpoint; parent start; two child starts; modify same filename independently; save ready; parent own snapshot after children ready; inspect awaiting_integration with distinct result refs and unchanged original. Resume one ready child and save another result, preserving earlier refs. Verify source-only and all-workspaces listings, source usage, no shared mutable inodes, no prompt in journal-style captured logs/events/receipts. Add a second disposable project to prove a blocked materialization does not block it. Include subprocess-loss/recovery evidence from Task 6 rather than mocking recovery outcomes.
- [ ] Run `python -m unittest discover -s tests/solace_core -p test_task_acceptance.py -v`; if it exposes a real integration failure, record the failing assertion and fix its owning component with a regression first. Do not manufacture a failing assertion for already-correct behavior.
- [ ] Run the complete required checks, separately:

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

Record command, exact count, result and evidence paths. Existing 122 tests must pass alongside new tests; explain any updated compatibility assertion (schema2 supported,3 refused), never silently remove prior coverage. Do not claim full interactive GUI or actual logout testing from Tk unit results. No software installation or live catalog migration during these tests.
- [ ] Request the approved independent whole-branch review using `superpowers:requesting-code-review`. Give reviewer baseline `40c8d22`, final HEAD, approved spec, this plan, changed-file inventory and test evidence. Review prompt must emphasize migrations/backup durability, namespace replay/privacy, explicit ancestry, claims/races, link/ancestor attacks, reflink fallback, crash gaps and truthful storage accounting. Resolve verified findings with focused regression tests; rerun affected suites, then final full checks once. Document unresolved limitations and review disposition without claiming approval prematurely.
- [ ] Commit acceptance/report and specific reviewed fixes in scoped commits. End with a concrete preview of the candidate runtime, current installed runtime, immutable install backup strategy, schema compatibility and disposable installed acceptance commands. The preview must identify exact real paths/IDs from that execution; do not reuse historical acceptance IDs. Ask for installation approval only after the reviewed candidate passes. If authorized, use the existing installer/backup flow, then live same-user socket activation and disposable schema1-upgrade/task/restart/privacy acceptance; record rollback commands. Never downgrade the live project database as part of runtime rollback. Do not publish remotely or integrate to main merely because tests passed.

## Self-review and acceptance mapping

| Approved spec section | Owning tasks / evidence |
| --- | --- |
| 1–3 outcome, direct-child workflow, CLI | 2, 5, 7, 9, 10 |
| 4 lifecycle, external state, ready/results | 5–8, subprocess crash/transition tables |
| 5 explicit ancestry/profiles/source usage | 1, 3, 5, 9; sibling-latest regression |
| 6 placement/isolation/link/cache/status/storage | 3–5, 9; inode/copy/manifest and no-write diff tests |
| 7 intent/replay/recovery/deferred stores/cleanup | 5–7; real crash points and identity-guarded cleanup |
| 8 exact prompt/default/overrides/privacy/expiry | 2, 8, 9; fake clock and canary leakage assertions |
| 9 explicit upgrade/API/revisions/compatibility | 1, 5–9; backup rollback, old core and race tests |
| 10 acceptance/review/install boundary | 10; full suites and explicit install gate |
| 11 exact interfaces/schema/cadence/error decisions | Data/API contracts and Tasks 1–9 |

Self-review must check every API method has validation, a dispatch owner, a body-free receipt policy and a test; every revision-bearing mutation replays first; every stored head update names an explicit parent; every filesystem publication has durable intent and identity verification; every prompt read enforces expiry independent of purge. Keep unresolved design changes out of implementation until reviewed. Execute inline only after the user confirms this written plan.

Plan self-review completed 2026-10-15: all eleven spec sections map to owning tasks; all five review-focus cases have regression owners; task/interface names and revision/replay ordering were checked. Artifact validation passed for 17 fenced blocks, Python snippet syntax, relative links, ten task/checklist structures, placeholder scan, executable SQLite DDL, foreign keys, positive retention constraints, repeated relinks and the single-live-intent guard. This validates the written plan only; it does not claim product tests or implementation are complete.
