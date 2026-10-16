# Manual Result Integration

**Status:** Proposed written design for user review. This is not implementation or installation approval.

**Baseline:** Local `main` at `6af0b2c`; installed core `59f3d8340f868d4a`. The merged task/workspace milestone passed 192 tests and live acceptance. Design branch: `codex/task-integration-design`.

**Scope assumption:** Support child → parent and root task → original project through the CLI/API. Build the GUI on that API afterward. The user approved continuing with source closeout and integration design; detailed behavior below remains proposed.

## 1. Outcome and delivery boundary

A user can inspect a saved result, compare it with the current destination, resolve conflicts, explicitly approve an exact proposed tree, and apply it with recoverable filesystem evidence. Existing destination edits survive unless the approved plan specifically changes them. A receipt links the exact input versions, approval and resulting checkpoint.

Solace continues to manage work while external tools execute requests. It does not launch agents. The first integration slice is manual and does not execute configured checks. Approval must explicitly acknowledge `checks: not_run_by_solace`; it cannot be presented as a passing test result. This leaves a usable manual workflow while the check runner is designed against stable candidate/receipt contracts.

Follow-up slices are configured check profiles/execution, automatic integration policy, forward undo, then GUI review/status. Git compatibility, replication and GC retain their architecture ordering. Each item and its prerequisites is captured in the [follow-up register](../../engineering/2026-10-09-review-findings-and-follow-ups.md#proposed-integration-follow-ups-2026-10-16). None is silently included in this implementation proposal.

## 2. Approaches considered

| Approach | Benefit | Cost |
| --- | --- | --- |
| **Immutable plan plus per-file publication journal — recommended** | Preserves the destination root, protected metadata and displaced files; supports explicit interrupted-apply recovery | More careful per-path durability, ownership and recovery logic |
| Whole-root staged directory replacement | One directory publication can present a complete tree | Replaces the root inode, disrupts open tools and workspace identities, and complicates preservation of Git/store/excluded metadata |
| Candidate export with no application | Smallest initial change; useful for inspection | Leaves the actual integration and its recovery outside Solace |

Use the first approach. Do not claim a transaction spanning SQLite and a filesystem, or atomic visibility of an entire multi-file edit. Other core mutations are blocked during publication; external writers must be paused. Uncooperative same-user processes remain outside the threat model.

## 3. Inputs, targets and contribution ancestry

Each plan names four immutable versions, all compared under the incoming task's exact capture profile:

| Name | Meaning |
| --- | --- |
| `B` | Effective contribution baseline |
| `R` | Selected immutable ready result |
| `C` | Captured current destination, including its unsaved edits |
| `M` | Proposed merged destination after explicit conflict resolutions |

The source task must be `ready` with `R` its latest ready result at preparation and application. A cancelled or subsequently resumed source task makes an older plan stale. The mutable source workspace need not be online when the verified result and its store are available: `R` is read from immutable objects.

A child can target only its recorded parent's workspace. A root task can target only its registered source project. There is no arbitrary destination pathname or cross-project integration method.

Initially, `B` is the task's saved start checkpoint. If an earlier result from this same task was applied to this same logical target, use the latest applied result that is an ancestor of `R`. This makes a resumed task contribute `R2 - R1`, preserving destination-only changes from the previous integration. Never choose a global-latest checkpoint or a sibling result. If a prior applied result exists but is not an ancestor, refuse the plan and require a new task based on current destination state.

Applying the same `(task, result, logical target)` twice returns/refers to the existing receipt without changing files again. This also applies when someone subsequently edits the destination: a historical receipt does not authorize silently reapplying old work.

## 4. Preparation, inspection and conflict resolution

`integration prepare` is a durable operation that stores objects, snapshots and a plan; it does not modify destination files or advance published heads. A metadata-only show or diff-page read does not capture files. Preparation holds short core claims while capturing a stable destination, then releases them when the plan is ready for review.

The plan records source task/result and target IDs; source/target revisions; the previous target head; exact profile; physical store, root and relevant ancestor identities; intersecting protected roots; `B/R/C/M`; path actions; conflicts; and an optimistic plan revision. Unpublished candidate checkpoints are retained without entering normal source/task history. Operation parameters contain IDs/digests, not whole manifests.

Changes to destination contents, relevant identities, heads, task revisions or protected boundaries require a fresh plan. Resolving a conflict produces a new candidate `M`, increments the plan revision, and invalidates all earlier approval tokens. Solace never silently recomputes a plan after approval.

Plan states are `conflicted`, `prepared`, `approved`, `applying`, `applied`, `recovery_required`, `restored` and `abandoned`. Preparation yields conflicted/prepared; resolution yields another conflicted/prepared revision; only prepared can be approved; only approved can begin apply. Successful apply ends at applied. Interrupted file changes require recovery. Successful restore-before ends at restored and needs a new plan before another apply; abandoning is permitted only before changes. Known stale inputs are reported separately and prevent approval/application. Ordinary metadata shows the last validation time rather than claiming live filesystem freshness.

Merge regular-file content with these conservative rules:

1. If current equals baseline, take incoming; if incoming equals baseline, keep current; identical current/incoming changes are retained once.
2. For strict UTF-8 text without NUL, merge disjoint base-relative line edits. Preserve LF/CRLF bytes and final-newline absence. Overlapping edits or different insertions at the same location are conflicts unless their replacement bytes are identical.
3. Limit automatic text merging to files at most 1 MiB and 50,000 lines. Enforce a two-second per-file deadline in an isolated core-owned merge worker; terminate a timed-out computation without blocking the service or accepting partial output. Larger, non-text or timed-out files require whole-file selection. Report the reason; do not guess at encoding or normalize content.
4. Compare entry kind, executable/permission mode and symlink text as separate versioned attributes. Incompatible type/mode changes, delete-versus-modify, differing binary edits and file/directory prefix collisions require explicit resolution.

Rename detection is not included: a rename appears as deletion plus addition. Regular files and safe relative internal symlinks are supported. Validate the complete candidate link graph before approval/application. Refuse unsupported special entries and unsafe links rather than following them.

A conflict can select the current or incoming entry, explicitly delete the entry, or import a user-edited regular file. Import reads exact stable bytes through nofollow traversal and creates a new immutable candidate. A path/subtree resolution must yield a valid complete manifest; it cannot leave both `a` and `a/b` as file entries. All rejected versions remain retained in the store.

An export command materializes base/current/incoming/candidate versions into a fresh separate directory for ordinary editors and binary viewers. It never exports a prompt body implicitly, overwrites an existing directory, or becomes an automatic check result. Edits to an exported copy affect the plan only through an explicit resolution import.

## 5. Approval and lifecycle semantics

Approval is a separate mutation bound to the exact plan revision, `M` digest, destination snapshot/identities, source result and target head/revisions. The review token is not a general authorization to write later versions. Apply requires that exact approval token and a fresh acknowledgement that destination writers are paused.

Manual approval must explicitly accept that this slice has no Solace-run checks. Missing/failed/fabricated check metadata cannot become `passed`. No automatic integration setting or implicit approval is introduced. The future check runner will bind evidence to the same candidate/profile digests and revoke it whenever the candidate changes.

The Unix peer UID is authenticated; the request's actor label is descriptive. An explicit approval command records intent but cannot cryptographically prove that a human, rather than another same-user process, issued it. Do not describe actor labels or the token as a sandbox against agents.

External task lifecycle remains `created/active/blocked/ready/cancelled`. Application status belongs to each immutable result and logical target, rather than permanently completing a reusable task. Task inspection adds latest-result application metadata and receipt IDs. A ready result may be labelled `applied` while its raw task state remains `ready`; resume creates a new revision and a later result can be integrated incrementally.

Integrating a child changes its parent workspace head/revision. Preserve an active or blocked parent's state and block reason; a previously ready parent becomes active because its own saved result predates the new files. Keep that older result as history. The parent must save ready again before applying its result to the source project.

Root-task preparation/application requires every direct child either cancelled or its latest ready result already applied to this parent. Ordinary lifecycle counts remain available separately from applied-result counts. Cancellation preserves already applied changes and receipts; it is not undo.

## 6. Protected paths and target ownership

Apply only the incoming task's declared capture scope. Preserve `.git` at every depth, `.solace_vault`, excluded paths and every known Solace-owned store/cache/workspace/journal root. A newly protected destination path cannot be overwritten merely because an older baseline included it. Refuse an incoming delta that crosses such a boundary and require a fresh task/profile decision.

Empty directories and directory modes are not represented by the checkpoint engine. Integration must not recursively delete directories to approximate a manifest. Leave unaffected directories intact. A directory-to-file replacement requires the directory to be empty after its explicitly planned child removals; remaining excluded, protected, special or empty subdirectories cause a conflict/refusal. Preparation records an auxiliary directory-entry/identity inventory at replacement prefixes so these conflicts are visible before approval; application rechecks it before changing files. New directories are private by default; do not rewrite existing directory metadata.

Use the same coordinated ownership rules as registration/workspaces. Root integration refuses a source that overlaps another registered project's source root, because the other project has a separate coordinator. Relevant unavailable catalogs make ownership checks fail closed. Active target and journal reservations also block conflicting source/store registrations and relinks. A workspace relink cannot adopt an integration preparation or journal.

Integration changes only approved file entries, never the source root inode. Descriptor traversal and nofollow leaf operations must cover all components. Do not expose a generic public option that bypasses workspace placement policy; integration is a distinct operation against an already registered source/workspace.

## 7. Recoverable file publication

Before any destination mutation, reserve core target/source claims and a private journal beside the target on the same filesystem. The target parent must be writable and on that filesystem. Mount-root or unwritable-parent targets are refused before changes with a clear placement error; this first slice does not place a journal inside the project or fall back across devices.

Claims block every core method that reads or mutates the affected live tree, including source checkpoint capture and task starts from a source being applied. Unrelated workspaces/projects may continue. Metadata inspection remains available, but it exposes applying/recovery-required status and withholds an affected workspace's ordinary usable path until publication or recovery completes. This cannot revoke a pathname already held by an external process.

The durable intent contains exact target/journal paths and identities, old/proposed manifests, approval, ordered actions and per-path progress. Reserve journal paths globally before creation. Keep every prepared/displaced file private and retain its provenance. Sync receiving ancestors, staged file data, journal entries and relevant directories before committing corresponding progress.

For an existing entry, move it into its operation-owned journal slot without replacement, then validate the displaced entry against `C`. Preserve it even on a mismatch. Publish a prepared replacement only with a no-replace rename into the now-vacant name. A winner created by another process is never overwritten. A deletion moves its expected entry into the journal; it is not an unlink. Parent creation and empty-directory removal are separately identity-checked and journaled.

This does not promise undetectable races cannot occur with an external process that ignores the writer-pause requirement. Such a writer can retain open descriptors. The guarantees are preserved displaced versions, no blind overwrite of a newly occupied name, explicit uncertainty and no automatic recovery decision.

After the target matches `M`, verify and sync it. One catalog transaction publishes before/after checkpoint contexts, advances the target head, appends retained references and the immutable integration receipt, records the operation outcome/event, updates affected revisions, and releases claims. The receipt records incoming-result provenance separately from the checkpoint engine's single-parent link.

For a source target, the before checkpoint follows the previous source head, and the after checkpoint follows before; the source head advances only in the success transaction. For a workspace target, the same rule applies to that workspace's head. Published contexts disclose the actual capture profile, including protected exclusions. No history or storage calculation uses global-latest task checkpoints.

## 8. Crash, replay and recovery

| Interruption point | Required observable behavior |
| --- | --- |
| Preparation before any destination changes | No published head/result change; retained draft evidence can be inspected or abandoned |
| Journal reserved but no file action | Interrupted operation; identity validation required before resume or abandonment |
| Some file actions completed | Target reported as requiring recovery; block conflicting core access; retain old/new versions and journal progress |
| Files match `M` but success transaction did not commit | Explicit recovery may verify and finish publication; directory/file existence alone is not success |
| Success committed but reply lost | Same request replays its original operation/receipt without another application |

Restart never resumes file writes or rolls them back automatically. It classifies interrupted work, retains claims for unresolved publication, and defers validation when the original store/target mount is unavailable. Relocation must durably reconcile journal paths and expected identities, using the receipt/marker pattern established for workspace recovery; a success receipt cannot strand old paths.

Explicit `resume` verifies journal/target identities and the expected old/new version for every action before continuing. Explicit `restore-before` reverses only recorded changes whose present entries still match the operation's published versions, preserving any third-party winner. A mismatch stops recovery with all remaining evidence intact. It never recursively replaces the target root.

Each recovery request has its own durable operation and replayable outcome. Recovery links to the interrupted operation without rewriting that operation's historical outcome; successful publication or restoration records the resulting plan state and receipt separately.

Abandoning a draft is allowed. Abandonment cannot release an unfinished apply claim while the target is partially changed: first finish, restore-before, or resolve its recovery conflicts. Cancellation of an operation is not permission to delete displaced files. Journal deletion and retention/GC remain a separate previewed cleanup design; retain them in this slice.

## 9. Receipts and retained data

An immutable receipt links project/task/client identity, result `R`, effective baseline `B` and previous contribution receipt, target identity and old head, captured `C`, applied `M`, capture profile, path decisions, exact approval, `checks: not_run_by_solace`, before/after checkpoint IDs and durable operation/event IDs. Ordinary views contain bounded summaries/counts and paginated path metadata. Explicit diff/export commands expose file contents separately.

Prompt bodies remain solely in the existing prompt store under its fixed retention policy. Receipts reference task IDs and current availability metadata; they never copy prompt bodies, store private agent reasoning or resurrect redacted text. Logs omit raw requests, file contents and prompt bodies.

Retain base/current/incoming/candidate/before/after references and all unresolved journal references. No speculative reclaimable-space report or object deletion is introduced. A failed preparation may leave unreachable objects, as existing capture can; no automatic repair or garbage collection follows from that failure.

## 10. Storage, API and compatibility boundary

Propose core schema **3**, with an explicit preview/apply upgrade and verified consistent backup. New stores created by the future integration core use schema 3; existing schema-1/2 stores preserve their current capabilities until explicitly upgraded. Opening a store must not silently migrate it. The engine and registry schema numbers remain separate. A schema-2 runtime must refuse schema 3 without changing it. No existing/live store is upgraded by preparing this design.

The future upgrade preview targets schema 3 from either schema 1 or 2, using one consistent pre-upgrade backup and one transaction; a schema-1 upgrade includes the existing task/lineage transformation before adding integration metadata. Previously committed schema-2 upgrade requests must still replay their original schema-2 receipts, even when the current catalog has subsequently reached schema 3.

Project catalogs gain versioned plans, candidate/resolution references, approvals, target claims, publication journals/actions and append-only receipts. Preserve current task-state values; application status is a separate relation. Exact table DDL and command parameter schemas belong in the implementation plan, with these contracts fixed.

Add `integrations.v1` capability; retain wire version 1 and the 1 MiB complete-frame bound. Long prepare/export/apply/recovery actions return durable operation IDs. All mutations check for an existing request receipt/operation before checking revisions or availability; synchronous receipts contain no file or prompt bodies. IDs and tokens are exact, request fingerprints include actor/method/parameters, and list/diff responses are paginated and byte-bounded.

Use lowercase 32-hex IDs and 64-hex digest tokens. Path pages default to 128 entries, allow 1–256, and have a 128 KiB byte ceiling with a returned continuation cursor pinned to plan revision. Summaries include exact counts and truncation metadata. Receipt replay and inspection by stable project ID remain available after unregister while its store is readable; new preparations/applications require registration. Unregister/relink cannot bypass unresolved apply claims.

Proposed user flow, with placeholder IDs representing values returned by earlier commands:

```bash
solace integration prepare PROJECT TASK --result RESULT --wait --json
solace integration show PROJECT PLAN --json
solace integration diff PROJECT PLAN --limit 128 --json
solace integration export PROJECT PLAN --to /absolute/new-review-copy --wait --json
solace integration resolve PROJECT PLAN --path relative/file --take incoming --revision N --json
solace integration resolve PROJECT PLAN --path relative/file --from-file /absolute/edited-file --revision N --json
solace integration approve PROJECT PLAN --revision N --token REVIEW_TOKEN --accept-unchecked --json
solace integration apply PROJECT PLAN --revision N --approval APPROVAL_TOKEN --writers-paused --wait --json
solace integration receipt PROJECT RECEIPT --json
solace integration recovery show PROJECT OPERATION --json
solace integration recovery resume PROJECT OPERATION --writers-paused --wait --json
solace integration recovery restore-before PROJECT OPERATION --writers-paused --wait --json
# Alternative: withdraw a draft that has not changed destination files.
solace integration abandon PROJECT PLAN --revision N --json
```

Prepare infers the sole eligible target from task ownership. Explicit request IDs require pinned revisions/tokens as appropriate. Every new method has strict typed validation, a dispatch owner, a retention/replay policy and an integration test. Expected errors distinguish conflicts, stale plans/approvals, unchecked acknowledgement, shared targets, journal placement, changed identities, recovery requirements and reused request IDs.

## 11. Implementation boundaries and acceptance

Keep manifest/line merging, plan persistence, review/receipts, descriptor-based publication and recovery as focused units. Reuse engine object verification, explicit checkpoint ancestry, coordinator claims, bounded observations and the core's protocol/CLI patterns. Do not adapt the legacy ZIP-vault merge into the core integration engine.

Required acceptance cases include:

- Child → parent → source through real CLI/socket requests, with independent destination-only edits retained and protected metadata byte-identical.
- Repeated submission `R1` then descendant `R2` contributes only the incremental delta; repeated request/result does not duplicate application.
- Disjoint text edits, identical edits, insertion boundaries, CRLF/no-final-newline, malformed UTF-8, bounded large-file fallback, executable modes, link targets, binary conflicts, deletion/modification and prefix collisions.
- A parent already ready becomes active after child application; earlier results remain retained; a root cannot bypass unapplied children.
- Changed destination/revision/approval/profile and concurrent new destination entries are refused without blind overwrite; displaced third-party versions remain recoverable.
- Real subprocess exits before/after each file move, journal update and final SQL commit; reconnect/relocation, explicit resume/restore-before and replay after the lost reply.
- Protected/excluded descendants prevent directory replacement; source roots, `.git`, vault/store data and unrelated worktrees are never recursively swapped/deleted.
- No-follow ancestor/leaf substitutions; unsupported filesystems; absent or replaced journals; receiving-directory fsync; physical power-loss claims remain separate from process-exit evidence.
- Large plans/receipts/diffs remain readable through the real socket; prompt redaction/expiry cannot leak through approvals, receipts, logs or retries.
- Explicit schema-2-to-3 backup/rollback failures and schema-1/2 compatibility; all existing 192 tests remain green alongside new regressions.

After spec approval, write a separate implementation plan, execute in isolation, obtain independent whole-branch review, remediate confirmed defects with RED→GREEN tests, and present a concrete installer preview. Installation and disposable installed acceptance remain a separate approved action. This proposal does not authorize an agent to apply results to user projects.

## 12. Decisions for written review

The proposal deliberately selects both integration targets, manual approval with explicit unchecked acknowledgement, result-level application status, a same-filesystem sibling journal, conservative text merging and retained journals. These are concrete choices for review, not implemented behavior. Configured check execution, automatic policy, forward undo, GUI, rename heuristics/locks, deeper nesting and retention/GC are recorded as follow-ups rather than inferred approval for a larger implementation.
