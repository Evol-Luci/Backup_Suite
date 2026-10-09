# Review findings and follow-ups

**Reviewed:** 2026-10-09

**Worktree:** `codex/brainstorm`
**Scope:** Installed Solace source alignment and the first local checkpoint engine slice

This register consolidates the final whole-branch review, the scoped fix review, and the architecture work deferred by the checkpoint-engine plan. “Fixed” means covered by the reviewed engine changes; it does not describe the legacy `ProjectVault` implementation.

## Findings from the whole-branch review

| Priority | Area | Finding | Status |
|---|---|---|---|
| Critical | Legacy restore | `ProjectVault.restore_snapshot()` removed project contents before checking ZIP integrity. A damaged archive could destroy the current working tree. | **Fixed in this follow-up:** archive integrity and staged extraction precede the project swap; failed publication rolls back. Regression tests cover CRC corruption, unreadable archives, path traversal, reserved vault metadata, and publish rollback. |
| Important | Legacy compare | `compare_snapshot()` considered file size but not content when sizes matched, so edits such as replacing one same-length value with another were missed. | Open; separate follow-up. |
| Important | Legacy merge | Both GUIs call `apply_merge(..., None)`. Its cleanup path checks `os.path.exists(None)` after applying edits, so the UI can report failure after changing files. | Open; separate follow-up. |
| Important | Engine error reporting | `ProjectRepository.create_checkpoint()` translates `CorruptObject` into “file changed during checkpoint capture.” The checkpoint still fails safely, but the diagnosis can cause futile retries. | Open; preserve corruption errors in a follow-up. |
| Important | Branch delivery | Installed-source alignment and architecture documents are uncommitted in the worktree. | Open; commit or otherwise preserve the intended baseline and docs before treating this as a mergeable branch. |
| Verification gap | GUI | Existing Tk tests could not run because the session had no display at `:0`. | Open; run in a graphical session or establish a supported headless test setup. |

## Checkpoint-engine findings already fixed

- Store paths are resolved and checked by physical identity, so aliases cannot cause a project to capture its own store or use the project root as its store.
- Capture detects changes to an opened file during reading and refuses to publish a mixed capture.
- Linux filenames containing a literal backslash round-trip through capture and restore.
- Object and shard directory entries are synced through their parent directories before checkpoint metadata can be committed.
- Other reviewed fixes cover no-follow descriptor-relative traversal, symlink/FIFO replacement races, multi-chunk verification, rollback after partial database inserts, staged restore, concurrent destination creation, and interrupted object writes.

## Deferred system work

These items are part of the intended Solace product direction, but are outside the first checkpoint-engine slice:

| Area | Work still needed |
|---|---|
| Local core | Per-user local service/socket, project registry, lifecycle management, and explicit local-filesystem/store placement checks. |
| Agent task flow | Parent/child tasks, isolated workspaces, task state/events, prompt retention/deletion policy, per-task and per-project automatic integration settings, and review gates. Solace manages work; Codex and Claude execute prompts. |
| Human/agent interaction | CLI/API as the canonical control surface, terminal-friendly review/gate actions, and GUI status for prompts and concurrent agent work. |
| Version-control workflows | Understandable merge/conflict explanations, text merge flow, binary previews/locks, and Git import/export without live dual-write synchronization. |
| Storage lifecycle | Workspace/checkpoint references, retention, conservative garbage collection, local-folder replication, and later encrypted Google Drive storage. |
| Capture/restore limits | Cross-file application-consistent snapshots are not promised. Restore publication is atomic, but its completed tree is not yet promised durable across sudden power loss. Same-user hostile processes are outside the storage threat model. |
| Platform compatibility | Linux is first. Windows/WSL behavior and full GUI rendering remain unverified. |

## Suggested order

1. Finish and review the staged legacy restore fix in this change.
2. Fix same-size comparison and merge error reporting in a separate legacy-safety change.
3. Correct the engine corruption error classification and settle GUI test execution in the target desktop environment.
4. Commit the installed-source alignment and architecture artifacts as an explicit baseline change.
5. Continue with the local core service/project registry, then the agent task lifecycle and CLI/GUI review surfaces.

## Evidence

- Architecture: [`../specs/local-first-agent-version-engine.md`](../specs/local-first-agent-version-engine.md)
- Engine plan: [`../superpowers/plans/2026-10-09-local-checkpoint-engine.md`](../superpowers/plans/2026-10-09-local-checkpoint-engine.md)
- Final review and fix reports: `.superpowers/sdd/2026-10-09-local-checkpoint-engine/` in the worktree

## Verification for this follow-up

- `python -m unittest tests.test_project_vault_restore -v` — 6 tests passed.
- `python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v` — 48 tests passed.
- `python -m unittest discover -s tests -p 'test_*.py' -v` — 6 restore tests passed; 7 branch-visualizer Tk tests errored because the environment could not connect to display `:0`.
- Scoped review approved the restore fix and issue-register status. `git diff --check` passed for the restore code, tests, and register.
