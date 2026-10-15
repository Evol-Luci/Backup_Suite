# Parent/Child Tasks and Isolated Workspaces

**Status:** Approved by the user on 2026-10-15; implementation-plan review is next.
**Base:** Local `main` at `40c8d22`; installed core remains the independently reviewed version.
**Authority:** `docs/specs/local-first-agent-version-engine.md`, especially sections 4–7, and the completed local-core subsystem spec.
**Delivery:** Linux first, existing Python standard-library core, SQLite, local Unix API, and CLI.

## 1. Intended outcome

A person can record a request, start its parent workspace, give independent child tasks separate copies of an exact parent checkpoint, and collect immutable results ready for review. External tools such as Codex and Claude execute the requests. Solace records their observable status and local history.

Success means two agents can edit the same filename in separate child workspaces without changing one another or the original project. Each saved result has an explicit starting checkpoint, task, workspace, and parent relationship. Core restarts preserve external task state, while interrupted Solace-owned preparation/capture operations are recoverable.

This milestone ends at saved results and an honest terminal status of work awaiting review/integration. Text/binary merging, applying results to the parent or project, configured checks, approval-driven completion, and automatic integration belong to the following integration milestone. No command in this slice calls a result integrated or a parent project updated.

## 2. Recommended approach and alternatives

**Recommended:** Durable task records and one managed workspace per started task, with explicit checkpoint ancestry. Add these to the existing core authority and per-project catalog. This carries the approved product model into a usable CLI flow without introducing a second writer.

**Records first, workspace later:** Cheaper initially, but cannot provide the isolation that makes parallel agents useful. Rejected for this milestone.

**Task tracking around Git worktrees:** Provides existing Git tools, but makes Git a required history backend and inherits the workflow the product intends to replace. Defer Git compatibility to its already planned milestone.

The first slice supports root tasks and their direct children. The schema retains parent IDs, but deeper nesting is rejected clearly until its lifecycle and integration behavior is designed.

## 3. Workflow

1. Register a project through the existing CLI.
2. Create a parent task with a short title and optional exact UTF-8 prompt.
3. Start the parent. Solace checkpoints the project using the visible task capture profile, prepares a separate workspace, and returns its absolute path and task ID when preparation succeeds.
4. Create child task records, then start them. By default, each child start captures the parent's current saved workspace files and records that exact checkpoint as the child's baseline. Starting another child never uses a sibling's checkpoint.
5. Run external agents in those returned workspace paths. Solace does not launch or stop them.
6. Agents report active or blocked state; an explicit ready action captures their workspace and publishes a result checkpoint atomically with task state and its event.
7. Inspect results and the parent summary in the terminal. Subsequent integration work will consume these immutable baselines/results.

Illustrative grammar; exact parser/API signatures follow in the implementation plan:

```bash
solace task create PROJECT 'Improve validation' --prompt-file request.txt
solace task start PROJECT PARENT
solace task create PROJECT 'Add parser cases' --parent PARENT --prompt-file child.txt
solace task start PROJECT CHILD
solace task show PROJECT CHILD --json
solace task block PROJECT CHILD --reason 'Need an input format decision'
solace task resume PROJECT CHILD
solace task ready PROJECT CHILD
solace task list PROJECT --parent PARENT --json
solace task show PROJECT PARENT --watch
solace workspace status PROJECT CHILD --refresh --json
solace workspace checkpoint PROJECT CHILD 'Before another experiment'
solace task prompt show PROJECT CHILD
solace task prompt delete PROJECT CHILD
solace task cancel PROJECT CHILD
```

Creating a record does not scan files or create a workspace. Starting and ready capture are long operations and return durable operation IDs first. A queued/running preparation response does not claim the workspace is usable. Task show returns the usable path only after verified publication and its catalog commit.

An explicit `--from CHECKPOINT` child start may use a checkpoint belonging to that parent's workspace history. Cross-project and sibling-owned baselines are refused. Parent creation can precede child planning, but child start requires a prepared parent workspace.

## 4. Task state, result references, and parent summaries

Own lifecycle states are `created`, `active`, `blocked`, `ready`, and `cancelled`. `preparing`, `capturing`, and `interrupted` describe core operations/workspace availability, not an external agent's execution state.

- Create records `created`.
- Successful start publishes the workspace and changes `created` to `active`.
- Explicit block changes `active` to `blocked`; resume changes `blocked` to `active`.
- Ready captures an `active` or `blocked` workspace and changes it to `ready` only when its checkpoint/result/event commit succeeds.
- Resume from `ready` explicitly returns to `active`. Previous result checkpoints remain referenced; they are never overwritten or removed.
- Cancel preserves task records, prompts according to retention, checkpoints, and workspace files. It does not terminate an external process. Cancellation is refused while a core operation still owns the task. A parent with noncancelled children cannot be cancelled implicitly; children must be cancelled explicitly first.
- A cancelled task is not silently reopened. A new task can be created from an eligible preserved checkpoint.

Parent show reports its own lifecycle separately from direct child counts and a computed aggregate label. Outstanding children yield `waiting_for_children` or `blocked`. A parent's own ready snapshot plus saved child results yields `awaiting_integration`, with the child result count. It never reports that child changes are combined. A parent cannot save ready while any child is created/active/blocked; saved child results are still preserved independently.

There is no `complete` or `integrated` transition in this slice. These states are reserved for the later checks/review/integration workflow. Ready is a saved submission, not a claim that the original source has changed.

A core restart leaves `active` and `blocked` external tasks in their last durable state. Missing workspace storage is reported as availability failure alongside that state. An interrupted core capture does not erase the last successful task checkpoint or label the agent failed.

## 5. Checkpoint ancestry and capture profiles

The current engine selects the latest project checkpoint as parent. That cannot represent parallel task histories. Add an explicit-parent publication path while preserving existing callers' default behavior.

Maintain separate source and workspace heads. Manual project checkpoints advance the source head; task captures advance only the relevant workspace head. Task checkpoint context records purpose, owner task/workspace, baseline, and effective exclusions. Default checkpoint listing remains understandable by showing source checkpoints; an explicit all-workspaces view exposes the rest with their owners.

For a parent start, capture the registered source as its task baseline, with an explicit source-history parent and the task capture profile. This filtered task baseline does not advance the manual source head. For a default child start, capture the parent workspace from its explicit head, advance that parent's head, and use that exact new checkpoint as the child's baseline. Ready/manual task capture uses the task's own previous head as parent. No task operation queries a global newest checkpoint to choose ancestry.

Task capture has visible protected exclusions for Git control metadata (`.git` at every depth), legacy `.solace_vault` data, and Solace-owned store/cache/workspace roots. These rules prevent copied Git/submodule/worktree pointers from referring back to the original repository. Existing manual source checkpoint behavior remains available; task-profile checkpoints explicitly disclose their exclusions. Ordinary hidden source files remain included. Later integration compares the same task profile and must preserve protected metadata in the destination.

Project checkpoint usage must select the recorded source head instead of treating the latest task capture as the source state. Task/workspace inspection reports its own recorded head.

User exclusions are copied into the task's effective profile at creation, inherited by its children, and shown by task inspection. Profile changes require a new task; a task's baseline and results cannot silently use different exclusion rules. A future project exclusion configuration can supply defaults; this milestone accepts explicit rules at task creation.

The existing checkpoint engine records files and symlinks, rather than empty directory entries. This slice preserves that limit and explains it in workspace handoff output; empty-directory preservation is a separate engine extension. Cross-file application-consistent capture is also not promised.

## 6. Workspace placement and isolation

Default workspace root: `$XDG_DATA_HOME/solace/workspaces/<project-id>/<task-id>`. Configure an alternative absolute local root in `core-config.json`. Workspace roots must pass local-filesystem/sync-root validation and sit outside registered source roots, project stores, and Git-controlled ancestor trees. A nested project store remains supported, but it is never used as the workspace root. Invalid placement produces a clear error naming the required relocation.

Each workspace has a stable ID and cataloged canonical path, mount identity, physical directory identity, base checkpoint, head checkpoint, and availability state. Identity metadata stays in the store/database, not in source files. A missing/remounted/replaced workspace is unavailable; it is never silently recreated at the same path. Workspace relink requires the task to be blocked or ready and no core operation to own it; the caller explicitly acknowledges that external writers have been paused. Explicit workspace relink confirms the new path and verifies content against a selected existing checkpoint, reporting dirty files instead of claiming source identity can be proved automatically.

Create mutable regular files with reflink copies when supported and verified ordinary copies otherwise. Never use hardlinks. Source/task files and siblings cannot share mutable inodes. Source and destination placement is revalidated before workspace publication. External agents can still write elsewhere under their user account; this is filesystem workflow isolation, not a process sandbox.

Only relative symlinks whose resolution remains inside the workspace are allowed for materialized tasks. Reject absolute targets, escaping `..` targets, and chains escaping via another link, before publishing a workspace. Fail with the affected paths and guidance to exclude or replace those links. Do not silently rewrite symlink contents or turn external targets into ordinary files. Source checkpoints can retain their existing more general symlink support; unsafe links prevent workspace materialization until explicitly addressed.

The content-addressed object store is not a reflinkable complete-file tree for chunked files. Use an immutable materialized base cache per eligible checkpoint, derived from verified objects. Clone or copy its files into an operation-owned staging directory and verify the resulting manifest before publication. Cache corruption is a hard failure or an explicit cache rebuild operation; no reuse without verification. Cache and staging trees are not task workspaces and are never advertised as editable locations.

Workspace path selection cannot overwrite existing destinations. Publication uses an operation-owned staging path and no-replace rename; all file/mode/symlink checks precede publication. Task cancellation or unregister never removes workspace files. Workspace deletion and cache garbage collection are explicitly deferred.

Workspace status scans and compares against its recorded head, including content, modes, additions, removals, and link targets. It reports differences without creating a checkpoint or changing task state. File notifications and a live GUI are deferred; `--refresh` performs a requested scan, while ordinary show returns dated last-observed status. No event is persisted for each filesystem change.

Storage reporting separates retained object bytes from materialized cache/workspace logical bytes. Do not sum reflinked file sizes or allocated blocks and present that as unique physical usage. If unique/shared extent accounting is unavailable, report that limitation explicitly. All checkpoint/workspace/cache references remain retained; do not advertise speculative reclaimable bytes as safe to delete before the GC milestone.

## 7. Durable publication and recovery

Use the existing per-project coordinator and durable operation IDs. Capture/materialization runs outside write transactions. Task/workspace state, head/result references, operation outcome, and durable events commit together. Resolve task/parent/source/store identities under coordination when accepting work, and validate them again before final publication.

Workspace publication spans SQLite and the filesystem, so do not claim a cross-resource atomic transaction. Record an operation intent containing the chosen task/workspace IDs and staging/final paths before work. Keep the final path unadvertised until its manifest/identity has been verified and the success transaction commits.

- Before filesystem publication: restart reports an interrupted preparation with its preserved staging evidence.
- After filesystem publication but before the success transaction: restart reports interrupted publication. A caller may explicitly resume validation and commit the recorded matching workspace, or abandon the operation while preserving its files. Do not infer success from directory existence alone.
- After the success transaction but before a response: replay returns the original operation ID/result without cloning a second workspace.
- Interrupted ready capture leaves the previous head/result and external task state intact unless the checkpoint/result transaction committed.
- Unavailable stores/workspaces defer relevant recovery. Recovery is retried after original storage returns or after a validated relink, before new conflicting work proceeds.

No retry/repair command deletes a pre-existing directory. Cleanup of known operation-owned unpublished staging is an explicit preview/apply action with identity checks. Deferred object/cache/workspace deletion cannot race retention because this slice does not implement deletion/GC.

## 8. Prompt records and retention

Store exact supplied UTF-8 prompt text in a separate per-task prompt record. No implicit newline changes. Limit prompts to 128 KiB UTF-8 and also enforce the existing complete-frame bound. Reject binary/NUL/invalid UTF-8 input with a clear error. Missing prompt text is allowed for manually tracked tasks and is distinguishable from a deleted prompt.

Approved project default: expire prompt text after 30 days. Project policy can select `retain`, a positive day count, or `none`; task creation may override it. Effective policy is fixed at task creation so later project policy changes do not silently rewrite existing retention decisions. `none` never persists the body. These are approved requirements for the next implementation, not existing installed behavior.

Normal list/show/watch results contain prompt availability and expiration metadata, not the body. Prompt show requires an explicit command; delete clears the logical body while retaining task/history references. Do not duplicate prompt bodies into task titles, result messages, operation rows, request receipts, events, logs, cache names, or workspaces. Body hashes used for request identity do not expose plaintext through APIs.

Expired bodies are hidden from every prompt read immediately, even if cleanup is delayed by unavailable storage. Startup and periodic core maintenance remove expired logical bodies transactionally under project coordination. Failure to purge never permits an expired body to be returned. Record redaction events with task ID/reason only.

This is logical application-level retention, not a forensic secure-erasure guarantee for SQLite free pages, old WAL, user-created backups, copied prompt files, terminal output, or other tools. No prompt replication is introduced here. Future backup/replication design must carry expiration/deletion policy as the approved architecture requires.

## 9. Schema, API, and compatibility

Keep the user registry as location/registration authority; tasks, workspace references, prompt policy, and events belong to each project store. Add task/workspace/provenance/prompt tables with foreign keys and a versioned core schema upgrade. The checkpoint engine's schema numbering remains separate.

Existing version-1 stores require explicit `project upgrade --preview` and an apply action after the migration plan is reviewed. Back up the catalog through SQLite's consistent backup API under coordination before migration; preserve objects and source trees. Upgrade changes and the migration event commit transactionally. New stores use the new core schema. A new binary can continue existing checkpoint/doctor operations on version-1 stores, while task actions explain the required upgrade. The old binary refuses the newer schema rather than treating it as an empty store.

A runtime rollback never silently rolls back project databases or erases task/workspace references. Record this compatibility boundary in the install preview and doctor. Retired version-1 receipt lookup must remain supported.

Add version-1 wire methods for task lifecycle, workspace status/checkpoint/relink, project prompt policy, prompt read/delete, and explicit recovery. Advertise task/workspace capabilities in core status so a new CLI connected to an older installed core can report upgrade guidance. Retain strict argument validation, same-user credentials, durable request replay, stable errors, and both human/JSON output.

Task IDs are labels, not authorization secrets. All clients already run as the same OS user. State changes use a task revision/expected-state condition, preventing two agents from silently overwriting one another's lifecycle decisions. Child start, parent ready, cancel, relink, and unregister check relevant durable task/operation references under the same project coordination. Project unregister is refused while noncancelled task work remains; an explicit cancelled project can be unregistered while preserving all stores/workspaces and read-only receipts.

## 10. Acceptance criteria

- Parent start and two child starts produce separate mutable directories from recorded exact baselines. Editing one leaves the source and sibling unchanged; no mutable hardlinks are used.
- A child never inherits another child's latest checkpoint. Every result follows its own explicit ancestry and carries visible capture-profile provenance.
- Source Git/vault data is unchanged, copied Git pointers cannot reach the source, and unsafe symlinks/placements are rejected before publication.
- Lifecycle transitions, parent summaries, prompt access, and expected-revision failures behave identically through CLI and the local API.
- Ready capture publishes checkpoint, task/head/result references, operation outcome, and event together or none of them. Resuming retains earlier saved results.
- Core restarts preserve external task state and expose interrupted core work accurately. Crash tests cover both sides of filesystem publication and database commits, including unavailable storage and lost responses.
- Reflink and ordinary-copy paths produce the same verified manifest; unsupported reflinks fall back safely and writes never affect immutable bases.
- Prompt defaults/overrides, `none`, explicit deletion, expiry, delayed cleanup, request replay after redaction, and log/event privacy are tested. Logical-erasure limitations are documented.
- Version-1 stores upgrade only through explicit reviewed migration, consistent backup, and transactional schema changes. Failed upgrade and old-runtime rollback preserve data and explain incompatibility.
- Existing 122 tests remain green; add targeted API/concurrency/fault-injection/CLI tests plus independent review before installation.
- Installed acceptance uses disposable projects and preserves the established installer backup/rollback gates. No live agent, automatic merge, original-source replacement, project-data deletion, Git import, or GUI redesign is introduced.

## 11. Implementation-plan decisions

The implementation plan will supply exact schemas, migration SQL, API signatures/command grammar, task revision conditions, reusable safe materialization helpers, explicit recovery commands, prompt maintenance cadence, base-cache identity format, copy/reflink error classification, storage reporting, and test fixtures. It must preserve the choices and boundaries above and identify any existing engine limitations before promising additional guarantees.

Written-spec approval was received on 2026-10-15. Prepare the task-by-task implementation plan for review, preserving inline execution as the established preference unless the user selects another method. Product implementation begins after that plan review.
