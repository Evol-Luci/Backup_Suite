# Task/workspace candidate review and acceptance

Branch `codex/task-workspace-design`, product baseline `40c8d22`, verified implementation through `79111c2`. The approved [design](../superpowers/specs/2026-10-15-task-lifecycle-workspaces-design.md) and [plan](../superpowers/plans/2026-10-15-task-lifecycle-workspaces.md) govern this candidate. Installation is pending explicit approval.

## Verification

All **191 tests pass**: 52 engine, 123 core and 16 legacy. `git diff --check` passed. The baseline had 122 tests; no previous test was removed. Deliberate compatibility assertion changes support core schema 2, refuse schema 3, and refuse unregister when ownership cannot be checked in an unreadable catalog.

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 52 tests in 2.065s

OK
```

Evidence: [engine log](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/final-engine.log).
```bash
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 123 tests in 51.868s

OK
```

Evidence: [core log](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/final-core.log).
```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 16 tests in 0.512s

OK
```

Evidence: [legacy log](/var/tmp/solace-task-workspaces-20261015-review-wdupci0o/final-legacy.log).

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

Deferred minors: none established separately. These rulings explicitly cover every item the reviewer declined to judge: remediation, installed rollout, physical power loss, deferred milestones, documented exclusions, and the selective test audit.

## Concrete installation preview

- Candidate runtime: `/home/lucievol/.local/share/solace/core-runtimes/4681a85c8a74759d` (preview only; not installed).
- Current installed runtime: `/home/lucievol/.local/share/solace/core-runtimes/152406111fa424cb`; live service PID `2696041`, active; socket enabled. Current installed doctor is healthy and registry has zero projects.
- Changed targets: `/home/lucievol/.local/bin/solace` and `/home/lucievol/.config/systemd/user/solace-core.service`. The socket unit bytes are unchanged.
- Candidate unit verification: `systemd-analyze --user verify` returned 0 for staged units in `/tmp/solace-task-workspaces-4681a85c8a74759d-units`; this verifies unit syntax/wiring, not live startup.
- Fresh apply creates a uniquely named backup beneath `/home/lucievol/.local/share/solace/core-install-backups/`, preserving previous launcher/unit bytes and a rollback manifest. The exact new backup ID is allocated only on apply and must come from that apply result. Existing runtime and prior backup `20261015T111851Z-76a1c4a2` remain preserved.
- Supported core schemas: 1 and 2; new projects use 2. Engine/registry versions stay 1. Installation does not migrate existing catalogs. Runtime rollback never downgrades databases or erases workspace references.
- Disposable installed acceptance fixture reserved in this preview: `/home/lucievol/.local/share/solace/acceptance/task-workspaces-20261015-a12a16b1`. It has not been created.

After installation approval, the proposed commands are:

```bash
python /home/lucievol/Projects/Backup_Suite/.worktrees/task-workspace-design/packaging/install-core.py --apply > /var/tmp/solace-task-workspaces-20261015-review-wdupci0o/install-result.json
systemctl --user daemon-reload
systemctl --user enable --now solace-core.socket
systemctl --user restart solace-core.service
python /var/tmp/solace-task-workspaces-20261015-review-wdupci0o/installed-acceptance.py --run --fixture /home/lucievol/.local/share/solace/acceptance/task-workspaces-20261015-a12a16b1
```

The reviewed acceptance script requires an empty registry before creating fixtures. It verifies installed CLI/socket behavior, exact prompts, parent/two-child isolation, immutable result restores, ready status, restart persistence, replay after redaction, and absence of the fake prompt canary from the service journal. It cancels and unregisters fixtures while retaining their files. With the service/socket stopped, the retained old runtime creates a **new disposable genuine schema-1 store**; the candidate then previews/applies its upgrade, verifies the backup and task use, and checks upgrade replay after unregister. No existing project database is downgraded.

Rollback uses the **new apply result's** `rollback_command`, then `systemctl --user daemon-reload` and restoration of activation settings. Preserve that command and install result; do not substitute a historical backup ID. Already upgraded catalogs require a schema-2-capable runtime and remain intact.

Installation, installed acceptance, merge and remote publication have not occurred. The feature branch and worktree remain available at the approved installation gate.
