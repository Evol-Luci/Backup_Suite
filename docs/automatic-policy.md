# Checked integration queue

New projects default to manual integration. The checked queue prepares candidates and runs approved commands; it never approves or publishes files. Check commands run with your operating-system authority, without a sandbox. Actor labels are audit metadata, not authentication.

Use `solace core status --json` to confirm `policy.v1`. For an older project, run `solace project upgrade PROJECT --preview`, inspect the source and destination schema and backup plan, then run `solace project upgrade PROJECT --apply --plan-token TOKEN`. Each explicit upgrade advances one schema; repeat previews until schema 5. Keep the verified backup. Older runtimes cannot open schema 5; rollback requires restoring a backup, not downgrading in place. Opening a store does not upgrade it.

Create an approved check profile using `solace project check-profile preview PROJECT create --definition PROFILE.json`, then `solace project check-profile apply PROJECT create --definition PROFILE.json --plan-token TOKEN --confirmed`. Review its commands before confirming. Enable the queue:

```sh
solace project policy preview PROJECT --mode checked_queue --required-profile PROFILE --no-allow-task-opt-in
solace project policy apply PROJECT --mode checked_queue --required-profile PROFILE --no-allow-task-opt-in --plan-token TOKEN --confirmed
```

Copy the exact issued token and repeat the identical fields. Tokens expire and are bound to the current policy, profile and store. Inspect `project policy show PROJECT`. To disable new stages, preview and confirm `--mode manual --no-allow-task-opt-in`. Omitting `--required-profile` removes the required-check rule through this separately confirmed change. Keeping a required profile in manual mode still requires that profile for every new approval.

Select the exact profile with `solace task check-profile select PROJECT TASK PROFILE` before saving a ready result. A missing or different selection cannot run automatic checks. Task overrides use `task policy preview PROJECT TASK --mode manual --revision REVISION`, followed by `task policy apply` with the same fields plus `--plan-token TOKEN --confirmed`. Modes are `inherit`, `manual`, and `checked_queue`. Explicit checked opt-in requires a project policy confirmed with `--allow-task-opt-in` and a required profile. Overrides cannot weaken required checks. Ready tasks must be resumed before changing an override and saving a new result.

Inspect bounded queue pages with `solace integration queue list PROJECT --limit 128`, then `--after CURSOR`, or `solace integration queue show PROJECT INTENT`. JSON output uses the standard `--json` envelope. `pending_apply` means a candidate passed checks, and both source and parent destination remain untouched. Inspect its plan and run:

```sh
solace integration show PROJECT PLAN
solace integration diff PROJECT PLAN --revision REVISION
solace integration check show PROJECT RUN
solace integration approve PROJECT PLAN --revision REVISION --token REVIEW_TOKEN --check-run RUN
# Pause external editors and agents writing the destination before continuing.
solace integration apply PROJECT PLAN --revision REVISION --approval APPROVAL --writers-paused --wait
```

Ordinary `--accept-unchecked` is refused while a required profile exists. A task's manual override does not waive this rule. Changed policy/profile/target/candidate bindings invalidate old evidence or approvals; inspect the precise error and prepare/check a fresh candidate when needed.

Conflicts, failed checks, interrupted commands and stale bindings remain pending with a reason and retained evidence. A restart reconciles accepted operations without silently rerunning commands with possible external effects. Inspect the referenced operation and run before choosing a manual retry. Disabling policy leaves previous plans and evidence visible. No automatic stage supplies a writer-paused acknowledgement or applies a result.

Development acceptance uses disposable stores and sockets only. Installing a runtime, upgrading real stores and applying to real sources have separate operator gates.
