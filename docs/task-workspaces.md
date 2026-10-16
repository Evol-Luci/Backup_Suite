# Tasks and isolated workspaces

The installed local core supports parent tasks and direct children. External tools do the work in separate directories. Solace records requests, exact baselines and immutable ready results. A ready result awaits review/integration; it does not change the original project or combine child files. See the [review and installed acceptance report](engineering/2026-10-15-task-workspaces-review-and-acceptance.md) for verification and review limitations.

## Upgrade an existing store

New projects use core schema 2. Existing schema-1 projects retain checkpoint and doctor access; task commands require an explicit upgrade. Engine and registry schema versions remain 1.

```bash
solace project upgrade PROJECT --preview --json
solace project upgrade PROJECT --apply --plan-token TOKEN --request-id REQUEST_UUID --json
```

Read the preview and use its exact token. Apply refuses a stale preview or pending operations. It saves a consistent SQLite backup at `STORE/schema-backups/REQUEST_UUID/checkpoints.sqlite3`, verifies it, and commits schema changes and their receipt together. Original files and immutable objects are preserved. A failed migration preserves its backup evidence and rolls back SQL changes. Repeat the same apply request/token/UUID to retrieve its original receipt.

Runtime rollback does not downgrade project databases or erase task/workspace references. An older core refuses schema 2. Preserve the store and return to a compatible runtime; do not replace its database with an old backup over new task history. Doctor reports versions and this boundary. Installation preview never migrates a project.

## Give tools separate copies

```bash
solace task create PROJECT 'Improve validation' --prompt-file request.txt --json
solace task start PROJECT PARENT --wait --json
solace task create PROJECT 'Parser cases' --parent PARENT --json
solace task start PROJECT CHILD --wait --json
solace workspace status PROJECT CHILD --refresh --wait --json
solace workspace checkpoint PROJECT CHILD 'Before experiment' --wait --json
solace task block PROJECT CHILD --reason 'Need an input format decision'
solace task resume PROJECT CHILD
solace task ready PROJECT CHILD --wait --json
solace task show PROJECT PARENT --watch --json
solace task list PROJECT --parent PARENT --json
solace checkpoint list PROJECT --all-workspaces --json
```

Create records without scanning files. Start and capture commands return durable operation IDs; `--wait` reports the terminal operation record, whose `result` contains the published path or checkpoint. Waiting times out after 60 seconds without cancelling work; inspect the printed operation ID. A pending/interrupted preparation has no usable advertised path. Give the external tool the committed available workspace path, then execute its request there. Solace does not launch, stop, or sandbox that tool.

A default parent start captures source using the visible task profile and the recorded source head. A default child start checkpoints its parent's current workspace, advances the parent's workspace head, and uses that exact checkpoint as its baseline. Child `--from CP` selects published parent workspace history with the same profile; sibling/source-only checkpoints are refused. A root `--from CP` can reuse matching published history referenced by a cancelled task, without reopening it. Every ready result follows its own head and remains retained after resume.

Task states are created, active, blocked, ready and cancelled. Parent summaries show own state and separate child counts; ready parents with ready children report awaiting_integration. Parent ready requires children ready/cancelled. Cancel children explicitly before cancelling a parent. Cancellation preserves files and immutable results and does not terminate external processes. Unregister requires every task cancelled and recovery resolved; unavailable stores must return or be explicitly relinked before ownership can be checked. Retained receipts and task inspection remain readable after unregister.

Commands automatically inspect current revisions. Conflicts require inspection and a new decision; they are never silently retried. With an explicit `--request-id`, pin `--revision N` and, for child create/start, `--parent-revision N` so a retry sends identical arguments. Policy mutations similarly require a pinned policy revision with explicit request IDs. API clients always supply expected revisions. Preserve the original UUID, actor, method and parameters for replay.

## Placement, profiles and file limits

Default paths are `$XDG_DATA_HOME/solace/workspaces/PROJECT_ID/TASK_ID` (`~/.local/share` by default). Optional `$XDG_DATA_HOME/solace/core-config.json`:

```json
{"sync_roots":["/absolute/synchronized/path"],"workspace_root":"/absolute/local/workspaces"}
```

Workspaces must be outside registered source/store trees and Git-controlled ancestors, on supported writable local storage. Known/configured sync roots are refused; arbitrary background sync cannot be detected automatically. Missing or replaced workspaces are unavailable and are never recreated implicitly. Nested source stores remain supported, but cannot contain workspaces.

An empty `.git` directory does not make an ancestor a Git working tree. Populated directories, pointer files, symlinks and unreadable Git markers are refused conservatively. Solace never removes or changes those markers.

New source/store locations and workspace reservations also protect retained workspaces and preparations, including other projects and records retained after unregister or a workspace-root configuration change. Placement checks require those retained catalogs to be readable. Reconnect an unavailable recorded store, or explicitly relink its still-registered project, before reserving new locations. Ordinary reads and captures in already owned available workspaces do not perform a new reservation.

Protected task exclusions remove `.git` files/directories at every depth, `.solace_vault`, and Solace-owned trees. Ordinary dotfiles remain included. Root `task create --exclude GLOB` records additional exclusions; children inherit the profile and cannot override it. Profile changes require a new task. Manual source checkpoints retain their existing behavior; default listing and source usage use source history, while `--all-workspaces` exposes task owners/profiles. Pre-upgrade checkpoint exclusions are labelled unknown.

Only relative internal symlinks can be materialized; absolute, escaping or cyclic chains are refused before publication. Exclude or replace unsafe links explicitly. Workspaces use reflink copies when supported and verified ordinary copies otherwise, with separate mutable inodes and no hardlinks. Immutable materialized bases remain internal to the store; corruption prevents reuse. Empty directories and special files are not preserved by the existing engine. Pause external writers for stable captures; Solace does not promise application-consistent multi-file snapshots.

Ordinary workspace status returns a dated observation; `--refresh` requests a scan without storing new objects or checkpoints. Observations identify their comparison checkpoint and show stale when the head changed. Storage separates CAS file bytes from cache/workspace logical sizes. Reflinks can share extents: unique physical usage is reported unavailable, and retained references are not advertised as safe reclaimable space. Workspace/cache deletion and object GC are deferred.

Task views and operation results contain bounded comparison samples, exact `counts`, an `observation` token, and a `truncated` flag. Full change lists remain in the catalog. Cached status returns a `changes` page with `entries`, `total` and `next_after`; read subsequent pages with the returned token:

```bash
solace workspace status PROJECT TASK --limit 128 --json
solace workspace status PROJECT TASK --after CURSOR --observation TOKEN --limit 128 --json
```

API paging parameters are `after` (nonnegative), `limit` (1–256, default 128), and `observation` (SHA256 token, required after cursor 0). Pages are also capped at 128 KiB. A refresh cannot accept paging parameters. If the cached observation changes between pages, `OBSERVATION_CHANGED` requires restarting at cursor 0. Frame-limit errors retain their request ID and report `RESPONSE_TOO_LARGE`.

Task lists also accept `--after CURSOR --limit N` (default 128, maximum 256). The response has a `tasks` array and `next_after` cursor, or null at the end. Pages are bounded by bytes as well as count, so use the returned cursor even if a page has fewer than N rows. Task cursors follow stable creation order; each row shows its current state, with no promise of a snapshot across pages. `--parent ID` limits every page to that parent's children.

## Recovery and explicit relink

```bash
solace workspace recovery show PROJECT OPERATION --json
solace workspace recovery resume PROJECT OPERATION --task TASK --wait --json
solace workspace recovery abandon PROJECT OPERATION --task TASK --json
solace workspace staging cleanup PROJECT OPERATION --preview --json
solace workspace staging cleanup PROJECT OPERATION --apply --plan-token TOKEN --json
solace workspace relink PROJECT TASK --path /absolute/candidate \
  --confirm-path /absolute/candidate --checkpoint CP --writers-paused --wait --json
```

Core restarts preserve external active/blocked state. An interrupted ready capture keeps the old head/results unless its transaction committed. Start publication spans filesystem and SQLite; a final directory alone is not proof of success. Resume validates recorded identities and complete baseline content before publishing state. A new recovery operation records success; the original stays interrupted. Incomplete or modified trees cannot be adopted as a successful start. Abandon preserves all files and releases ownership. Preserved final paths block a new start from overwriting them.

Cleanup applies only to known, abandoned, unpublished operation staging. Preview tokens cover identities and entries; a changed preview is refused. Final workspaces and caches cannot be removed through this command. Interrupted storage recovery is retried when storage returns or is explicitly relinked.

Workspace relink requires blocked/ready state, no owned operation, exact path confirmation and an acknowledgement that writers are paused. It compares an existing candidate against a task checkpoint and reports dirty content; it does not restore files, move files or replace the saved head. Old directories remain intact. Explicit project-store relink validates persistent identity and verifies relocated caches before reusing them.

Relink refuses another workspace's directory or descendants and any candidate with multiply linked regular files. Copy such files into independent inodes first. Project-store relink records its previous location in the registry receipt; startup reconciles cache paths and recovery identities atomically with a catalog event. A crash between the two databases leaves reconciliation pending and gates new work until it can finish. Replaying a completed relink or upgrade returns its original receipt.

## Exact prompts and logical retention

```bash
solace project prompt-policy show PROJECT --json
solace project prompt-policy set PROJECT days --days 30
solace project prompt-policy set PROJECT retain
solace project prompt-policy set PROJECT none
solace task create PROJECT TITLE --prompt-file request.txt --prompt-retention 7
solace task prompt show PROJECT TASK
solace task prompt delete PROJECT TASK
```

The approved default is 30 days. Project policy applies to future tasks; task creation can override it with retain, none or positive days. Each task's decision is fixed at creation. Day counts must fit the supported UTC date range through year 9999. None never persists a body. UTF-8 prompts are limited to 128 KiB and the complete 1 MiB protocol frame, reject NUL/invalid UTF-8, and preserve line endings and final-newline absence. Human prompt show writes the exact text without an extra newline; JSON show returns availability metadata and includes the body only while available.

Normal views/watch/events/operations/receipts/logs omit bodies. Missing, discarded, deleted and expired states are distinct. Reads hide expired bodies immediately even if storage is offline or purge deferred. Startup and 60-second maintenance sweeps logically remove at most 100 expired unclaimed bodies per project per turn; active claims defer physical purge, never read-time hiding.

Logical deletion is not forensic secure erasure of SQLite free pages, old WAL, migration/user backups, copied prompt files, terminal output or external tools. No prompt replication is introduced. Preserve backup evidence according to your own retention requirements.
