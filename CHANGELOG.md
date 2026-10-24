# Changelog

## Unreleased — integration candidate, 2026-10-22

### Added

- Manual child-to-parent and root-task-to-source integration: immutable B/R/C/M plans, bounded review pages, explicit resolution/export, exact-revision approval and unchecked acknowledgement, per-file journal publication and retained receipts.
- Explicit interrupted-apply resume/restore-before and confirmed, identity-checked relocation; restart does not write destination files automatically.
- Core schema 3 with explicit preview/apply upgrades and verified backups; schema 2 retains task support. New candidate stores use schema 3; existing stores are never migrated by opening or installation preview.
- Real-process crash matrix covering 168 cases and 247 durable barriers per integration/core gate, plus selected-socket and escaped-filename CLI guidance regressions.

### Documentation

- Added operator guide, candidate acceptance record, concrete installer preview and fresh-context handoff. The required source runtime gates and independent whole-branch review passed on 2026-10-23. This candidate is not installed.
- Recorded current sandbox socket/systemd/display failures without replacing them with historical passes. Runtime rollback preserves databases and cannot make schema-3 stores readable by old runtimes.

Configured checks remain `not_run_by_solace`. Automatic policy, check runner, forward undo, GUI, rename heuristics, deeper nesting, GC, replication and Git synchronization remain deferred.

## Installed task/workspace milestone — 2026-10-16

### Added

- Local checkpoint/object engine, private per-user core, project registry, durable operations/events and CLI/API access.
- Revisioned parent/direct-child task records, exact checkpoint ancestry, isolated reflink/copy workspaces and retained ready results.
- Exact prompts with logical retention, deletion, immediate expiry and body-free ordinary views/events.
- Explicit schema-1-to-2 preview/apply upgrade with a verified consistent backup, replayable receipts and installed rollback evidence.

### Fixed

- Workspace traversal, path ownership, mutable-hardlink relink, interrupted store relocation, large-response pagination, upgrade receipt replay and ancestor syncing regressions found during review.
- Empty `.git` directories no longer falsely claim an ancestor as a Git working tree. Populated, pointer, symlink and unreadable markers remain conservatively blocked.

### Documentation

- Recorded 192 passing tests and successful disposable installed acceptance on 2026-10-16.
- Refreshed installed-status guidance and preserved a standalone rollback script before source-branch cleanup.

This installed milestone saves results without applying them to the original project. The integration candidate above remains separate from this installed runtime.
