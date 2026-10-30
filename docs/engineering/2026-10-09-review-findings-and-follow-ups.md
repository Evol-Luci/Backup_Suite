# Review findings and follow-ups

**Reviewed:** 2026-10-09

**Worktree:** `codex/brainstorm`
**Scope:** Installed Solace source alignment and the first local checkpoint engine slice

This register consolidates the final whole-branch review, the scoped fix review, and the architecture work deferred by the checkpoint-engine plan. “Fixed” means covered by the reviewed engine changes; it does not describe the legacy `ProjectVault` implementation.

## Findings from the whole-branch review

| Priority | Area | Finding | Status |
|---|---|---|---|
| Critical | Legacy restore | `ProjectVault.restore_snapshot()` removed project contents before checking ZIP integrity. A damaged archive could destroy the current working tree. | **Fixed in this follow-up:** archive integrity and staged extraction precede the project swap; failed publication rolls back. Regression tests cover CRC corruption, unreadable archives, path traversal, reserved vault metadata, and publish rollback. |
| Important | Legacy compare | `compare_snapshot()` considered file size but not content when sizes matched, so edits such as replacing one same-length value with another were missed. | **Fixed in this follow-up:** same-size files are compared byte-for-byte using a fixed-size buffer; a regression test covers equal-length changed content. |
| Important | Legacy merge | Both GUIs call `apply_merge(..., None)`. Its cleanup path checks `os.path.exists(None)` after applying edits, so the UI can report failure after changing files. | **Fixed in this follow-up:** cleanup now runs only when `apply_merge` receives a temporary directory; tests cover both `None` and owned-directory cleanup. |
| Important | Engine error reporting | `ProjectRepository.create_checkpoint()` translated `CorruptObject` into “file changed during checkpoint capture.” The checkpoint still fails safely, but the diagnosis can cause futile retries. | **Fixed in this follow-up:** stored-object corruption keeps its `CorruptObject` type and verification message; a regression test confirms no checkpoint is added. |
| Important | Branch delivery | Installed-source alignment and architecture documents were uncommitted in the worktree. | **Fixed:** baseline and architecture documents were committed and merged in PR #1. |
| Verification gap | GUI | Existing Tk tests could not run because the session had no display at `:0`. | **Test execution verified 2026-10-13:** all seven Tk tests passed with desktop-display access outside the sandbox. Full interactive GUI rendering remains unverified. |

## Checkpoint-engine findings already fixed

- Store paths are resolved and checked by physical identity, so aliases cannot cause a project to capture its own store or use the project root as its store.
- Capture detects changes to an opened file during reading and refuses to publish a mixed capture.
- Linux filenames containing a literal backslash round-trip through capture and restore.
- Object and shard directory entries are synced through their parent directories before checkpoint metadata can be committed.
- Other reviewed fixes cover no-follow descriptor-relative traversal, symlink/FIFO replacement races, multi-chunk verification, rollback after partial database inserts, staged restore, concurrent destination creation, and interrupted object writes.

## System work and follow-ups (updated 2026-10-22)

These items are part of the intended Solace product direction, but are outside the first checkpoint-engine slice:

| Area | Work still needed |
|---|---|
| Local core | **Installed and live-verified:** private socket, registry, lifecycle/recovery, events, CLI and local-store checks. [Current acceptance](2026-10-15-task-workspaces-review-and-acceptance.md). |
| Agent task flow | **Installed and live-verified:** parent/direct-child records, isolated workspaces, ready results, task events, logical prompt retention, manual integration/review and receipts. [Integration acceptance](2026-10-23-result-integration-installed-acceptance.md). Configured checks and automatic policy remain deferred. |
| Human/agent interaction | Task and manual integration CLI/API actions are installed. GUI integration/review status remains deferred. |
| Version-control workflows | Understandable merge/conflict explanations, text merge flow, binary previews/locks, and Git import/export without live dual-write synchronization. |
| Storage lifecycle | Workspace/checkpoint references, retention, conservative garbage collection, local-folder replication, and later encrypted Google Drive storage. |
| Capture/restore limits | Cross-file application-consistent snapshots are not promised. Restore publication is atomic, but its completed tree is not yet promised durable across sudden power loss. Same-user hostile processes are outside the storage threat model. |
| Platform compatibility | Linux is first. Windows/WSL behavior and full GUI rendering remain unverified. |

## Suggested order

1. Design configured check profiles and execution against the installed exact-candidate and receipt contracts; automatic integration policy follows a verified check runner. The [approved architecture](../specs/local-first-agent-version-engine.md) defines the delivery order.
2. Preserve interactive GUI rendering as a separate verification item; the existing Tk test execution gap is closed.

## Proposed integration follow-ups (2026-10-16)

The [first integration design](../superpowers/specs/2026-10-16-result-integration-design.md) covers manual child-to-parent and parent-to-project planning, conflict resolution, recoverable application and immutable receipts. That slice is installed and passed [disposable acceptance](2026-10-23-result-integration-installed-acceptance.md). See the [operator guide](../result-integration.md) and earlier [source review](2026-10-16-result-integration-review-and-acceptance.md). These separable follow-ups remain tracked:

| Follow-up | Why separate / prerequisite |
| --- | --- |
| Configured check profiles and runner | Needs stable candidate-manifest IDs, invalidation rules and integration receipts first. The manual slice records that Solace did not run configured checks; it never invents a passing result. A later runner must bind real results to exact candidate/profile digests. |
| Automatic integration policy | Depends on trusted check-profile configuration, passing exact-candidate checks and a verified manual apply/recovery path. Manual approval remains the default. |
| Forward undo of an integration | Depends on durable before/after receipts and a decision on incremental submissions after a reverted contribution. Preserve immutable history; do not implement destructive rollback as undo. |
| GUI review/status and binary previews | Depends on the stable CLI/API plan, conflict, apply and receipt contracts. Existing GUI behavior remains unchanged during core design. |
| Rename detection and advisory binary locks | Path-based add/delete conflicts and whole-file selection provide an initial deterministic workflow; rename heuristics and lock ownership need separate design. |
| Integration at a mount root or beneath an unwritable parent | The first journal lives beside its target on the same filesystem. Supporting another journal placement needs a separate ownership, capture-exclusion and recovery design. |
| Integration into overlapping registered sources | The first slice refuses this target because projects have separate coordinators. Supporting shared roots needs cross-project claim and history semantics. |
| Deeper task nesting | Needs integration and lifecycle rules beyond parent/direct-child ownership. |
| Workspace/cache/journal retention and conservative GC | Needs the final reference model, abandoned-plan retention rules and active-operation/replication protection. No automatic deletion in the integration slice. |
| Git import/export, local-folder replication, later encrypted cloud targets | Remain in the approved architecture's delivery order; they depend on local integration/history being reliable. |

## Evidence

- Architecture: [`../specs/local-first-agent-version-engine.md`](../specs/local-first-agent-version-engine.md)
- Engine plan: [`../superpowers/plans/2026-10-09-local-checkpoint-engine.md`](../superpowers/plans/2026-10-09-local-checkpoint-engine.md)
- Final review and fix reports: `.superpowers/sdd/2026-10-09-local-checkpoint-engine/` in the worktree

## Verification for this follow-up

- `python -m unittest tests.test_project_vault_restore -v` — 6 tests passed.
- `python -m unittest tests.test_project_vault_compare -v` — 1 same-size content comparison regression test passed.
- `python -m unittest tests.test_project_vault_merge -v` — 2 merge cleanup tests passed.
- `python -m unittest tests.solace_engine.test_repository.RepositoryTests.test_checkpoint_capture_preserves_corrupt_object_error -v` — corruption error classification regression test passed.
- `python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v` — 49 tests passed.
- `python -m unittest discover -s tests -p 'test_*.py' -v` — GUI test limitation remains: 7 branch-visualizer Tk tests errored because the environment could not connect to display `:0`.
- `git diff --check` passed for the comparison code, tests, and register.

## Build continuation verification, 2026-10-13

Verified in `.worktrees/local-core-design`, branch `codex/local-core-design`, base commit `3e1ddc5`:

- `python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v` — 49 tests passed.
- `python -m unittest discover -s tests -p 'test_*.py' -v` — 16 tests passed, including all seven branch-visualizer Tk tests. The Tk tests initially failed to access `:0` inside the sandbox; the same command passed with approved desktop-display access outside the sandbox.
- Combined baseline: 65 passing tests. The two discovery commands cover separate test groups.
- The next core milestone is planned; service, registry, and new commands are not implemented or installed by this continuation.

## Local core implementation verification, 2026-10-13

The approved plan is implemented in the development branch. All 114 tests pass (49 core, 49 engine, 16 legacy/Tk). Generated systemd units pass verification, inherited-descriptor subprocess activation is tested, and installer tests use isolated temporary HOME/XDG roots. Real-home installation and user-manager activation have not been performed. See [the implementation review](2026-10-13-local-core-implementation-review.md) for evidence, scope limits, and rulings.

## Installed core acceptance, 2026-10-15

A separate reviewer identified five Important edge-case findings. Reproductions and bounded fixes passed, with 122 total automated tests (57 core, 49 engine, 16 legacy/Tk). The reviewed core was then installed and passed 28 real systemd/CLI acceptance checks, including deliberate process termination and automatic restart recovery. The final registry is empty and doctor is healthy; the small fixture store was preserved after unregister. See [the independent-review and live-acceptance record](2026-10-14-local-core-independent-review-and-acceptance.md) for evidence and rollback. Actual logout behavior and full interactive GUI rendering remain unverified.

## Configured checks source continuation, 2026-10-26

The source branch adds manually approved checks on isolated integration candidates. Installed-runtime acceptance and existing-store upgrades remain separate gates. See the [configured checks handoff](configured-checks-handoff.md) for current evidence and blockers. Automatic integration policy remains a later design and must define project-required minimum checks.

## Current continuation, 2026-10-29

The installed service now advertises `checks.v1` and schema 4, as observed through `solace core status --json`; this status observation does not replace a documented disposable installed acceptance. The [checked-queue design](../superpowers/specs/2026-10-27-automatic-integration-policy-design.md) is implemented in source at schema 5. It prepares and runs the exact required check profile after a ready result, then waits for human review, approval and paused-writer application. Its [source handoff](automatic-policy-handoff.md) records final review, tests, preview and the remaining installation gate.

The next separately scoped work is installed checked-queue acceptance, followed by forward undo and GUI review/status design. True automatic application still needs a writer-handoff contract; the checked queue never asserts that external editors or agents are paused. Existing project-store upgrades remain individual preview/apply decisions.
