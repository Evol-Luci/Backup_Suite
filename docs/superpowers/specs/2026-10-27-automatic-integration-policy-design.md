# Automatic integration policy: checked queue

**Status:** Approved, 2026-10-27
**Parent direction:** [Local-first agent version engine](../../specs/local-first-agent-version-engine.md)
**Prerequisites:** Installed manual integration, configured checks, and checked receipts

## Intent

Solace should remove repetitive preparation and check-running work after a task saves an immutable ready result, while preserving a clear decision before replacing files in a parent workspace or source folder. The first policy slice is a **checked queue**: the core prepares the exact candidate and runs the project's required check profile. It does not approve or apply the result. Manual remains the default.

The approved architecture calls for eventual automatic apply. The existing apply contract requires a fresh `writers_paused` acknowledgement, while Solace does not launch or pause external agents and editors. This slice keeps apply pending until a person invokes the existing reviewed apply workflow. A later writer-handoff design must establish how the core can truthfully authorize automatic publication for either target.

## Policy and authority

- A schema-5 project has a single current project policy revision with a default mode (`manual` or `checked_queue`), an optional required check profile, and `allow_task_opt_in`. `manual`, no profile and no opt-in are the defaults for new and upgraded stores. A `checked_queue` default requires exactly one active, project-approved profile. A manual default may retain a required profile for manually run checks or task opt-in. The policy pins that profile's active revision and digest when confirmed.
- Project policy creation or change uses an owner-reviewed preview and a short-lived token bound to the exact policy, current profile revision/digest, project/store identity, and current policy revision. Applying it requires explicit confirmation. The actor string is an audit label, not a security credential. The preview explains that check commands run with the user's OS authority and are not sandboxed.
- A task inherits the project mode. A task can opt out to `manual`; this disables automatic preparation/checking for that task but does not waive required checks. Enabling `checked_queue` for one task while the project default is manual requires `allow_task_opt_in`, a pinned project-required profile, and the same owner-reviewed confirmation. Overrides are allowed only before the next ready result; changing a ready task requires resuming it and saving a new result. A task override cannot replace or weaken the project's required profile. An agent may select an existing approved profile for its task, but only a selection matching the required profile ID is eligible for automatic checks.
- Editing or retiring the required profile pauses new automatic work until the owner confirms a fresh policy revision. Existing run evidence remains retained; it never authorizes a different profile revision. Setting the project to `manual` with `allow_task_opt_in=false` disables all new automatic stages, including task overrides, and leaves already prepared plans and evidence visible.
- Policy changes do not rewrite task results, plans, check runs, approvals or receipts. They are evented and visible through CLI and JSON API.
- If a project has a required profile, every new integration approval, including a manually prepared result, requires a current passing run of that exact profile. Ordinary `--accept-unchecked` is refused with a clear next step. A person who deliberately wants unchecked integration must first remove the requirement through a separately reviewed project policy change; the old policy and existing evidence remain in history. A project with no required profile retains the existing explicit unchecked path.

## Workflow

1. `task.ready` publishes the immutable result, task revision and a durable policy intent in the same catalog transaction when the effective mode is `checked_queue`. The intent identifies the project, task, result, target, policy revision and required profile binding. It records stages and stable request IDs so retries cannot duplicate work.
2. After the ready operation commits, the core schedules the intent. It uses existing `integration.prepare` semantics with exact task and target revisions. Preparation never edits the destination. Existing ancestry, ownership, child-readiness and conflict rules remain authoritative.
3. If the plan is current and conflict-free, and the task's frozen selected check profile matches the policy-required profile ID and active revision/digest, the core starts one check run on that exact plan/revision. It uses the existing private candidate staging and bounded evidence. It does not infer one profile is stronger than another by name or command contents.
4. A passing run moves the intent to `pending_apply` with the plan ID, revision, check run ID and an actionable next command. The person still inspects the diff and run, calls checked `integration.approve`, pauses external writers, then calls `integration.apply --writers-paused`. The resulting receipt records the existing human approval and check evidence.
5. A conflict, missing/different/retired profile, failed/timed-out/cancelled/interrupted/stale check, changed task/target/policy, or unavailable store moves the intent to an explainable pending state. Solace never silently retries a command that may have had effects outside the candidate. The person can inspect the exact operation/run and choose whether to prepare and check again.

The policy does not call `integration.approve`, synthesize a review token, set `writers_paused`, or call `integration.apply`. A task without policy support can still use the existing manual workflow.

## Durability and recovery

- Each intent has one durable identity for an immutable result and target under a policy revision, with persisted stage, operation IDs, plan/revision, check run ID, terminal reason and timestamps. Repeated ready/request delivery cannot create a second intent for the same binding.
- Startup reconciles recorded operations and check runs using their durable states. An accepted operation is not assumed successful; an interrupted or ambiguous step remains visible and pending. Already applied results show their historical receipt and do not create a new application.
- The core rechecks effective policy, profile revision/digest, task/result/target revisions, plan binding and current check evidence before advancing a stage. A changed binding stops the intent rather than refreshing an approval or silently choosing a new profile.
- Checked approval and apply on schema-5 stores reauthenticate the current project policy revision and required profile. A policy change cannot silently turn an old approval into permission under new rules. Receipts record the policy revision and whether the required check rule was met; human review remains human review.
- Automatic work observes existing per-project coordinators and bounded check-run concurrency. The queue retains a reason when work is delayed or refused. It does not retain full check output in ordinary status/event summaries.

## Interfaces and migration

- Add a `policy.v1` capability and schema 5. New stores are schema 5; older stores continue their supported manual operations at schemas 1–4. Existing stores move through explicit `project upgrade --preview` and `--apply` with a verified consistent backup; opening a store never migrates it. Runtime rollback cannot downgrade schema 5.
- Project policy preview/show/apply and task policy preview/show/apply are exposed through the local JSON API and CLI. Status/list/show of intents are read-only, paged, and include the next human action. Names and exact flags are implementation-plan decisions; the operations above are the behavioral contract.
- Policy controls reuse the current same-user socket model and confirmation ceremony. They do not claim protection against a malicious process running as the same OS user.

## Acceptance

Disposable tests through the real CLI and Unix socket must show: default manual behavior; owner-confirmed project enable/disable and task override; no auto check with a weaker/different/missing profile; required-check enforcement on manual approval and apply with refusal of ordinary unchecked approval; a checked child-to-parent and root-to-source plan ending at `pending_apply` without touching either destination; checked run evidence bound to the exact candidate; conflict and check failure left pending; policy/profile/target staleness; repeated requests and core restart without duplicate command execution; explicit schema-4-to-5 preview/backup/upgrade; and unchanged manual workflows on older supported schemas.

Installed acceptance uses only a disposable project and verifies the user service's `policy.v1` capability, one pending checked result, no destination mutation before human apply, and an explicit checked apply after the operator pauses writers. Existing user stores are not upgraded as part of runtime installation.

## Later automatic apply

True automatic application needs a separate writer-handoff contract for external agents/editors and distinct policy-approval provenance in the immutable receipt. That design must cover lease expiry, process crash, changed target content, policy revocation, recovery-required journals, and both child-to-parent and root-to-source targets before adding an automatic publisher. A standing opt-in alone does not establish that writers are paused.
