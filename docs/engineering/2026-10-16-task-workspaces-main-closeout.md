# Task/workspace main closeout

Completed 2026-10-16 after the user approved closing out the feature branch and refreshing documentation.

- Local `main` fast-forwarded from `40c8d22` through `6ad4705`; the final implementation fix is `63e8452`.
- Fresh pre-merge verification: engine 52, core 124, legacy 16, all passing.
- Fresh merged-tree verification: engine 52 (1.928s), core 124 (62.037s), legacy 16 (0.479s), all passing. `git diff --check` passed.
- Refreshed the local-core guide, task guide, spec/plan status, follow-up register and project front door/changelog. The original implementation instructions and historical evidence remain in Git/archive history.
- Installed runtime remains `/home/lucievol/.local/share/solace/core-runtimes/59f3d8340f868d4a`; source integration did not reinstall or migrate anything. Installed core is healthy, PID 2591374, with no registered projects or pending recovery.
- Retired only the fully merged `codex/task-workspace-design` branch and its worktree. Other development worktrees remain untouched.
- No fetch, pull, push, remote merge, release or publication was performed.

## Evidence and rollback

[Before/after merge logs and merge output](/var/tmp/solace-task-workspaces-closeout-20261016-jx3n1shg) retain all three suite runs. The [installed acceptance record](2026-10-15-task-workspaces-review-and-acceptance.md) records the real runtime, disposable upgrade and rollback chain.

Before worktree removal, the unchanged installer was pinned read-only at `/var/tmp/solace-task-workspaces-closeout-20261016-jx3n1shg/rollback-core.py`, and its SHA256 was verified against the checkout. The updated rollback instructions use this standalone script; they no longer require the retired feature worktree. Backup directories and runtime snapshots were preserved. No rollback was executed, and no project database was downgraded.

## Next work

The installed milestone ends at saved results. The next architectural design is manual result integration through the CLI/API: child into parent, then parent into the source project. Merge/apply recovery and receipts come first; configured check execution, automatic policy, forward undo and the GUI need explicit scope and prerequisites. The [follow-up register](2026-10-09-review-findings-and-follow-ups.md) tracks deferred work. Designing that subsystem does not authorize modifying project files or installing an integration implementation.
