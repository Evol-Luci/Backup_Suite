# Checked Queue Integration Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After an eligible task becomes ready, durably prepare its integration candidate and run the project's required checks, leaving approval and apply to a person.

**Architecture:** Add explicit schema-5 policy and intent records to each project catalog. Project and task policy changes use the same preview/confirmation model as approved check profiles. A core-owned, nonblocking state machine drives existing prepare and check operations with stable request IDs and rechecks their persisted outcomes; approval/apply enforce the project-required profile and current policy revision.

**Tech Stack:** Python 3.15, SQLite/WAL, existing Unix-socket JSON protocol and CLI, `unittest`, systemd user service for final acceptance.

**Spec:** [2026-10-27-automatic-integration-policy-design.md](../specs/2026-10-27-automatic-integration-policy-design.md)

## Global Constraints

- Manual is the default; schema-1–4 stores retain their existing manual operations. Never upgrade a store on open.
- Automatic work never calls approve/apply or supplies `writers_paused`; it must not edit the parent or source target.
- Commands run with the user's authority, so only a locally confirmed project-approved profile may become required or run automatically.
- The required profile's exact ID, active revision and digest are policy-bound; task selection cannot weaken that rule.
- Every asynchronous stage is persisted and idempotent. Lost replies, stale bindings and interrupted commands do not become a pass.
- Do not alter installed runtime, user units or existing project stores during source implementation or source tests.

## File map

| File | Responsibility |
| --- | --- |
| `solace_core/policy_schema.py`, `schema.py`, `catalog.py` | Schema 5, new-store defaults, explicit backup/upgrade and version gates |
| `solace_core/project_policy.py` | Project policy preview, confirmation, profile binding and status |
| `solace_core/task_policy.py` | Effective task mode, owner-confirmed override and ready-state freeze |
| `solace_core/policy_approvals.py` | Required-profile checks at approval/apply and receipt provenance |
| `solace_core/policy_queue.py` | Durable intent insertion, nonblocking prepare/check reconciliation and status pages |
| `solace_core/policy_protocol.py`, `policy_cli.py` | Strict API and human/JSON CLI construction |
| `solace_core/service.py`, `task_operations.py`, `integration_operations.py`, `check_approvals.py`, `integration_commit.py` | Existing dispatch and transaction hooks |
| `tests/solace_core/test_policy_*.py`, `test_policy_acceptance.py` | Focused red/green and disposable socket acceptance |
| `docs/automatic-policy.md` | Operator workflow, pause boundary, upgrade and recovery guidance |

## Review Focus

- A profile edited between policy preview and apply must make the token stale (Task 2 test).
- A task selecting another approved profile must never satisfy the project-required profile (Tasks 3 and 4 tests).
- A changed destination or parent revision between ready and prepare must leave a pending reason without editing files (Task 6 test).
- A core crash after check acceptance must not launch the command twice or fabricate a pass (Task 6 test).
- An unchecked approval or approval created under an old policy must not apply when the current policy requires a check (Task 4 test).

---

### Task 1: Schema 5 and explicit migration

**Files:** Create `solace_core/policy_schema.py`; modify `solace_core/schema.py`, `catalog.py`, `service.py`, and all version gates found by `rg 'schema_version|in \(2, 3, 4\)|in \(3, 4\)|== 4' solace_core`; test `tests/solace_core/test_policy_schema.py`.

**Interfaces:** Produce `create_schema_v5(db)`, `require_policy(catalog)`, and schema-5 tables `project_integration_policy`, `task_integration_policy`, `integration_policy_intents`. The project row stores `revision`, `mode`, nullable required profile ID/revision/digest, `allow_task_opt_in`, actor and updated time. Task rows store task ID, mode, owning project policy revision and selected time. Intent rows store unique task/result/target identity, policy/profile binding, stage, stable prepare/check request IDs, operation IDs, plan/revision, run ID, reason and timestamps.

- [ ] Write `test_new_store_defaults_manual_schema_five`, `test_schema_four_upgrade_requires_preview_and_verified_backup`, and `test_older_store_manual_operations_remain_readable` with literal schema/default/backup assertions.
- [ ] Run `env SOLACE_TEST_TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/solace_core -p test_policy_schema.py -v`; observe the expected pre-implementation failures.
- [ ] Implement schema 5 creation, `4 -> 5` preview/apply with the existing consistent backup flow, capability metadata `policy.v1`, and version-5 support for all existing task/integration/check paths. Preserve the `1 -> 3 -> 4 -> 5` explicit upgrade sequence.
- [ ] Rerun the focused test and existing schema tests; run `git diff --check` and commit.

### Task 2: Project policy and owner confirmation

**Files:** Create `solace_core/project_policy.py`; modify `solace_core/service.py`; test `tests/solace_core/test_policy_project.py`.

**Interfaces:** Produce `ProjectPolicy.show()`, `.preview(request)`, `.apply(request)`, and `ProjectPolicy.current(db) -> dict`. Parameters: `mode` is `manual` or `checked_queue`; `required_profile` is nullable; `allow_task_opt_in` is boolean. The stored binding pins current active profile revision/digest and current project policy revision. Preview token expires in 900 seconds and is stored as a request receipt; apply requires `confirmed=True` and exact fresh binding, following `CheckProfiles`.

- [ ] Write tests for default view, confirmed opt-in, stale token after profile edit/retire, invalid `checked_queue` without a profile, and disabling without changing retained evidence.
- [ ] Run the focused test red; implement the methods and service dispatch; run it green.
- [ ] Run `test_check_profiles.py`, `test_policy_schema.py`, diff check and commit.

### Task 3: Task policy override and profile selection

**Files:** Create `solace_core/task_policy.py`; modify `solace_core/service.py`, `tasks.py`, `check_selection.py`; test `tests/solace_core/test_policy_task.py`.

**Interfaces:** Produce `TaskPolicy.effective(db, task_id) -> dict`, `.show(task_id)`, `.preview(request)`, `.apply(request)`. Override mode is `inherit`, `manual` or `checked_queue`. Owner-confirmed opt-in needs current project `allow_task_opt_in` and required profile. An override updates the task revision only in `created/active/blocked`; ready requires resume and a new result. Inheritance reads current project default; project full disable supersedes old task opt-ins.

- [ ] Write tests for inherited mode, manual opt-out without waiving required checks, confirmed task opt-in, project disable revocation, ready-state freeze, and different task-selected profile.
- [ ] Run the focused test red; implement the override and task show exposure; run it green.
- [ ] Run existing task/check-selection tests, diff check and commit.

### Task 4: Required checks at approval and apply

**Files:** Create `solace_core/policy_approvals.py`; modify `solace_core/integration_claims.py`, `check_approvals.py`, `integration_operations.py`, `integration_commit.py`; test `tests/solace_core/test_policy_approvals.py`.

**Interfaces:** Produce `PolicyApprovals.required(db, task_id) -> dict | None`, `PolicyApprovals.stamp(db, selected_run) -> dict`, and `PolicyApprovals.verify(db, approval_binding, plan) -> dict`. Schema-5 approval bindings include project policy revision and required profile ID/revision/digest. Refuse `--accept-unchecked` when required, reauthenticate before reservation and worker publication, and copy the policy decision into the receipt. Schema-1–4 behavior remains unchanged.

- [ ] Write tests for unchecked refusal, selected weaker profile refusal, current passing required run success, policy/profile change after approval, old-schema unchecked behavior, and receipt provenance.
- [ ] Run the focused test red; implement gates while reusing `CheckApprovals.evidence`; run it green.
- [ ] Run existing integration/check-approval and crash tests, diff check and commit.

### Task 5: Durable ready-result intents and read-only status

**Files:** Create `solace_core/policy_queue.py`; modify `solace_core/task_operations.py`, `service.py`; test `tests/solace_core/test_policy_queue.py`.

**Interfaces:** Produce `PolicyQueue.enqueue_ready(db, catalog, task_id, checkpoint_id, task_revision, now) -> str | None`, `PolicyQueue.list(catalog, after=0, limit=128) -> dict`, `PolicyQueue.show(catalog, intent_id) -> dict`. Enqueue happens inside the successful `task.ready` publication transaction; it snapshots target key/revision and policy/profile binding. One unique intent per task/result/target prevents duplicate work. Status pages omit check stdout and prompt bodies.

- [ ] Write tests for manual default creating no intent, enabled ready creating one intent, exact retry deduplication, child target revision snapshot, task/project policy change staleness, and bounded paged status.
- [ ] Run the focused test red; implement transactional enqueue and read-only status; run it green.
- [ ] Run task lifecycle/recovery tests, diff check and commit.

### Task 6: Nonblocking prepare/check state machine and restart

**Files:** Extend `solace_core/policy_queue.py`; modify `solace_core/service.py`, `maintenance.py` or add a small dedicated scheduler; test `tests/solace_core/test_policy_worker.py`.

**Interfaces:** Produce `PolicyQueue.tick(service, project_id, limit=...)` that advances persisted intents one stage without waiting for a same-project operation. Use deterministic UUID request IDs per intent/stage, actor `policy`, existing `integration.prepare` and `integration.check.run` acceptance, then inspect terminal operation/run records. Wake promptly after ready; periodic reconciliation covers restart/lost wakeups. State values: `queued`, `preparing`, `checking`, `pending_apply`, `pending`, `done`. Do not automatically rerun an interrupted check, approve or apply.

- [ ] Write tests for passing prepare/check ending at `pending_apply` with unchanged target, conflict/failure/missing profile pending reasons, stale target/policy, restart during accepted check without duplicate execution, and applied historical receipt as done.
- [ ] Run the focused test red; implement scheduler with short lock holds and persisted transitions; run it green.
- [ ] Run existing check-run/recovery/concurrency tests, diff check and commit.

### Task 7: Protocol, CLI, operator guide and real-socket acceptance

**Files:** Create `solace_core/policy_protocol.py`, `policy_cli.py`, `tests/solace_core/test_policy_protocol.py`, `test_policy_cli.py`, `test_policy_acceptance.py`, `docs/automatic-policy.md`; modify `solace_core/protocol.py`, `cli.py`, `task_cli.py`, `integration_cli.py`, `service.py`.

**Interfaces:** Expose `project.policy.{show,preview,apply}`, `task.policy.{show,preview,apply}`, `integration.queue.{list,show}`. CLI forms are `solace project policy ...`, `solace task policy ...`, and `solace integration queue ...`; preview/apply accept exact mode/profile/allow-opt-in fields and a confirmed plan token. Keep `--json` envelopes and actionable human errors.

- [ ] Write strict protocol/CLI tests and real Unix-socket acceptance: default manual, confirmed policy, required profile selection, ready result -> checked pending plan, no target mutation, human checked approval/apply, and schema-4-to-5 backup.
- [ ] Run the focused tests red; implement protocol/CLI and guide; run them green.
- [ ] Run full core, engine and top-level suites separately with `SOLACE_TEST_TMPDIR=/tmp`; inspect counts/failures and `git diff --check`. Commit.

## Final source gate

Review the whole branch against the spec; resolve Critical/Important findings. Re-run the three complete suites and disposable real-socket acceptance after the last code change. Record exact HEAD, test logs/hashes, schema backup and installed-runtime preview in a handoff. Installation, real-store upgrades, and source applications require their separate gates.
