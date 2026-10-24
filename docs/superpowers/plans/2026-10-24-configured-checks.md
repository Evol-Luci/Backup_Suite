# Configured Integration Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run project-approved local checks on an exact unpublished integration candidate and preserve evidence that can authorize a manual checked approval.

**Architecture:** Add explicit schema 4 for immutable profile revisions, task selection, run/step evidence, and checked approvals, while preserving schema-3 unchecked records. A core-owned asynchronous runner verifies and materializes M into a private store directory, executes the selected profile there, then commits a passing result only after fresh binding checks. The existing apply journal and receipt path accept either an exact checked approval or an explicit unchecked approval.

**Tech Stack:** Existing Python standard library and unittest, SQLite, content-addressed checkpoint engine, Linux descriptor-relative filesystem operations and process groups, Unix socket protocol v1. No new runtime dependency.

**Spec:** [Approved configured-checks design](../specs/2026-10-24-configured-checks-design.md), approved 2026-10-24. Baseline `c2e34f2`; design commit `e15e580` on `codex/configured-checks-design`.

**Status:** Plan awaiting user review and execution-method choice. No product task below has started. Managed worktree: `/home/lucievol/.codex/worktrees/da64/Backup_Suite`.

## Global Constraints

- Only project-approved command profiles define argv, environment, timeout and output caps. Tasks select an approved profile ID; they never supply commands or environment.
- The profile runs in an owned private copy initially verified against the exact candidate M. Its writes never enter M, a workspace, or source.
- Check commands are trusted same-user code, not sandboxed. Configuration preview must show that authority and require a fresh token on apply.
- Each run binds plan/revision, review token, candidate checkpoint/manifest, capture profile, task selection, profile revision/digest and target expected state. Changed bindings cannot pass or approve.
- Start process steps in a group; timeout/cancel sends TERM then KILL. A waiting client disconnect does not cancel the core-owned run.
- Hash and count complete stdout/stderr streams; retain only bounded captures. Never put raw output in events or unbounded socket responses.
- Preserve schema-3 unchecked approval/receipt meaning. New checked records must not satisfy the old `accepted_unchecked=1` constraint by lying.
- Upgrade 3→4 only through preview, verified consistent backup and apply. Schemas 1/2 keep their existing first upgrade to 3; no silent migration.
- Manual approval remains required. No auto integration, GUI control, remote runner, installed-store migration, source application to user projects, installation, remote push or release is part of this source plan.

## Review Focus

1. A profile command floods both pipes beyond capture limits: drain and hash all bytes, bound retained captures, and avoid deadlock (Task 5).
2. A runner dies after a child starts but before pass commit: restart marks the run interrupted and never invents a pass; signal only a safely identified surviving group (Task 6).
3. The candidate or selected profile changes while a command runs: final status is stale, and no approval token can use it (Tasks 6 and 7).
4. A schema-3 unchecked approval is read after 3→4 migration: its approval and receipt still say `not_run_by_solace` (Tasks 1 and 7).
5. A staged path or ancestor becomes a symlink or changes identity before cleanup: preserve it and report blocked cleanup without following it (Task 4).

## File and interface map

| File | Responsibility | Owner task |
| --- | --- | --- |
| `solace_core/check_schema.py` | Schema-4 DDL and constraints | 1 |
| `solace_core/schema.py`, `catalog.py`, `service.py`, `workspace_paths.py`, `diagnostics.py`, `maintenance.py`, `task_operations.py`, `tasks.py`, `integration_claims.py`, `integration_plans.py` | Upgrade and version-4 compatibility at existing version guards | 1, 3, 6 |
| `solace_core/check_profiles.py` | Profile validation, canonical digest, preview/apply/retire/read | 2 |
| `solace_core/check_selection.py` | Task selection and ready-result pin | 3 |
| `solace_core/check_staging.py` | Candidate copy, byte/mode verification, safe owned cleanup | 4 |
| `solace_core/check_process.py` | Process group execution, streaming digests/caps, timeout/cancel | 5 |
| `solace_core/check_runs.py` | Durable reservation, dispatch, status, cancellation, binding recheck and restart | 6 |
| `solace_core/check_approvals.py`, `integration_claims.py`, `integration_operations.py`, `integration_commit.py` | Checked approval and receipt verification alongside unchanged legacy mode | 7 |
| `solace_core/check_protocol.py`, `protocol.py`, `check_cli.py`, `integration_cli.py`, `task_cli.py`, `cli.py` | Strict socket/CLI requests and human guidance | 8 |
| `tests/solace_core/test_check_schema.py`, `test_check_profiles.py`, `test_check_selection.py`, `test_check_staging.py`, `test_check_process.py`, `test_check_runs.py`, `test_check_approvals.py`, `test_check_protocol.py`, `test_check_cli.py`, `test_check_acceptance.py` | Named owner tests | 1–9 |
| `docs/configured-checks.md`, `docs/result-integration.md`, `docs/engineering/2026-10-09-review-findings-and-follow-ups.md` | Operator workflow and precise status | 9 |

### Shared contracts

`CheckProfile` is the canonical JSON document `{version:1,name,steps:[{name,argv:[...],timeout_seconds,output_limit_bytes}],environment:{...}}`. Use `canonical()` from `integration_snapshots.py`; digest is SHA-256 of UTF-8 canonical bytes. A step has a printable display name, at least one nonempty argv element, no NUL, a timeout of 1–3600 seconds and a capture cap of 1024–1048576 bytes **per stream**. At most 32 steps and 64 environment pairs; names are 1–80 printable characters. `PATH` defaults to `/usr/local/bin:/usr/bin:/bin`, `LANG` to `C.UTF-8`; `HOME` and `TMPDIR` point to owned private run directories outside M. The profile's environment may override `PATH` and `LANG`; reject NULs and invalid variable names. Do not inherit the core's other environment, including credential variables. The effective environment template is included in the profile digest.

Profile preview returns `{profile_id?, name, next_revision, digest, commands, authority_warning, plan_token}`. Apply needs that exact `plan_token` and a local confirmation flag, rechecks current project/profile revision in one transaction, and stores an immutable revision. The token binds project/store identity, current revision, proposed canonical profile and action (`create/update/retire`). `actor` is an audit label, never proof of ownership. Same-user hostile processes are outside the trust boundary.

Store selection in `task_check_profiles(task_id,profile_id,selected_revision,selected_at)`. At root creation, absence means no selected profile; a child copies its parent's selected profile ID. `task.check-profile.select(project,task,profile,expected_revision)` changes it only in `created/active/blocked`, with task revision and replay update. A ready task must be explicitly resumed and produce a new ready result before selection changes. Profile revision is resolved at run time, not baked into task creation; a profile edit makes old runs ineligible for new approval.

`CheckBinding` is canonical JSON with `plan_id,revision,review_token,candidate_id,manifest_sha256,capture_profile_sha256,expected_sha256,task_id,task_revision,profile_id,profile_revision,profile_digest`. Use the retained plan and `IntegrationClaims.validate(plan,scan=True)` to build it, plus the selected active profile revision. Run result states are `reserved/running/passed/failed/timed_out/cancelled/interrupted/stale`; only `passed` is eligible. Each run has one core `operations` row, one `check_runs` row, zero or more immutable terminal `check_steps` rows, stage path/identity, and explicit cleanup state. Retained capped stdout/stderr are private 0600 files under `store/check-evidence/RUN_ID`, separate from removable `store/check-runs/RUN_ID`; step rows pin paths, hashes and byte counts. Neither location is GC eligible in this slice. A running process group's leader PID and Linux start-time identity are recorded before waiting; on restart signal only if that identity still matches. Uncertain survivors and unsafe stage cleanup remain visible for operator inspection.

API methods, all requiring `project`: `project.check-profile.list` (`after?,limit?`), `show` (`profile_id`), `preview` (`action,profile_id?,definition?`), `apply` (same fields plus `plan_token,confirmed=true`); action is `create/update/retire`, profile ID is required for update/retire, and definition is required for create/update. `task.check-profile.select` takes `task,profile,expected_revision`; `integration.check.run` takes `plan,revision,profile` and returns `operation_id,run_id`; `show` takes `run`, `list` takes `plan,after?,limit?`, and `cancel` takes `run` and returns promptly with `cancelling` or a terminal status. Cancellation is replay-safe; a disconnected `--wait` client does not cancel. `integration.approve` takes the existing `plan,revision,token` plus exactly one of `accept_unchecked=true` or `check_run=RUN_ID`. The latter still requires the review token and does not treat a run ID as a secret capability. CLI spelling follows `solace integration check ...` and `solace project check-profile ...`. Keep protocol version 1 and the 1 MiB frame ceiling; page limits remain at most 256.

For checked approval, add a new `integration_checked_approvals` table with the selected run ID and immutable binding; do not rebuild or reinterpret the schema-3 `integration_approvals` table. Approval lookup checks both tables and rejects ambiguity. `integration_receipts.details_json` records `checks={status:'passed',run_id,profile_id,profile_revision,profile_digest,steps:[bounded summaries]}` or the historical `not_run_by_solace` string. A checked approval must be reauthenticated at apply reservation and receipt commit without rerunning commands; profile edits before apply make it stale. Existing unchecked paths and historical JSON remain readable verbatim.

---

### Task 1: Schema 4 and compatibility

**Files:** Create `solace_core/check_schema.py`, `tests/solace_core/test_check_schema.py`; modify `solace_core/schema.py`, `catalog.py`, `service.py`, `workspace_paths.py`, `diagnostics.py`, `maintenance.py`, `task_operations.py`, `tasks.py`, `integration_claims.py`, `integration_plans.py` and existing schema tests at their version guards.

**Interfaces:** `create_schema_v4(db: sqlite3.Connection) -> None`; `require_checks(catalog) -> None`; `preview_upgrade(catalog)` and `apply_upgrade(catalog,plan_token,request)` retain their public signatures.

- [ ] **Step 1: Write failing tests.** `test_v3_preview_targets_v4_and_requires_fresh_token` checks target 4, backup integrity and stale token refusal; `test_v1_v2_still_upgrade_to_v3_first` checks the existing first hop; `test_v4_manual_integration_and_task_paths` runs existing actions after upgrade; `test_v3_unchecked_rows_remain_unchanged` compares historical approval/receipt JSON byte-for-byte.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_schema.py' -v`; expect failures for missing v4 DDL/upgrade.
- [ ] **Step 3: Implement.** Add `check_profiles`, `check_profile_revisions`, `task_check_profiles`, `check_runs`, `check_steps`, and `integration_checked_approvals` with FK, state, digest and immutability constraints. Extend upgrade snapshot/token with v4 DDL digest; use the existing consistent backup and replay-first flow. Widen **only** existing schema-3 integration/task guards to include 4; check commands require exactly 4. On project move/relink, an active check run prevents unsafe rebasing; terminal retained paths remain owned.
- [ ] **Step 4: Confirm green and regressions.** Run the new test, `test_integration_schema.py`, `test_task_schema.py`, and `test_task_recovery.py`; expect all pass.
- [ ] **Step 5: Commit.** `git add` these files and commit `feat: add explicit check schema upgrade`.

### Task 2: Approved profile revisions

**Files:** Create `solace_core/check_profiles.py`, `tests/solace_core/test_check_profiles.py`.

**Interfaces:** `validate_profile(value: dict) -> dict`; `CheckProfiles.preview(request: dict) -> dict`; `CheckProfiles.apply(request: dict) -> dict`; `CheckProfiles.show(profile_id: str) -> dict`; `CheckProfiles.list(after: int = 0, limit: int = 128) -> dict`; `CheckProfiles.active(db, profile_id: str) -> dict`.

- [ ] **Step 1: Write failing tests.** `test_preview_apply_requires_exact_fresh_confirmation` checks create/update/retire, revision, token, warning and request replay; `test_profile_rejects_shell_string_nul_duplicate_name_and_bad_limits` checks validation; `test_task_actor_string_is_not_authority` checks that actor alone never bypasses preview token/confirmation; `test_profile_output_is_bounded_and_retired_revision_readable` checks paged reads.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_profiles.py' -v`; expect missing interface failures.
- [ ] **Step 3: Implement.** Hash exact canonical specification; bind preview token to the current active revision and store identity; add immutable revision rows and audit events. Retire only the active selection for new runs; retain old revisions and run references. Refuse an edit/retire while a checked apply journal using that profile is active, so a command update cannot invalidate receipt commit after file publication. Use transaction-local `TaskCatalog.lookup_request/save_request` for replay.
- [ ] **Step 4: Confirm green.** Run the profile test and `test_task_prompts.py`; expect all pass.
- [ ] **Step 5: Commit.** Commit `feat: add approved check profiles`.

### Task 3: Task profile selection

**Files:** Create `solace_core/check_selection.py`, `tests/solace_core/test_check_selection.py`; modify `solace_core/tasks.py`.

**Interfaces:** `CheckSelection.select(request: dict) -> dict`; `CheckSelection.selected(db, task_id: str) -> dict | None`; `CheckSelection.copy_parent(db, parent_id: str, child_id: str, now: str) -> None`.

- [ ] **Step 1: Write failing tests.** `test_root_select_and_child_inherit` checks approved profile ID and child copy; `test_task_can_override_with_another_approved_profile` checks the user's chosen workflow; `test_selection_freezes_at_ready_and_requires_new_result` checks revision/state behavior; `test_missing_or_retired_profile_is_precise` checks error and next step; `test_selection_replay_is_idempotent` checks request IDs.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_selection.py' -v`; expect missing interface failures.
- [ ] **Step 3: Implement.** Root task creation accepts optional approved profile ID; child creation inherits by default and may override with another approved ID. Selection mutation increments task revision and uses the existing task claim guard; `task.show` and integration show display selection. Do not modify capture `profile_json` or prompt policy.
- [ ] **Step 4: Confirm green.** Run selection, `test_task_records.py`, `test_task_lifecycle.py`, and `test_integration_prepare.py`; expect all pass.
- [ ] **Step 5: Commit.** Commit `feat: select project check profiles for tasks`.

### Task 4: Private exact-candidate staging

**Files:** Create `solace_core/check_staging.py`, `tests/solace_core/test_check_staging.py`; modify `solace_core/workspace_paths.py`, `solace_core/integration_plans.py` for retained run-location ownership.

**Interfaces:** `CheckStaging.stage(run_id: str, plan: PreparedPlan) -> StagedCheck`; `CheckStaging.verify(stage: StagedCheck, plan: PreparedPlan) -> None`; `CheckStaging.cleanup(run_id: str) -> dict`. `StagedCheck` carries owned root path and identity, candidate manifest and run-private HOME/TMPDIR paths.

- [ ] **Step 1: Write failing tests.** `test_stage_matches_m_without_touching_source_or_workspace` checks bytes/modes/link text and inode separation; `test_corrupt_cas_or_substituted_ancestor_refuses_launch` checks verifier; `test_stage_identity_change_blocks_cleanup` checks no symlink following/deletion; `test_command_artifact_is_not_candidate` checks M remains unchanged.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_staging.py' -v`; expect missing interface failures.
- [ ] **Step 3: Implement.** Reuse `IntegrationReview._materialize` and `SnapshotStore.entries` with verified bytes, then independently compare staged entries/modes against M before launch. Reserve `store/check-runs/RUN_ID` and `store/check-evidence/RUN_ID` before creation; hold no live target write lock during command execution. Use descriptor-held directory traversal, restrictive modes, fsync, recorded identities and conservative stage cleanup. Keep evidence separate and registered as owned in placement/scan guards.
- [ ] **Step 4: Confirm green.** Run staging tests and `test_integration_review.py`; expect all pass.
- [ ] **Step 5: Commit.** Commit `feat: stage exact integration candidates for checks`.

### Task 5: Bounded local process execution

**Files:** Create `solace_core/check_process.py`, `tests/solace_core/test_check_process.py`.

**Interfaces:** `CheckExecutor.execute(step: dict, stage: StagedCheck, cancel: threading.Event, on_start: Callable[[int,int],None]) -> StepResult`; `StepResult` includes exit/signal/timeout/cancel, timestamps, complete-stream SHA-256 and byte counts, capped byte captures and truncation flags.

- [ ] **Step 1: Write failing tests.** `test_large_dual_streams_are_drained_hashed_and_capped` verifies full digests and no deadlock; `test_timeout_kills_process_group` includes a spawned child; `test_cancel_kills_process_group_and_does_not_pass` checks explicit event; `test_launch_error_and_signal_have_distinct_status` checks precision; `test_environment_omits_core_secret_and_runs_fixed_argv` checks no implicit shell and minimal environment.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_process.py' -v`; expect missing interface failures.
- [ ] **Step 3: Implement.** Use `subprocess.Popen(..., start_new_session=True)` and nonblocking pipe draining/selectors; update hashes after cap. On timeout/cancel TERM then bounded grace then KILL the group, wait/reap, and close streams. Record Linux process leader start time with PID for recovery; never signal an unverified reused PID. No shell unless configured as argv executable.
- [ ] **Step 4: Confirm green.** Run process tests; expect all pass with no surviving test child.
- [ ] **Step 5: Commit.** Commit `feat: run bounded local check steps`.

### Task 6: Durable run orchestration and restart

**Files:** Create `solace_core/check_runs.py`, `tests/solace_core/test_check_runs.py`; modify `solace_core/service.py`, `catalog.py`, `workspace_paths.py`, `integration_plans.py`.

**Interfaces:** `CheckRuns.start(request: dict) -> dict`; `CheckRuns.run(operation_id: str, request: dict) -> None`; `CheckRuns.show(run_id: str) -> dict`; `CheckRuns.list(plan_id: str, after: int = 0, limit: int = 128) -> dict`; `CheckRuns.cancel(request: dict) -> dict`; `CheckRuns.recover(db: sqlite3.Connection) -> None`; `CheckRuns.binding(plan: PreparedPlan, profile: dict) -> dict`.

- [ ] **Step 1: Write failing tests.** `test_pass_and_fail_are_durable_and_replayable` checks reservation, ordered steps, stable run ID on replay and operation state; `test_candidate_profile_target_change_marks_stale` checks all binding fields; `test_wait_disconnect_does_not_cancel` checks core ownership; `test_restart_interrupts_without_false_pass` checks crash edges, safe group identity and retained cleanup; `test_cancel_then_replay_is_idempotent` checks cancellation; `test_stage_replacement_preserves_evidence_and_reports_cleanup_blocked` checks safety; `test_capped_evidence_files_are_private_and_separate` checks 0600 captures survive safe stage cleanup.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_runs.py' -v`; expect missing interface failures.
- [ ] **Step 3: Implement.** Reserve operation/run/stage/evidence ownership in one transaction, schedule on the existing coordinator, execute outside long project locks, and persist each step plus its capped evidence files before advancing. Revalidate plan/profile/target under coordinator lock before a single transaction marks pass. Keep a cancellation registry in the core; `cancel` sets its event and returns promptly, while `show` reports terminal status later. Service shutdown requests cancellation before waiting for workers. Startup marks incomplete check operations interrupted; it checks process identity before signaling and preserves uncertain stage/evidence paths. Distinguish operational failure from a check command's nonzero exit.
- [ ] **Step 4: Confirm green.** Run run tests plus `test_task_concurrency.py`, `test_integration_claims.py`, `test_task_recovery.py`; expect all pass.
- [ ] **Step 5: Commit.** Commit `feat: persist and recover integration check runs`.

### Task 7: Checked approval and immutable receipt

**Files:** Create `solace_core/check_approvals.py`, `tests/solace_core/test_check_approvals.py`; modify `solace_core/integration_claims.py`, `integration_operations.py`, `integration_commit.py`, `integration_review.py`.

**Interfaces:** `CheckApprovals.approve(request: dict, plan: PreparedPlan, binding: dict) -> dict`; `CheckApprovals.lookup(db, plan_id: str, revision: int, raw_token: str) -> dict`; `CheckApprovals.verify(db, selected: dict, plan: PreparedPlan) -> dict`. Existing `IntegrationClaims.approve` dispatches checked or unchecked; `IntegrationOperations._approval` and `IntegrationCommit._metadata` accept either immutable mode.

- [ ] **Step 1: Write failing tests.** `test_checked_approval_requires_current_pass_and_review_token` checks success and missing/failed/stale runs; `test_profile_edit_revokes_pending_approval` checks apply refusal; `test_checked_receipt_retains_step_summary_and_candidate_digest` checks immutable detail; `test_schema3_and_migrated_unchecked_approval_stay_unchecked` checks exact legacy meaning; `test_tampered_run_or_binding_blocks_receipt_commit` checks final gate.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_approvals.py' -v`; expect checked path failures.
- [ ] **Step 3: Implement.** Use separate checked-approval rows and existing random approval tokens. Reuse `IntegrationClaims.validate` and compare canonical run binding at approval, apply reservation and `IntegrationCommit.finish`. Preserve unchecked branch behavior, including schema-3 request replay. Include bounded checked evidence in receipt; never copy raw output to the receipt or event.
- [ ] **Step 4: Confirm green.** Run approval, `test_integration_commit.py`, `test_integration_publish.py`, `test_integration_crashes.py`; expect all pass.
- [ ] **Step 5: Commit.** Commit `feat: authorize manual integration with passed checks`.

### Task 8: Socket and CLI surface

**Files:** Create `solace_core/check_protocol.py`, `check_cli.py`, `tests/solace_core/test_check_protocol.py`, `test_check_cli.py`; modify `solace_core/protocol.py`, `service.py`, `cli.py`, `task_cli.py`, `integration_cli.py`.

**Interfaces:** `validate_check(method: str, params: dict) -> None`; `check_cli.add_parsers(commands, project_actions) -> None`; `check_cli.prepare(client,args,project,request_id=None) -> tuple[str,dict]`. Existing `integration.approve` remains wire version 1 and requires exactly one approval mode.

- [ ] **Step 1: Write failing tests.** `test_protocol_rejects_extra_fields_bool_ints_bad_ids_and_unbounded_profiles` checks strict frames; `test_cli_preview_confirmation_and_run_wait` checks method construction; `test_cli_request_id_requires_pinned_mutations` checks retry stability; `test_human_output_distinguishes_failed_stale_passed_and_unchecked` checks next steps; `test_one_megabyte_frame_and_bounded_pages` checks response size.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_protocol.py' -v` and `test_check_cli.py`; expect unknown methods/parsers.
- [ ] **Step 3: Implement.** Route profile, task-selection and run methods through the existing service with schema-4 guard; include `checks.v1` capability. Add `--check-run` mutually exclusive with `--accept-unchecked` and preserve old syntax. `integration_cli.emit` must stop claiming Solace never ran checks when checked evidence exists. Keep JSON output to one envelope and human guidance precise.
- [ ] **Step 4: Confirm green.** Run new protocol/CLI tests plus `test_integration_protocol.py`, `test_integration_cli.py`, `test_task_protocol.py`, `test_task_cli.py`; expect all pass.
- [ ] **Step 5: Commit.** Commit `feat: expose configured checks through local API and CLI`.

### Task 9: Disposable acceptance, documentation and handoff

**Files:** Create `tests/solace_core/test_check_acceptance.py`, `docs/configured-checks.md`; modify `docs/result-integration.md`, `docs/engineering/2026-10-09-review-findings-and-follow-ups.md` and create a dated engineering acceptance/handoff record.

**Interfaces:** No new product interface; exercise the public CLI/socket and a disposable schema-3→4 project.

- [ ] **Step 1: Write failing acceptance tests.** `test_socket_checked_child_parent_and_source_flow` stages exact M, runs passing steps, approves/applies and reads receipts; `test_failed_timeout_and_unchanged_live_trees` checks refusal and source/workspace immutability; `test_artifact_from_check_never_applies` checks private-copy isolation; `test_restarted_core_preserves_interrupted_evidence` checks recovery over real socket.
- [ ] **Step 2: Confirm red.** Run `env SOLACE_TEST_TMPDIR=/tmp python -m unittest discover -s tests/solace_core -p 'test_check_acceptance.py' -v`; expect gaps in public workflow.
- [ ] **Step 3: Finish implementation and docs.** Close only gaps proven by these tests. Document setup, trust boundary, exact binding, failed/stale/unchecked statuses, cancellation, retained-output limits, backup/upgrade and rollback. Record actual worktree/HEAD, evidence paths and hashes, installed runtime distinction, blockers and next steps.
- [ ] **Step 4: Verify source and live gates.** Run all three suites (`tests/solace_core`, `tests/solace_engine`, `tests`) with `SOLACE_TEST_TMPDIR=/tmp`, real Unix-socket acceptance, `git diff --check`, doc links/CLI help checks, and an independent whole-branch review. If restricted sandbox blocks socket/systemd/display, rerun permitted gates before claiming success. Do not install or upgrade a real store without separate concrete authorization.
- [ ] **Step 5: Commit.** Commit `docs: complete configured checks source acceptance and handoff` only after evidence is captured.

## Execution handoff

Implement in this managed worktree after plan review. A fresh task reviewer should inspect each task's tests, failure handling and schema compatibility before the next task; a whole-branch reviewer checks cross-task contracts. Keep `main`, installed runtime and all existing project stores unchanged until their separate gates. The next subsystem is automatic integration policy, whose design must specify project-required minimum checks so a task cannot choose a weaker profile to bypass them.
