# Changelog

## Unreleased — 2026-10-16

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

Result integration, configured checks, review receipts and GUI integration remain follow-up work; this milestone saves results without applying them to the original project.
