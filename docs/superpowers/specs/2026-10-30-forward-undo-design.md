# Forward undo after an applied result — design

## Premise and falsification test (recorded before source investigation)

The proposed next slice is a human-reviewed forward operation that restores a destination's prior content after a completed integration apply, while preserving the original receipt and all later history. This premise is wrong or materially narrower if the current source already exposes a durable, explicit post-apply reversal for arbitrary receipts that binds the exact prior state, refuses subsequent external edits, survives a crash, and records a new receipt. In that case, this document must identify the existing operation and only scope its demonstrated gap. A compensating action that works only before apply, or a filesystem copy without durable provenance, does not falsify the premise.

**Result:** The falsifying observation did not occur. Current restore-before handles interrupted apply only; completed integration has no undo protocol. Evidence is below.

## Decision and cost

The supervising reviewer approved a narrow v1: only the latest completed integration receipt for an **exact target** may be undone, and only while both catalog head and captured live destination match its recorded after state. The operation creates a fresh compensating candidate and immutable receipt. It never updates or deletes the original receipt. “Latest” means latest across all tasks for the target, not latest from the same source task.

This excludes older or nested undo until a later design. It also refuses unrelated external edits within the capture scope, even when a merge might be possible. The operator must preserve those edits separately and choose a newly reviewed workflow.

## Authority and invariants

1. A human selects the original receipt, reviews the new after-to-before candidate, authorizes checks or explicitly accepts unchecked where policy allows, then pauses external writers and acknowledges that pause for apply. Actor labels are audit metadata, not authentication. The checked queue does not prepare, approve or apply undo automatically.
2. Eligibility binds the completed original receipt, target key and identity, its before/after checkpoints, the current target head, the retained capture profile, current protected boundaries and current project policy. The target head must equal original after. A fresh scan of all included live paths must equal the original after manifest by path, kind, mode and content. Excluded/protected paths are outside that equality claim and remain untouched. If an old profile cannot be reproduced safely, refuse.
3. Read-only eligibility inspection reports whether the selected receipt and live target currently qualify; it creates no plan or checkpoint. A separate prepare operation pins a newly captured current checkpoint, fresh candidate with the original before content, exact diff, policy/check requirements, and review token. Neither operation mutates the destination. Candidate ancestry begins at current head, even when its bytes equal the original before checkpoint. Original before/after checkpoints remain retained as provenance.
4. Approval and check evidence must bind this exact undo plan/revision/candidate and current policy. The old integration's approval, check run and writer acknowledgement cannot be reused. Required project profile still applies despite a task manual override. If current check infrastructure cannot check an undo candidate, refuse checked approval until a typed binding exists.
5. Before any file action, repeat receipt order, head, identity, live manifest, policy, candidate and claim checks. Descriptor-held target/journal claims and per-action checks cover the changes they actually observe; they cannot universally detect or prevent an arbitrary concurrent external writer. Writer-paused is an operator acknowledgement, not a lock. If a mismatch is detected after file actions start, retain an interrupted operation for explicit recovery. A successful receipt is conditional on the operator's writer pause being true throughout publication; true automatic writer handoff is deferred.
6. On successful publication, one catalog transaction advances source or workspace head, pins before/after refs, writes a new immutable undo receipt linked to the original receipt and operation, emits an event and marks the operation succeeded. A child-to-parent undo increments parent task revision and invalidates its ready state. It does not propagate automatically from parent workspace to source.
7. Same-request replay returns the original undo operation/receipt without repeating actions. A distinct request for an already undone original receipt refuses. The original integration receipt remains queryable and unchanged.

## Data and public shape

Use an additive migration, likely schema 6, for typed undo plans, revisions, approval/check binding, journals and immutable undo receipts. Reuse the guarded publication machinery where safe. Do not insert a fabricated ordinary integration receipt: ordinary receipts have a unique task/result/target key and their baseline represents a task contribution. The undo receipt needs explicit original receipt, exact target, fresh plan/revision, pre/post checkpoint IDs, approval/check provenance and event ID. Retain refs for every checkpoint needed by receipts and unresolved journals. Older runtimes must reject the new schema; opening a store must not upgrade it. Any real upgrade requires a separate preview, verified backup and authorization.

Proposed CLI and protocol names (exact spelling can be settled during implementation):

| CLI | Required behavior |
| --- | --- |
| integration undo eligibility PROJECT RECEIPT | Read-only eligibility and staleness inspection; no plan or checkpoint writes |
| integration undo prepare PROJECT RECEIPT | Write a fresh durable plan/candidate after checking eligibility; no destination mutation |
| integration undo show/diff PROJECT PLAN | Bounded status and exact after-to-before review; opt-in content pages |
| integration undo check run PROJECT PLAN --revision N PROFILE | Run configured checks bound to this candidate |
| integration undo approve PROJECT PLAN --revision N --token TOKEN (--check-run RUN or --accept-unchecked) | Enforce current required profile and exact binding |
| integration undo apply PROJECT PLAN --revision N --approval APPROVAL --writers-paused | Publish with journal and fresh revalidation |
| integration undo receipt PROJECT UNDO_RECEIPT | Expose linked immutable provenance |
| integration undo recovery show/resume/restore-before PROJECT OPERATION | Explicit handling of an interrupted undo |

Strict protocol schemas must reject extra parameters and malformed IDs, revisions and booleans. Status should distinguish ineligible receipt, changed target, changed policy, missing checks and recovery required with actionable messages. Eligibility/show/diff/status must use sidecar-free catalog access and scan-only repository access. Prepare is an explicit catalog write and must make that side effect clear. A GUI may later consume the same typed statuses.

## Crash, restart and later submissions

Before file publication, stale/failed requests leave the destination untouched. After any file action, keep journal, claims, staged content and operation identity. Restart reconciles status only; it never silently resumes. Explicit undo recovery resume continues the same approved candidate after revalidation; undo recovery restore-before restores the captured pre-undo state for an **incomplete** undo. Both require a new writer acknowledgement and linked evidence. Neither reverses a completed undo. Relocation uses current recovery's exact path and identity confirmations or refuses.

Undo leaves source task's ready result and original integration receipt intact. Same-result replay remains blocked. A later result from that source task uses the last applied **result** as its contribution baseline, while the current destination is the post-undo head: unchanged reverted content must stay absent, and changed/conflicting content enters normal review. This needs focused acceptance. A newer receipt for that target renders the old one ineligible. Undoing an undo is deferred.

## Acceptance cases

1. Disposable root-to-source and child-to-parent latest receipts pass read-only eligibility with no catalog sidecars or new rows; prepare creates one durable plan/candidate but leaves the destination untouched; apply restores exactly the prior captured content with fresh head/receipt/event and original receipt unchanged. Child undo invalidates parent ready state and increments revision.
2. Older same-target receipt, newer different-task receipt, already undone receipt, mismatched catalog head, external included-path edit, moved target, changed protected boundary, unresolved claim and changed policy/profile refuse before mutation. Excluded paths survive unchanged.
3. Required checks cannot be bypassed by unchecked approval or task override; old check/approval evidence fails; exact passing run succeeds. Failed checks retain reviewable status.
4. External changes between eligibility, prepare, approval and apply refuse when detected. Inject observable races at guarded file actions; detected mismatches yield retained recoverable interruption and no false receipt. Test the documented writer-pause condition separately; do not treat the guard tests as proof against arbitrary concurrent writers. Restart never silently continues.
5. Fault injection at reservation, staging, each file action, files-ready and catalog commit proves explicit recovery, replay and receipt uniqueness.
6. Same original result cannot reapply; a later incremental result leaves reverted unchanged content absent and requires review for changed/conflicting content.
7. Disposable schema-5-to-new-schema preview/backup/apply retains old receipts/refs; backup integrity and old version are verified; old runtime rejects new schema. Installed disposable acceptance and installer/unit preview are separate gates.

**Design falsification probe:** On a disposable target, apply receipt R1, then prepare and approve its undo under the exact-after condition. Save a later ready result R2 from the same task whose unchanged files include content introduced by R1 and whose changed files overlap the reverted paths. After undo, prepare R2 through the existing merge semantics. If unchanged R1 content reappears without an explicit reviewed incoming change, or R2's changed paths cannot be represented as reviewable conflicts/candidate changes while keeping R1 immutable, the proposed baseline rule is invalid. Stop implementation and revise the incremental-submission design before claiming v1 acceptance. Independently, if the undo candidate cannot represent the original before tree under current protected/excluded boundaries without touching out-of-scope paths, reject that target rather than weakening the exact-after rule.

## Deliberately deferred

Older receipt undo, undo of undo, preservation/merge of unrelated external edits, automatic undo apply, agent/editor writer handoff, nested-history rewriting, GUI controls, arbitrary downgrade, cross-project moves, GC and replication.

## Source evidence

- docs/result-integration.md:64,74–78,118–125 distinguishes writer acknowledgement, interrupted restore-before and deferred forward undo.
- solace_core/integration_recovery.py:79–102 requires recovery of an interrupted original operation; solace_core/integration_protocol.py:5–18 has no undo method.
- solace_core/integration_schema.py:59–75 defines receipt before/after refs and recovery modes; :91–94 makes original receipts immutable.
- solace_core/integration_commit.py:159–199 commits lineage, receipt and operation together; :201–219 exposes provenance.
- solace_core/integration_plans.py:70–114 chooses target and last result baseline; :116–140 requires a latest ready task result, so undo cannot masquerade as a normal integration plan.
- solace_core/integration_claims.py:141–164 rescans live target/boundaries; solace_core/check_approvals.py:19–49 binds normal check evidence; solace_core/policy_approvals.py:9–34 enforces required profile.
- solace_core/lineage.py:21–29 advances target head; solace_core/tasks.py:125–130 receives child integration.
- docs/automatic-policy.md:1–4,16–18,24–31 keeps apply human controlled and required checks in force. docs/engineering/automatic-policy-handoff.md:31–35 lists forward undo as a next slice; its installation state is historical. docs/engineering/2026-10-09-review-findings-and-follow-ups.md:56 calls out incremental submissions after reversal.

## Human-only decisions

The narrow eligibility ruling above is already made. Later deployment still needs separate operator review of migration/backup, installer preview, installed disposable acceptance and any real-project apply. Broadening this scope requires another design review.
