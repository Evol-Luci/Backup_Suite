# Manual result integration

Manual child-to-parent and root-task-to-source integration is installed and passed disposable live acceptance on 2026-10-23. See the [installed acceptance and rollback record](engineering/2026-10-23-result-integration-installed-acceptance.md) and the earlier [source review](engineering/2026-10-16-result-integration-review-and-acceptance.md).

The installed core advertises `integrations.v1`. Opening an existing project never migrates it. New stores use core schema 3; existing schema-1/2 stores need an explicit `project upgrade --preview`, then an individually approved apply using that preview's exact token. Schema 2 retains task support; integration requires 3. Engine and registry schemas remain 1. An old runtime refuses schema 3; runtime rollback cannot downgrade databases. See [store upgrades](task-workspaces.md#upgrade-an-existing-store).

## What is reviewed

A result is an immutable saved task checkpoint. A plan records four verified versions: **B**, the contribution baseline; **R**, the saved incoming result; **C**, the current destination; and **M**, the proposed candidate. Preparation does not edit the destination. A child can apply only to its recorded parent's workspace; a root task can apply only to its registered original source. Apply has no destination flag.

Source, task and workspace history heads are distinct. Current destination edits are merged against B; unrelated edits survive when they do not conflict. A first submission R1 uses the task's original baseline. After R1 is applied, a descendant R2 contributes the delta from R1, so independent destination edits to R1 are retained. Repeating an already applied result returns its historical receipt without publishing again. Divergent ancestry requires a fresh decision and is refused rather than guessed.

A parent can become ready when children are ready/cancelled, but root integration is gated until each eligible ready child result has been applied or the child explicitly cancelled. Applying a child invalidates a ready parent's current result eligibility and advances the parent workspace; the old immutable result remains retained. Save the parent ready again before integrating it to the source. Task states remain `created/active/blocked/ready/cancelled`; applied-result counts are separate.

## Prepare and inspect an exact revision

Examples are templates for the installed runtime. Replace uppercase IDs/tokens and revision numbers with values from the exact response you inspected. The installed acceptance exercised preparation, approval and apply on a disposable project; these examples do not authorize application to an existing user project.

```bash
solace integration prepare PROJECT ROOT_TASK --result RESULT --expected-revision 7 --wait --json
solace integration prepare PROJECT CHILD_TASK --result RESULT --expected-revision 7 --expected-target-revision 12 --wait --json
solace integration show PROJECT PLAN --json
solace integration diff PROJECT PLAN --revision 1 --limit 128 --json
solace integration diff PROJECT PLAN --revision 1 --after 128 --limit 128 --json
```

Choose the root or child preparation form, using the contributing task revision and, for a child, its parent's revision. Omit `--expected-target-revision` for roots. Convenience preparation without a request ID can read missing current revisions; it never refreshes a review or approval token. With explicit `--request-id REQUEST_UUID`, pin all required expected revisions yourself. Preserve the UUID, method, actor and identical parameters for a retry; changing any binding under the same UUID is refused.

Preparation, export, apply and recovery mutations return an accepted operation ID. `--wait` inspects the terminal operation; acceptance alone is not success. A wait timeout leaves work running: inspect `solace operation show PROJECT OPERATION --json`. Inspect its `result` for the plan or receipt. Failed/interrupted waited operations return nonzero.

Metadata path pages default to 128 entries, permit 1–256 and fit within 128 KiB after JSON encoding. Follow the returned `next_after`, even when fewer rows than requested arrive; the example cursor 128 is illustrative. Pin the returned plan revision on every subsequent page. Summaries are at most 48 KiB; complete wire frames retain the 1 MiB bound. One oversized metadata row returns `RESPONSE_TOO_LARGE` with export guidance, rather than truncating durable data.

For exact file bytes, use a separate version page:

```bash
solace integration diff PROJECT PLAN --revision 1 --path='src/example.py' --version incoming --offset 0 --json
solace integration diff PROJECT PLAN --revision 1 --path='--literal' --version candidate --offset 49152 --json
```

Choose `base/current/incoming/candidate`. JSON `data` is base64, with at most 48 KiB raw bytes, exact size/digest/entry metadata and `next_offset`. Continue from the actual returned offset. This is a byte view, not an unbounded unified patch. Do not mix content offsets and metadata cursors. `--path=VALUE` safely names paths beginning with `--`; quote spaces and literal backslashes. Engine-relative paths cannot be absolute or contain empty, dot or dot-dot components.

## Resolve, export, approve and apply

Automatic text merge is limited to files no larger than 1 MiB and 50,000 lines and a two-second isolated-worker deadline. Two nonblocking merge slots bound concurrency; timeout, saturation, binary or incompatible edits require whole-file decisions. Conflicts block approval. Resolution creates a new immutable plan revision; inspect its returned revision and candidate again.

```bash
solace integration resolve PROJECT PLAN --revision 1 --path='src/example.py' --take incoming --json
solace integration resolve PROJECT PLAN --revision 2 --path='obsolete.bin' --take delete --json
solace integration resolve PROJECT PLAN --revision 3 --path='asset.bin' --from-file /absolute/reviewed/asset.bin --json
solace integration export PROJECT PLAN --revision 4 --to /absolute/new-review-copy --wait --json
solace integration show PROJECT PLAN --json
solace integration approve PROJECT PLAN --revision 4 --token REVIEW_TOKEN --accept-unchecked --json
solace integration apply PROJECT PLAN --revision 4 --approval APPROVAL_TOKEN --writers-paused --wait --json
solace integration receipt PROJECT RECEIPT --limit 128 --json
solace integration receipt PROJECT RECEIPT --after 128 --limit 128 --json
```

These resolution lines illustrate successive decisions only when those exact conflicts exist. `--take` also accepts `current`; choose exactly one of `--take` or `--from-file`. A directory choice selects its complete subtree and removes colliding prefixes in one revision. Imported files use absolute paths without symlink traversal. The complete candidate link graph, including unchanged links, is validated after resolution; absolute, escaping and cyclic links are refused.

Export requires the exact revision and a new absolute review-copy directory. Editing that copy never changes M. Import a reviewed replacement explicitly with `resolve --from-file` when appropriate, then inspect the new revision. A disjoint export does not invalidate the original review token. Changes to relevant ownership/protection boundaries, destination identity/head/content, or task bindings can make approval stale. Actual ownership and protected-path guards still consider all known Solace paths.

**Approval names one exact candidate revision. If Solace reports a stale plan, prepare and review a fresh plan; never reuse the old approval token.** Copy `REVIEW_TOKEN` only from the inspected prepared plan's `show` response. Copy `APPROVAL_TOKEN` only from `approve` for that exact revision; the tokens are different authorizations. Neither CLI nor core silently refreshes them. IDs are lowercase 32-hex; review/approval tokens are lowercase 64-hex; request IDs use the existing UUID format.

**Solace does not run configured checks in this release. `--accept-unchecked` records that fact; it does not mark tests as passed.** Receipts retain `checks: not_run_by_solace`. Run needed checks yourself on the reviewed candidate/export; do not describe them as Solace-run results. Pause agents, editors and other writers before apply and keep them paused through completion or explicit recovery. `--writers-paused` is an acknowledgement, not a process lock.

Apply uses descriptor-held per-file actions and a private sibling journal on the target filesystem. It preserves the target root inode, displaced files, `.git` at every depth, `.solace_vault`, task exclusions and known Solace-owned trees. Source-only edits are part of C, not silently overwritten wholesale. There is no recursive root swap. Final checkpoint/head/lifecycle changes and the immutable receipt commit together in SQLite. Receipts retain full decisions at rest and return bounded pages; follow their returned cursor.

## Interrupted application and relocation

Restart classifies interrupted publication and **never resumes or rolls back live file writes automatically**. Inspect the original interrupted apply operation and retained evidence before choosing one recovery path:

```bash
solace integration recovery show PROJECT ORIGINAL_APPLY_OPERATION --limit 128 --json
solace integration recovery resume PROJECT ORIGINAL_APPLY_OPERATION --writers-paused --wait --json
solace integration recovery restore-before PROJECT ORIGINAL_APPLY_OPERATION --writers-paused --wait --json
```

`resume` continues the exact approved candidate. `restore-before` restores C for an incomplete application, preserving the interrupted operation and returning a linked restoration result. These are alternatives; restoration is **not forward undo of a completed integration**. Forward undo remains deferred. Recovery operations link back to the original apply; use that original ID for later inspection/recovery. Same-request replay returns the original accepted outcome rather than repeating file actions.

If storage moved, explicitly relink the registered source/store first where required. Recovery alone may then confirm the recorded target and journal locations, with all four path flags supplied together:

```bash
solace integration recovery resume PROJECT ORIGINAL_APPLY_OPERATION --writers-paused \
  --target-path /absolute/moved-target --journal-path /absolute/moved-journal \
  --confirm-target /absolute/moved-target --confirm-journal /absolute/moved-journal --wait --json
```

The same group is supported by `restore-before`. Confirmations must literally match their corresponding absolute paths. A source target must equal its already relinked registered root. A workspace retains its logical task/workspace owner through a verified durable handoff. Relocation cannot select an unrelated destination or rewrite approval, B/R/C/M or original history.

Reserved-only/unidentified journals moved before an immutable physical marker was recorded remain blocked for explicit diagnosis. Do not infer their ownership from existence or matching bytes. An original-location reserved-only operation can use verified absence plus C to prove no change; that proof does not extend to an unknown moved journal. Unknown created-directory identities, foreign replacements, missing journals or ambiguous evidence preserve files and claims rather than guessing. Staged, physically identified journals support confirmed relocation.

## Refusals and retention

| Situation | Action |
| --- | --- |
| Compatible runtime missing / schema too old | Review a runtime installation separately; preview an explicit per-project upgrade. Never open schema 3 using an old runtime or copy an old backup over new history. |
| Destination/task/boundary changed | Inspect refusal, prepare a fresh plan and review the new exact candidate. |
| Store or recorded catalog unavailable | Return the original storage or explicitly relink the still-registered project. Do not create an empty replacement store. Retained metadata/replay may remain readable; new I/O still validates availability. |
| Target at mount root or parent unwritable | Refused before changes: the sibling journal needs the same filesystem and a writable parent. Another journal placement is deferred. |
| Overlapping registered sources or foreign Solace ownership | Refused; do not bypass ownership claims. |
| Abandoned draft | `solace integration abandon PROJECT PLAN --revision 4 --json` retains the plan; it is not journal cleanup. |
| Interrupted operation | Keep writers paused, inspect original apply recovery, preserve journal and displaced/staged files. Never delete uncertainty evidence to release claims. |

Plans, exports, receipts and journals remain retained. Automatic journal/workspace/cache cleanup and object GC are absent. Ordinary views/events/receipts/logs omit prompt bodies; logical deletion is not forensic erasure of SQLite pages, WAL, backups or external copies. Integration adds no prompt replication. Existing [prompt retention](task-workspaces.md#exact-prompts-and-logical-retention) still applies.

## CLI/API ownership

All methods require `project`. Strict validation lives in `solace_core/integration_protocol.py`; dispatch and replay/admission live in `solace_core/integration_operations.py` through `service.py`. `integration_cli.py` constructs the exact pinned fields. Owner tests below are under `tests/solace_core/`; every row also has protocol and CLI construction coverage in `test_integration_protocol.py` and `test_integration_cli.py`. Real-socket workflow coverage in `test_integration_acceptance.py` passed in the permitted runtime gate on 2026-10-23.

| CLI after `solace integration` | API | Semantic owner | Owner tests |
| --- | --- | --- | --- |
| `prepare PROJECT TASK --result RESULT --expected-revision N` (child also `--expected-target-revision N`) | `integration.prepare` | `integration_plans.py` | `test_integration_prepare.py` |
| `show PROJECT PLAN` | `integration.show` | `integration_review.py` | `test_integration_review.py` |
| `diff PROJECT PLAN` (paging/byte flags above) | `integration.diff` | `integration_review.py` | `test_integration_review.py` |
| `resolve PROJECT PLAN --revision N --path PATH` (exactly one selection above) | `integration.resolve` | `integration_review.py` | `test_integration_review.py` |
| `export PROJECT PLAN --revision N --to /ABSOLUTE` | `integration.export` | `integration_review.py` | `test_integration_review.py` |
| `approve PROJECT PLAN --revision N --token TOKEN --accept-unchecked` | `integration.approve` | `integration_claims.py` | `test_integration_claims.py` |
| `apply PROJECT PLAN --revision N --approval TOKEN --writers-paused` | `integration.apply` | `integration_publish.py`, `integration_commit.py` | `test_integration_publish.py`, `test_integration_commit.py`, `test_integration_crashes.py` |
| `receipt PROJECT RECEIPT` | `integration.receipt` | `integration_commit.py` | `test_integration_commit.py` |
| `abandon PROJECT PLAN --revision N` | `integration.abandon` | `integration_plans.py` | `test_integration_prepare.py` |
| `recovery show PROJECT OPERATION` | `integration.recovery.show` | `integration_recovery.py` | `test_integration_recovery.py` |
| `recovery resume PROJECT OPERATION --writers-paused` | `integration.recovery.resume` | `integration_recovery.py` | `test_integration_recovery.py`, `test_integration_crashes.py` |
| `recovery restore-before PROJECT OPERATION --writers-paused` | `integration.recovery.restore-before` | `integration_recovery.py` | `test_integration_recovery.py`, `test_integration_crashes.py` |

Automatic policy, configured check runner, forward undo, GUI review, rename heuristics, advisory binary locks, deeper nesting, alternate journal placement, overlapping-source integration, GC, replication and Git synchronization remain in the [follow-up register](engineering/2026-10-09-review-findings-and-follow-ups.md#proposed-integration-follow-ups-2026-10-16).
