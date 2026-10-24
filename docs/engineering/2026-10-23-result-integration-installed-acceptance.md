# Manual result integration: installed acceptance

**Installed locally:** 2026-10-23 EDT (installer backup timestamp 2026-10-24 UTC). The user approved installation of the previewed runtime and disposable installed-candidate acceptance. No existing user project was migrated, no runtime rollback was executed, and no branch was merged or pushed.

## Installed runtime and preserved rollback

The clean source branch was `codex/task-integration-design` at `8ae2aea0c9d090b48dc5b5d45d3906f74e3e3933`. The fresh committed-tree preview proposed runtime `f877c3c2f0ee11a0`; `python packaging/install-core.py --apply` installed that same runtime. Its manifest lists 44 Python modules, and each installed module hash matched the manifest after installation. The previous runtime `59f3d8340f868d4a` remains intact.

The installer backup is `/home/lucievol/.local/share/solace/core-install-backups/20261024T005355Z-ec23d0f8`. Its manifest contains the prior service unit and launcher; the socket unit did not change. Both current files match their recorded installed digests, and both previous backup files exist and name the old runtime. The persistent rollback helper `/home/lucievol/.local/share/solace/core-rollback-tools/e77eb01bf4e57e51/rollback-core.py` matches SHA256 `e77eb01bf4e57e51fc43fc5db40457628808e4d31a63b7ec08d0717424f5bfd7`.

The socket was enabled before installation. It and the service were stopped, the installer applied, the user manager reloaded, and the socket reenabled. Final systemd status reports the socket **enabled and active** and the service **active**, PID `2229639`. The installed core reports `integrations.v1`, supports core schemas 1–3 and creates schema-3 stores. `solace doctor --json` is healthy. Final live status has zero registered projects and no pending recovery.

The backup is eligible for the pinned helper's digest-guarded rollback, as verified read-only. Rollback was **not executed**. The socket was enabled before this installation; these are the exact rollback commands if needed:

```bash
systemctl --user stop solace-core.socket solace-core.service
python /home/lucievol/.local/share/solace/core-rollback-tools/e77eb01bf4e57e51/rollback-core.py --rollback /home/lucievol/.local/share/solace/core-install-backups/20261024T005355Z-ec23d0f8
systemctl --user daemon-reload
systemctl --user enable --now solace-core.socket
solace core status --json
```

Runtime rollback preserves project databases and cannot downgrade schema-3 stores; the older runtime refuses them. Inspect the backup and current file digests again before executing these commands in a later session.

## Disposable installed-core acceptance

The installed launcher and actual user service completed 22 checked steps. The preserved fixture and JSON report are at `/home/lucievol/.local/share/solace/acceptance/result-integration-20261023-pfx8ooq9/acceptance-report.json`. The disposable project was `416a653bdfeb4d06bbc8557db19cad2d`; its store, source and workspaces remain available as evidence after unregister.

- A parent and child received separate mutable workspaces, excluding protected `.git` and `.solace_vault` entries.
- A saved child result applied only to its recorded parent. The parent's independent edit survived, and the parent's earlier ready result remained immutable when child application required a new ready capture.
- The parent result applied to the source while preserving the source root inode, an independent source edit, and exact Git/vault bytes.
- A changed binary file produced a conflict. Explicitly choosing the incoming version created a new prepared revision and applied those exact bytes.
- All three applied receipts reported `checks: not_run_by_solace`. The final receipt remained readable after unregister and after a real service restart.

The three receipt IDs are `4f51706676d146339da43265809653a4`, `60985763273f456bacc077e0cd686217`, and `bf934ae8c7f54cc4a6a9c194b9b0f008`. All acceptance tasks were cancelled and the disposable project was unregistered. The fixture is deliberately preserved because retained catalog paths must remain available to Solace's placement checks.

## Scope of the result

Source verification before installation passed integration 441/441, engine 52/52, complete core 570/570 and legacy 16/16 in a permitted runtime environment; integration and core suites overlap. Independent whole-branch review found no code issue and its stale-status documentation finding was corrected. See the [source review record](2026-10-16-result-integration-review-and-acceptance.md).

Installed acceptance exercised the new-store schema-3 workflow. Existing user projects were not upgraded. Any existing schema-1/2 store needs its own preview, review and explicit apply before integration commands can use it. Configured checks, automatic policy, forward undo and GUI integration remain deferred. This acceptance does not prove physical power-loss behavior or a full interactive GUI workflow.
