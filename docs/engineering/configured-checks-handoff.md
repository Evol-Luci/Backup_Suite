# Configured checks source handoff — 2026-10-25

## Exact state

- Managed worktree: `/home/lucievol/.codex/worktrees/da64/Backup_Suite`.
- Branch: `codex/configured-checks-design`; HEAD `73f30709c38f68475b9ceb5602d815b136789079` (`feat: select project check profiles for tasks`). Working tree was clean when this record was written.
- `main` remains `c2e34f2873a71f1fb28c8105d371468378522619`. No merge, push, installed-runtime change, or existing project-store upgrade occurred.
- Approved design: `docs/superpowers/specs/2026-10-24-configured-checks-design.md`. Approved nine-task plan: `docs/superpowers/plans/2026-10-24-configured-checks.md`.
- Ignored execution ledger, briefs, reports and independent-review packages: `.superpowers/sdd/2026-10-24-configured-checks/`. Its `progress.md` is the task-by-task authority for continuation. Preserve it.

## Completed source tasks

1. Schema 4 and explicit upgrade: commit `df0f5f5`, independent spec and quality review approved. Permitted core suite: 580/580, 243.645s.
2. Approved profile revisions: commit `900bc72`, independent spec and quality review approved. Focused 10/10; permitted core suite 587/587, 238.258s.
3. Task profile selection: commit `73f3070`, independent spec and quality review approved. Targeted 65/65; permitted core suite 593/593, 264.441s. The restricted suite was blocked by 64 socket errors and one systemd activation failure; `/tmp/task-3-full-suite.log` SHA-256 `456d676310b91b901843dbe022ca4b196205d5c9071831e683f3ddede5a7f18f` records that environment-only result. The permitted rerun passed.

The Task 3 reviewer confirmed that existing integration preparation requires the exact latest ready result. At run time, Task 6 must resolve the current profile revision. Task 4 must create private HOME/TMPDIR outside candidate M; Task 5 must launch with only the effective minimal environment; Task 8 must reject task-supplied commands/environment at the protocol boundary. These are deferred cross-task checks, not completed claims.

## Current blocker and next action

Task 4 (`task-4-brief.md`) has no source edits. Its first agent and two replacements stopped responding after initial analysis or before a file write, including after a narrow stage/verify assignment. The first Task 2 agent explicitly reported an agent usage limit. No Task 4 tests or implementation can be claimed. Resume with a responsive implementer under the approved subagent-driven workflow, starting from the brief. If an agent fails again, check its tool/usage state promptly; do not mistake silence for a running test. The worktree is outside the writable sandbox root: create patches under `/tmp`, then use approved escalated `cp` to write into it. Real Unix-socket/systemd test gates require permitted execution. The prior staging agent proposed consuming a reserved `check_runs` row, with separate candidate, HOME, TMPDIR and evidence locations, but this proposal is not implemented or reviewed.

After Task 4, execute Tasks 5–9 sequentially with a new implementer and independent task review each time, then run the plan's full source/socket/engine/test acceptance and whole-branch review. Task 9 must replace this interim handoff with final evidence and hashes. Installation and any real-store upgrade remain separate approval gates.
