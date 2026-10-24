# Manual result integration: agent handoff

Updated: 2026-10-23. This is the continuation entry point for a fresh context.

## Current state

Tasks 1–10 passed independent task reviews. Task 11 implementation passed its source review and required runtime gates and was committed as `0d71e15` on 2026-10-23. The Task 12 documentation/preview subset passed independent review after one sequencing correction and scoped re-review. An independent whole-branch review found no code issue and one Important stale-documentation finding, now corrected. Documentation checks passed after that correction. The source plan is complete in this branch; manual integration has **not been installed by this execution**.

- Repository: `/home/lucievol/Projects/Backup_Suite`.
- Use this existing worktree: `/home/lucievol/Projects/Backup_Suite/.worktrees/task-integration-design`.
- Branch: `codex/task-integration-design`.
- Task 11 commit: `0d71e15`; use `git log -1` for the following Task 12 documentation commit.
- Plan: [2026-10-16-result-integration.md](../superpowers/plans/2026-10-16-result-integration.md).
- Approved specification: [result-integration-design.md](../superpowers/specs/2026-10-16-result-integration-design.md).
- Execution ledger: `.superpowers/sdd/2026-10-16-result-integration/progress.md` inside the worktree. Read it before dispatching work. Its completed tasks must not be repeated.
- Task briefs, reports, diff packages and test logs share that ignored directory. Preserve it until evidence is archived.

The latest user instruction is to continue the current work and leave an easy fresh-agent handoff. They selected subagent-driven execution and approved source implementation. Source work does not authorize installation, live project migration/application, merge, push, publication, or destructive cleanup. Home must not be initialized as a Git repository.

## Active environment constraints

The ordinary sandbox still denies Unix listener binding under `/tmp` with `PermissionError: [Errno 1] Operation not permitted`. On 2026-10-23, approved unrestricted execution permitted a real bind and all four required suites passed without mocks or skips. Keep the restricted-run failures as historical evidence.

The ordinary sandbox allows writes inside the source workspace and `/tmp`, while Git metadata is read-only there. Both source-plan commits used explicitly authorized Git-metadata access. Do not relocate Git metadata or bypass that restriction for later work.

`/var/tmp` is now read-only. Earlier evidence there remains readable. Task 11 has added an explicit test-only temporary-root override (`SOLACE_TEST_TMPDIR=/tmp`) so disposable tests and subprocesses use `/tmp` while preserving their historical default elsewhere. Do not weaken production storage/identity checks. Use `/tmp` for installer preview if these permissions remain active; never run installer apply.

## Completed source checkpoints

| Task | Latest task commit | Independent gate |
| --- | --- | --- |
| 1: schema 3 and explicit upgrades | `38b6173` | Passed |
| 2: conservative bounded merge | `4278ab3` | Passed after startup fix |
| 3: exact immutable preparation | `e649f6e` | Passed; historical timing concern retained |
| 4: review, resolution and export | `bccd42e` | Passed after retained-binding fix |
| 5: approval and live-tree ownership | `1e2f32a` | Passed after ownership/lock corrections |
| 6: descriptor-held journal publication | `f41d53d` | Passed after inventory/final-scope fixes |
| 7: atomic history, lifecycle and receipts | `0ebced1` | Passed |
| 8: explicit recovery and relocation | `b72082d` | Passed after moved-restoration fix |
| 9: strict API and durable replay | `cbc7754` | Passed |
| 10: CLI and socket acceptance | `f3cbdd0` | Passed; custom-socket guidance Minor assigned to Task 11 |

Each row has `task-N-report.md` and independent review evidence in the execution directory. Do not reconstruct every earlier task from chat. Read only the relevant task handoff sections when crossing an interface.

## Task 11: exact resume point

Task 11 source/test implementation, task-scoped independent review, permitted runtime gates and commit are complete. The reviewer approved implementation quality/spec compliance with no actionable finding. Read `.superpowers/sdd/2026-10-16-result-integration/task-11-report.md`, `task-11-review.md`, and `task-11-evidence-20261022/` for the earlier tests and classifications. No process worker should be resumed by an old session-local agent handle.

The working review package is `review-task11-working-57d4d54c7bd5.diff`, SHA256 `57d4d54c7bd59e4f90b0db45be5e714957ebb145d0c0de192fa5d9c2fed33317`, based on prior committed `f3cbdd0`. Its 13 source/test files matched the patch manifest before commit `0d71e15`. The 135-file gate manifest covers source, tests and packaging. README and CHANGELOG have since changed for Task 12; all source/test/packaging hashes remain identical. Do not silently call the old whole-manifest digest a current all-file match.

The required Task 11 runtime gates passed on 2026-10-23 in a legitimately socket/systemd/display-capable environment, and the patch was committed. Do not mock or skip tests, manufacture product RED, or rerun completed crash waves. The real process-crash matrix passed 168/168 cases in both the earlier restricted and current unrestricted integration/core runs; the earlier gate logs record 247 exact durable barrier records per broad gate.

## Remaining sequence

1. Preserve the ignored execution scratch, logs and current worktree until evidence is archived. The 135-file gate manifest rehash found only README and CHANGELOG changed from the pre-documentation snapshot; source/test/packaging files are unchanged.
2. Prepare a fresh installation preview and review exact runtime, backup and rollback paths. Separate concrete authorization is required before live installation or a per-project schema upgrade. Run installed-candidate acceptance and rollback verification only after that installation; no such side effect occurred in this execution.

## Binding implementation limits

- Child results go only to their recorded parent workspace; root results go only to the registered source. No arbitrary destination.
- Exact immutable B/R/C/M, current plan revision and selected approval are bound. Configured checks are not executed: `checks=not_run_by_solace`; explicit unchecked and paused-writer acknowledgements remain required.
- Source/task/workspace heads stay distinct. Repeated R2 contributes only its delta from the prior applied R1.
- Per-file no-replace journal publication preserves the target root, displaced versions, protected/excluded paths, and uncertainty evidence. No recursive root replacement or automatic journal cleanup.
- Restart never resumes/rolls back live files. Only explicit linked recovery can continue or restore-before; original interrupted outcomes and approval history remain unchanged.
- Moved reserved-only/unidentified journals without a physical immutable marker stay blocked. Do not infer ownership from existence or equal bytes.
- Target-relevant boundary binding allows disjoint exports/registrations without stale approval. Actual exclusions/ownership/placement still use full global boundaries, including physical identity protections.
- Core schema 3 upgrades are explicit and backed up. Engine/registry schema remains 1, wire version 1 and complete frame ceiling 1 MiB. Path pages: default 128/max256/128 KiB; summaries48 KiB; content48 KiB raw; automatic text merge1 MiB/50,000 lines/two-second isolated deadline.

## Installed baseline and rollback

No live runtime was switched by this source plan. On 2026-10-23, the installed launcher and service unit still named runtime `59f3d8340f868d4a`, and the launcher/socket/service hashes were identical before and after preview. The durable rollback helper `/home/lucievol/.local/share/solace/core-rollback-tools/e77eb01bf4e57e51/rollback-core.py` exists and matches the prior SHA256 `e77eb01bf4e57e51fc43fc5db40457628808e4d31a63b7ec08d0717424f5bfd7`. No rollback was run. Old runtimes cannot open schema-3 stores; never silently downgrade/migrate them.

## Fixed-source verification

On 2026-10-23, the controller reran the required gates sequentially in an approved unrestricted environment with `SOLACE_TEST_TMPDIR=/tmp`. A Unix listener bind probe succeeded. Integration passed 441/441 in 209.273s; engine passed 52/52 in 2.048s; complete core passed 570/570 in 223.504s; legacy passed 16/16 in 0.422s. All four commands exited 0 with `OK`, including socket, systemd verification and Tk tests. Integration/core scopes overlap. `git diff --check` passed. The 135-file source manifest rehash had only the already documented README and CHANGELOG differences; no source/test/packaging file changed. This is source verification, not installed-candidate acceptance.

The prior 2026-10-22 restricted run remains historical evidence:

| Gate | Result |
| --- | --- |
| Integration | 441 tests: 404 passed, 37 socket-setup EPERM errors |
| Engine | 52 passed |
| Complete core | 570 tests: 505 passed, 64 socket errors, one sandbox-blocked systemd verification assertion |
| Legacy | 16 tests: 9 passed, 7 unavailable Tk display setup errors |
| Diff whitespace | Passed |

Scopes overlap: do not sum integration and core counts. All 168 real process-crash tests passed in both gates, with 247 exact durable barrier records per gate. All runtime failures have exact names/tracebacks in `task-11-evidence-20261022/gate-failure-classification.json`; the systemd test ran partially before its denied socket operation, and legacy GUI tests never reached their bodies.

The 135-file source/test/packaging manifest remained unchanged through the sequential gates, with an independent controller rehash at that time. On 2026-10-23 README/CHANGELOG changed for documentation; the other 133 manifest files, including all source/test/packaging files, still match. `gate-source.json` SHA256 is `41b1f2e2f90f32011ef1f9a4354388d3da381852b941e860600e1fbacf49ff5c`; the 13-file patch manifest SHA256 is `b16c423b6999eaf9e986d7bada9fa6628007380de955d5fc9045d9e1a0126c55`. Evidence and full matrix: `.superpowers/sdd/2026-10-16-result-integration/task-11-report.md`.

Task 11 independent patch review approved implementation/spec compliance with no actionable findings. The reviewer verified all 494 barrier hashes and both earlier 168-case passes. The reviewed package is `review-task11-working-57d4d54c7bd5.diff`, SHA256 `57d4d54c7bd59e4f90b0db45be5e714957ebb145d0c0de192fa5d9c2fed33317`; preserve it until final validation and commit. The subsequent independent whole-branch review inspected source, tests and documentation, found no code issue, and identified one Important stale-runtime-status documentation issue. The acceptance record and linked guidance were corrected after that finding. The reviewer relied on controller-run gates and did not judge live installation.

Unix socket, systemd verification and display access were available in the approved unrestricted test execution. Task 11 was committed using authorized Git metadata access. Do not bypass Git restrictions for subsequent writes.

## Task 12 documentation and preview subset (2026-10-23)

The operator guide, README, changelog, local-core/task-workspace guidance, follow-up register and candidate acceptance record are committed. Independent subset review found one Important acceptance-sequence conflict; the original documentation worker corrected it, and `task-12-rereview.md` confirms no open or new findings. `task-12-review.md`, `task-12-report.md` and `task-12-evidence-20261023/` in the execution directory record the review, file scope, actual installer preview and documentation checks. Preview ran under private `/tmp/solace-integration-preview-1mcmw61d` with exit 0, mode `preview`, backup `null`. Proposed runtime is `f877c3c2f0ee11a0`, full payload digest `f877c3c2f0ee11a05d793fbcc149a111d8f798e3c05f5463c812209dca8c2937`, with all 44 Python modules. Three launcher/unit preview files were created only in `/tmp`. Source/test/packaging remained identical to Task 11's gate manifest; README and CHANGELOG are the two expected changed manifest entries. This preview is preparation evidence, not installed acceptance.

## Controller decisions and consequences

These are copied verbatim from the execution ledger, in order, to preserve decisions if session scratch is archived. Refresh this appendix from the ledger at final handoff. The execution skill requires exhaustive disclosure of rulings and their costs; do not drop them during compaction.

1. Ruling: Limit merge workers to two nonblocking slots and report merge_worker_busy as a whole-file conflict when saturated — prevents core worker starvation without guessing a merge — cost if wrong: extra manual conflicts under concurrent load.

2. Ruling: If combined symlinks are unsafe, allow complete-current fallback only when that graph validates and every reverted changed path is explicitly conflicted; unsafe current is refused — preserves a valid candidate without silent dropped changes — cost if wrong: more conflicts than a narrower dependency-aware merge.

3. Ruling: Replace the plan’s multiprocessing spawn mechanism with a small subprocess exec and deadline-controlled stdin/stdout — spawn can synchronously serialize unbounded preparation metadata before supervision, violating the approved two-second bound — cost if wrong: additional private worker-entrypoint/IPC code to maintain; public merge interfaces and scope stay unchanged.

4. Ruling: Repair SnapshotStore verification to stream through a shared bounded helper and reuse it in review/export, instead of duplicating metadata/profile/link validation to bypass its full-file read — the existing _verify joins complete regular files before a bounded view — cost if wrong: changing a reviewed adapter needs renewed preparation and review regression coverage. Public signatures, immutable verification and scope remain intact.

5. Ruling: Centralize retained B/R/C/M token authentication in the plan loader for approval/export/resolution as part of Task5 — current loader checks only M’s stored digest while reconstructing B/R/C from mutable rows; content now checks its original token separately — cost if wrong: a shared loader change requires corruption and preparation/review regression coverage. Preserve existing review_token API and token bytes.

6. Ruling: Make Journal.identity optional only for a durable reserved-but-uncreated journal — no physical inode exists until Task6 stages after reservation commit — cost if wrong: staging/publication/recovery must explicitly reject missing identity outside the reserved phase. reserve returns None identity; stage persists the actual identity before staged/applying use.

7. Ruling: Replace the impossible disjoint-source/parent-inside-another-source fixture with disjoint registered source roots where one occupies the predicted sibling journal path; keep ordinary ancestor-source refusal — a parent inside another source necessarily makes its child source overlap — cost if wrong: an exotic journal-placement condition may need an additional fixture. No placement policy is relaxed.

8. Ruling: Add an adapter-produced immutable verified-snapshot record so CAS verification happens before placement/coordinator locks while C/M/plan rows still commit atomically — SnapshotStore.insert currently rereads all CAS data inside its caller’s transaction — cost if wrong: the internal provenance/identity seam needs misuse, corruption and rollback regressions. Preserve insert’s existing wrapper signature; no public unchecked boolean or separately committed draft checkpoints.

9. Ruling: Treat registered-store ownership plus committed task.start intent/task claim as the encompassing cache reservation, with strict cache containment and owning-store relink exclusion — caches never reserve an independent external path — cost if wrong: a future cache path outside that store will need its own durable placement intent. No additional cache table or workspace_trees rewrite is introduced. Require focused live-cache adoption/relink evidence.

10. Ruling: Keep recursive workspace.relink private-file validation in its existing asynchronous worker after durable intent, removing the duplicate acceptance traversal — worker already rechecks identity/context, and synchronous traversal violates short global-lock ownership — cost if wrong: unsafe hardlink candidates now return a failed operation with the same safety error rather than synchronous rejection; regression must prove unchanged workspace and unrelated registration progress.

11. Ruling: Treat a crash after mkdir but before durable recording of its observed inode as ambiguous recovery, preserving the directory and claims rather than adopting/removing it — intent alone cannot distinguish an operation-created directory from a foreign winner, and the spec prohibits existence-only inference — cost if wrong: some mkdir interruption windows require explicit conflict inspection rather than automatic completion by resume; no staged-directory publication redesign in Task6. Require focused failpoint evidence and Task8 handoff.

12. Ruling: Task9 pins the specifically supplied approval ID/hash/binding in journal intent in the reservation transaction after exact token/binding/writers-paused validation; Task6 preserves that record — reserve/stage have no approval parameter and choosing latest or all matching approvals loses precise provenance — cost if wrong: public orchestration and private fixtures must share a tested intent-field contract; raw tokens must remain absent from filesystem evidence. No API/schema change.

13. Ruling: Task8 owns the verified location_json mapping and targeted finish metadata-comparison extension for relocated recovery; Task7 retains exact physical identity checks now — no typed/validated relocation mapping exists until the durable handoff is implemented, and original approval/history must not be rewritten — cost if wrong: Task8 must extend both publisher continuation and completion comparisons with relocation regressions; Task7 cannot finish moved-target recovery prematurely. No unchecked overlay flag.

14. Ruling: Extend Task8’s file scope to targeted WorkspacePaths logical pending-handoff guards; registry.py only if a demonstrated durable relink primitive is missing — all access and foreign placement routes converge at those guards, so enabling moved storage safely cannot rely on obsolete physical paths — cost if wrong: shared guard changes need ownership/adoption/relocation regressions and independently reviewed cross-project behavior. Reuse existing registry receipts where sufficient; never guess external journal moves or rewrite approval/history.

15. Ruling: Keep moved reserved-only/unidentified-journal recovery fail-closed instead of adding a markerless relocation/abandonment path — original-location absence plus C can prove no change, but a moved path cannot prove an unrecorded physical journal was never created or moved elsewhere before its inode was persisted — cost if wrong: an interrupted uncreated/unidentified reservation moved with storage can remain blocked for explicit diagnosis; no automatic claim release/adoption. Require clear error/show, refusal regression and documentation; staged identified journal relocation remains supported.

16. Ruling: Extend Task9 to narrow plan/export acceptance-reservation seams and separate retained catalog metadata/replay availability from CAS availability — existing private prepare/export reserve inside worker, and objects-directory validation before receipt lookup violates acceptance ownership and replay-first — cost if wrong: shared catalog/adapter changes need missing-CAS replay plus fresh-I/O refusal and identity/schema regression evidence. Preserve store UUID/SQLite/schema/physical validation, no silent CAS replacement or weakened live mutation guard; prefer smallest metadata seam.

17. Ruling: A distinct historical prepare request still enforces child-required/root-forbidden expected_target_revision shape using retained task ownership metadata, after same-request replay — the API’s context-specific field contract is independent of stale revision values and live availability — cost if wrong: new historical requests with invalid field shape are refused; targeted root/child tests must prove no CAS/live freshness check precedes historical return. Same-request fingerprints/replay and valid historical no-reapply behavior stay unchanged. Any source change after Task9 broad run is separately covered/reported.

18. Ruling: Task10 fixes export-for-review invalidating its own plan by binding only target-relevant ownership/protection boundaries (equal/ancestor/descendant), while retaining complete global placement/access guards — global expected-root equality makes a disjoint retained export stale the unchanged candidate, breaking the approved review workflow — cost if wrong: boundary filtering could miss a real target/journal overlap, so require new relevant-boundary stale tests plus unrelated/export stability and ownership/placement regressions. No token/revision refresh or guard bypass; one shared relevance helper. Evidence task-10-diagnose-export.log.

19. Ruling: Extend Task10’s boundary correction to the single integration_review.resolve protected-delta call, using fresh full boundaries rather than newly scoped expected roots — physical-identity exclusions must remain global even when approval binding ignores lexically unrelated roots — cost if wrong: a resolve-specific protection regression and final owner/broad checks are required. Immutable expected/token unchanged; no global guard is reduced.

20. Ruling: Respect current workspace-write sandbox and read-only Git metadata; finish code/tests/review using a base-plus-working-patch hash and leave commit pending rather than bypassing permissions — current policy allows workspace and /tmp writes only, with no escalation — cost if wrong: a later authorized agent must revalidate hashes and commit before claiming the original plan fully complete. No git-dir relocation/workaround, installation or live writes.

21. Ruling: Use an explicit /tmp disposable-test root and /tmp installer preview/evidence where needed, preserving original default fixture behavior outside the override — /var/tmp is no longer writable under the active sandbox — cost if wrong: location-specific behavior needs retained filesystem/identity coverage and the test-root override must stay test-only. No product storage-policy relaxation.

22. Ruling: Treat the real-process crash matrix as verification of existing recovery invariants when no recovery defect reproduces; do not require or manufacture a recovery implementation RED — the approved design requires honest fault-window proof and verified-gap fixes, while the plan cannot guarantee a preexisting bug — cost if wrong: crash confidence rests on exact barriers, negative invariants and independent test review rather than a demonstrated recovery regression. Retain genuine CLI RED/GREEN, harness/expectation failures separately; no recovery-fix claim.

23. Ruling: Prepare Task12 documentation and installer-preview subset while Task11 runtime/commit are environment-blocked, without declaring either task complete or final branch approval — these reversible artifacts make the requested fresh-agent handoff concrete and do not bypass acceptance — cost if wrong: preview/docs must be refreshed if later permitted-runtime validation changes source; whole-branch review and commit remain explicit pending gates.
