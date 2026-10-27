# Task/workspace review and installed acceptance

Implementation branch `codex/task-workspace-design` started at `40c8d22` and was integrated into local `main` through `6ad4705` on 2026-10-16; product code was verified through `63e8452`. The approved [design](../superpowers/specs/2026-10-15-task-lifecycle-workspaces-design.md) and [plan](../superpowers/plans/2026-10-15-task-lifecycle-workspaces.md) govern this candidate. Installation and disposable live acceptance completed on 2026-10-16. The active runtime is `59f3d8340f868d4a`.

## Verification

All **192 tests pass**: 52 engine, 124 core and 16 legacy. These complete suites were rerun after the installed acceptance exposed the empty-Git-marker defect. `git diff --check` passed. The baseline had 122 tests; no prior test was removed. Deliberate compatibility assertions support schema 2, refuse schema 3 and refuse unregister when task ownership cannot be checked in an unreadable catalog.

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 52 tests in 1.972s

OK
```

Evidence: [engine log](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6/corrected-engine.log).

```bash
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 124 tests in 67.513s

OK
```

Evidence: [core log](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6/corrected-core.log).

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 16 tests in 0.507s

OK
```

Evidence: [legacy log](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6/corrected-legacy.log).

The two end-to-end acceptance tests use a real same-user Unix socket and CLI in disposable directories. They prove independent same-filename edits in two children, exact immutable result restores, unchanged source Git/vault/hidden/binary/executable/link data, separate source history, retained earlier results after resume, awaiting-integration parent status, and progress in another project while preparation is blocked. Recovery coverage uses actual subprocess exits around staging, rename and SQLite publication, including relocated/unavailable storage, dirty finals, revision conflicts and guarded staging cleanup.

Prompt coverage verifies exact UTF-8/line endings, fixed retention, immediate expiry independent of purge, replay after deletion, bounded maintenance, and body-free ordinary views/events/receipts/watch output. Tk tests do not establish interactive GUI or actual logout behavior.

## Independent review and remediation

The fresh reviewer assessed **`40c8d22..9f79da2`**. Its original final-report delivery was interrupted by a usage limit; it subsequently supplied a consolidated report from that same analysis without inspecting remediation. It established **seven Important findings, no Critical finding, and no separate Minor finding**. Its pinned verdict was correctly **not ready to merge at `9f79da2`**.

All seven findings were reproduced with failing tests before their owning fixes. Commits `8edc5fe` and `79111c2` form one remediation pass. The following disposition is the author's regression verification; it is not an independent second review or rollout approval.

| Confirmed finding | Resulting behavior | Regression evidence |
| --- | --- | --- |
| Intermediate directory substitution redirected writes | Cache/workspace creation, reads, mode changes and syncing use held directory descriptors and nofollow traversal; unrelated files remain untouched | `test_substituted_intermediate_directory_never_receives_source_bytes`; supplemental cache creation/read substitution coverage |
| Missing global workspace/store ownership guards | New source/store locations and reservations protect other workspaces, retained paths, and intents across unregister/config changes | `test_store_placement_protects_retained_workspaces_after_config_change`; nested workspace relink regression |
| Relink accepted shared mutable hardlinks | Multiply linked regular files are refused with copy guidance, including revalidation before publication | `test_workspace_relink_refuses_other_workspace_and_hardlinks` |
| Registry relink success could strand catalog recovery after a crash | Registry receipt retains previous location; startup reconciles cache/intent metadata and records an atomic catalog marker before admitting new work | `test_project_relink_crash_reconciles_durably_before_recovery_and_replay`, actual exit 88 |
| Large observations broke socket inspection and subsequent mutations | Bounded samples/counts, complete retained change lists with token-pinned pages, byte-bounded task-list pages, and correlated frame-limit errors | Real socket with 6,000 long names; aggregate large-view paging; revision-inferred block/ready still succeed |
| Upgrade receipt failed after unregister | Original migration receipt replays; a new upgrade on an unregistered project is refused | `test_upgrade_replays_after_unregister_without_another_backup` |
| New ancestors lacked durability syncing | Every receiving ancestor directory is synced, including empty-source starts under a new multi-level root | `test_empty_start_syncs_every_new_workspace_ancestor` |

RED/GREEN logs, the original review package and reproductions, final suite output, and execution ledger are preserved in [solace-task-workspaces-20261015-review-wdupci0o](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o). The reviewer inspected tests selectively and did not observe its whole-suite run finish; the complete results above were freshly run and read by the implementer.

## Rulings and review exclusions

- Task 3: Ruling: /tmp has an existing .git directory on this machine — workspace fixtures use disposable /var/tmp directories rather than bypassing the approved Git-ancestor rule — cost if wrong: fixture portability needs a different non-Git temporary parent.
- Task 4: Ruling: cached bases intentionally live inside the store, whose paths the normal scanner excludes — add an explicit read-only inspect_tree(include_store=True) used only for complete materialized-tree verification — cost if wrong: cache integrity checks could omit data; capture defaults remain unchanged.
- Task 5: Ruling: SQLite publication must acquire the project lock before BEGIN, after scanning — add optional engine publication_guard rather than acquiring the lock inside a hook (which could deadlock with a concurrent lifecycle writer) — cost if wrong: publication coordination can stall; legacy default remains unchanged.
- Task 7: Ruling: the old regression allowed unregister with an unreadable store — change it to fail closed because task ownership cannot be proved absent, as the approved plan requires — cost if wrong: unregister requires storage return/relink even for a task-free unavailable project.
- Task 9: Ruling: auto-fetching a new revision on a retry changes its fingerprint — CLI mutations with an explicit --request-id require pinned --revision/--parent-revision flags; ordinary commands still infer current revisions — cost if wrong: power-user retries require additional flags, preserving exact request replay.
- Task 9: Ruling: keep task/workspace CLI parsing in focused task_cli.py rather than expanding the existing core CLI into one large module — cost if wrong: one additional internal module to navigate.
- Final: Ruling: protect locations from registered and retained catalogs after unregister/configuration changes, and serialize reservation admission with registration — unknown offline ownership must not allow mutable overlap — cost if wrong: an unavailable retained store blocks new placements until its recorded path is reconnected or a registered project is relinked.
- Final: Ruling: bound task/operation observation samples and add token-pinned pagination to cached workspace.status while retaining full catalog observations — unbounded comparisons otherwise make revision inference and all later mutations unusable over the 1 MiB socket — cost if wrong: consumers of large comparisons must use counts and pages.
- Final: Ruling: use the registry relink receipt as a durable hand-off and a catalog event as the atomic reconciliation marker — registry and catalog commits cannot be made one transaction, and opens must not migrate registry schema — cost if wrong: startup reconciliation reads retained receipt/event history and new relink receipts include previous_location.
- Final: Ruling: subsequent remediation commits were outside the pinned independent review — author regression verification and one final complete suite stand, as the approved inline workflow requires — cost if wrong: no independent second review of the fixes.
- Final: Ruling: installed runtime behavior and rollout were not reviewed or exercised — keep them behind the approved installation/disposable acceptance gate, with a concrete preview — cost if wrong: candidate tests do not prove the live user-manager rollout.
- Final: Ruling: physical power-loss behavior was not experimentally judged — rely on explicit fsync ordering, syscall instrumentation and process-exit recovery tests for this milestone — cost if wrong: hardware persistence behavior may differ from tested process loss.
- Final: Ruling: merge/integration, configured checks, deeper task nesting, Git import, GUI changes and garbage collection remain deferred — preserve the approved saved-results milestone — cost if wrong: users cannot use those broader workflows in this slice.
- Final: Ruling: process sandboxing, arbitrary sync detection, forensic prompt erasure, empty-directory preservation and application-consistent capture remain documented exclusions — external writers must cooperate and callers must accept engine limits — cost if wrong: these protections and preservation guarantees are unavailable.
- Final: Ruling: the reviewer selectively inspected tests and did not observe its complete suite run finish — author freshly ran all three complete suites and the report labels independent versus author evidence — cost if wrong: unexamined weaknesses in tests may remain despite passing coverage.

- Installed acceptance: Ruling: an empty `.git` directory is not a Git working tree — Git itself rejects the home directory as a repository, so ignore only empty directory markers while retaining conservative rejection of populated, pointer, symlink and unreadable markers — cost if wrong: an empty placeholder is admitted until repository metadata appears; later placement rechecks must catch that change.

Deferred minors: none established separately. These rulings explicitly cover every item the reviewer declined to judge: remediation, installed rollout, physical power loss, deferred milestones, documented exclusions, and the selective test audit.

## Installed runtime and live acceptance

The user approved installation and disposable acceptance on 2026-10-16. The initial candidate `4681a85c8a74759d` was installed with a verified backup, then acceptance exposed a placement false positive: `/home/lucievol/.git` is an **empty directory**, and Git reports that home is **not a repository**. Commit `63e8452` corrects that check and adds an end-to-end regression. Empty markers are ignored; populated, pointer, symlink and unreadable markers remain blocked. The full 192-test run passed before deploying the correction. No home Git marker was removed, no custom core configuration was created, and no alternate disk location was configured.

| Verified installed state | Value |
| --- | --- |
| Runtime | `/home/lucievol/.local/share/solace/core-runtimes/59f3d8340f868d4a` |
| Service | active, PID `2591374`; socket enabled |
| Default workspace root | `/home/lucievol/.local/share/solace/workspaces` |
| Doctor | healthy |
| Live registry after acceptance | empty |
| Runtime payload | every installed file matched the candidate manifest |
| Supported core schemas | 1 and 2; new projects use 2; engine/registry versions remain 1 |

The successful unmodified installed acceptance script ran from 13:18:46 to 13:18:52 UTC on 2026-10-16. Its fresh fixture is `/home/lucievol/.local/share/solace/acceptance/task-workspaces-20261016-2897659e`; immutable-result restores are preserved under `/var/tmp/solace-installed-results-task-workspaces-20261016-2897659e`.

Actual live checks passed:

- Exact prompt bytes, line endings and replay after logical deletion; fake prompt canary absent from ordinary task views and the service journal.
- Parent and two children use independent mutable inodes and retain separate same-filename edits; saved results restore exact A/B contents while original source Git/vault/hidden/executable/link files remain unchanged.
- Active external task states survive a service restart; ready parent/children report awaiting integration.
- Socket activation reloads the installed candidate, including after the service/socket were stopped for genuine legacy-store creation.
- The retained old runtime created a **new disposable schema-1 store**, which the candidate explicitly previewed/upgraded, then successfully used for a task. The consistent backup independently passed integrity checking at schema 1 while the current catalog is schema 2.
- Migration receipt replay succeeds after unregister. All four successful tasks are now cancelled and both projects unregistered; preserved workspaces remain available for retained inspection.

Successful project IDs: `8bc02e5d19f1405da0057242217c4e93` (parent plus two children), `9f7db594497b4a45ab965341311b9df8` (genuine schema-1 upgrade). The migration backup is `/home/lucievol/.local/share/solace/projects/9f7db594497b4a45ab965341311b9df8/schema-backups/387b997c-41a7-4439-b251-257469e0d23b/checkpoints.sqlite3`.

The earlier failed attempt at fixture `task-workspaces-20261015-a12a16b1` produced only one created task and no workspace. Its prompt was logically redacted, task cancelled and project `fd9164711ffc401cbf07a9178445fb71` unregistered. Files and its store were preserved. This failure and the RED/GREEN regression are retained rather than hidden.

Evidence: [successful acceptance](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6/retry-acceptance-stdout.json), [final doctor](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6/final-doctor.json), [service state](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6/final-unit.txt), [runtime and migration-backup verification](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6/runtime-and-backup-verification.json), and [installation run directory](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-run-20261016-br0xoco6). Earlier review evidence remains intact in the parent archive.

## Rollback

Both immutable runtimes and both uniquely named install backups are preserved:

- Pre-feature runtime `152406111fa424cb`: backup `/home/lucievol/.local/share/solace/core-install-backups/20261016T131221Z-c1ea3f1b`.
- First feature runtime `4681a85c8a74759d`: backup `/home/lucievol/.local/share/solace/core-install-backups/20261016T131839Z-2e037263` made immediately before the placement correction.

The second backup's previous bytes match the first candidate's installed digests, and current launcher/unit bytes match the corrected install result. To return fully to the pre-feature runtime, roll back these file changes in reverse order:

```bash
python /home/lucievol/.local/share/solace/core-rollback-tools/e77eb01bf4e57e51/rollback-core.py --rollback /home/lucievol/.local/share/solace/core-install-backups/20261016T131839Z-2e037263
python /home/lucievol/.local/share/solace/core-rollback-tools/e77eb01bf4e57e51/rollback-core.py --rollback /home/lucievol/.local/share/solace/core-install-backups/20261016T131221Z-c1ea3f1b
systemctl --user daemon-reload
systemctl --user restart solace-core.service
```

These rollback commands are recorded, **not executed**. The installer script is pinned read-only in the persistent core rollback-tools directory so source-worktree cleanup cannot break rollback. The socket was enabled before installation and remains enabled. Runtime rollback preserves stores and never downgrades databases; the older runtime refuses schema-2 catalogs, whose task/workspace references remain intact and need a compatible runtime. No existing user project was migrated; only disposable acceptance stores were created/upgraded.

Installation and installed acceptance are complete. Local `main` was fast-forwarded through `6ad4705` and all 192 tests passed again on the merged tree. The merged feature branch/worktree were retired after pinning the rollback script outside the worktree. No remote publication occurred. See the [main closeout record](2026-10-16-task-workspaces-main-closeout.md).
