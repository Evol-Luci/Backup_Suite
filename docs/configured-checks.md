# Configured checks for manual integration

Configured checks run approved project commands on a private copy of the exact prepared candidate. They do not approve or apply it automatically. On 2026-10-29 the installed core advertised `checks.v1` and schema 4; that status check does not establish disposable installed acceptance of checks. The [source handoff](engineering/configured-checks-handoff.md) records source acceptance. This checkout also adds [checked-queue policy](automatic-policy.md), which has its own source and installation gates.

## Upgrade and backup

Opening a project never upgrades its store. With a compatible core, run `solace project upgrade PROJECT --preview` and inspect its result. For a schema 3 store, apply the exact fresh token with `solace project upgrade PROJECT --apply --plan-token TOKEN`; the upgrade creates a verified consistent backup. Schemas 1 and 2 first upgrade to 3, then require another preview for 4. Review the backup location and retain it. An older runtime cannot use schema 4, and switching executables does not downgrade the store. Restoring a backup loses changes made since it was taken; preserve the current store and review recovery before restoring.

## Approve a profile and select it

A profile is JSON with `version: 1`, a `name`, an `environment` object, and ordered `steps`. Each step has a `name`, an `argv` array, `timeout_seconds` and `output_limit_bytes` per stream. For example:

```json
{"version":1,"name":"unit tests","environment":{},"steps":[{"name":"tests","argv":["python3","-m","unittest"],"timeout_seconds":120,"output_limit_bytes":65536}]}
```

Run `solace project check-profile preview PROJECT create --definition profile.json`, inspect commands and the authority warning, then apply with `solace project check-profile apply PROJECT create --definition profile.json --plan-token TOKEN --confirmed`. A fresh token binds the exact definition and current project state. Update and retire also require preview and confirmation. Commands are trusted same-user code: they can access the user's files and network; the private candidate copy is **not a security sandbox**. Arguments are passed directly, without implicit shell parsing. The environment is restricted; PATH and LANG have defaults, while HOME and TMPDIR are private. A profile may override PATH and LANG.

Select the approved ID before marking the result ready: `solace task check-profile select PROJECT TASK PROFILE`. Children inherit their parent's selection, and may select another approved profile. A ready task must be resumed and produce a new result to change its selection. Schema-5 [project policy](automatic-policy.md) can require an exact profile for every new integration approval; older stores retain the explicit unchecked path.

## Run, review and apply

Prepare and inspect a conflict-free plan as described in [result integration](result-integration.md). Run `solace integration check run PROJECT PLAN --revision N --profile PROFILE --wait`; inspect `solace integration check show PROJECT RUN` and `solace integration check list PROJECT PLAN`. A nonzero exit from a waiting command can indicate a completed failed or timed-out check; inspect its JSON result and run record. `solace integration check cancel PROJECT RUN` requests cancellation; disconnecting a waiting client does not cancel a core-owned run. Timeout or cancellation terminates the process group with TERM then KILL.

A pass binds the plan and revision, review token, candidate checkpoint and manifest, capture profile, expected target, task and revision, and selected check profile revision and digest. A changed binding makes evidence stale. Failed, timed-out, cancelled, interrupted and stale runs cannot authorize a checked approval. A passing run still needs human review: use `solace integration approve PROJECT PLAN --revision N --token REVIEW_TOKEN --check-run RUN`, then apply with its approval and `--writers-paused`. The checked receipt records the run and bounded step summaries. The alternative `--accept-unchecked` explicitly records `not_run_by_solace`; it is not a passing check.

All steps start from a verified copy of the candidate; later steps can see earlier steps' artifacts, but application uses the original candidate. Complete stdout and stderr are counted and hashed; only capped output is retained in private files in the project store. Output may contain secrets. Evidence and run metadata remain retained; automatic garbage collection is not included. Check `show` for cleanup or survivor warnings after interruption, and inspect unsafe retained paths rather than deleting them blindly.
