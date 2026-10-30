# Solace Backup

Solace combines the existing backup application and Project Vault with a Linux-first local core for recoverable project history and isolated agent work.

The local core, task workspaces and manual result integration are installed and live-verified. Codex, Claude and other tools execute requests in separate workspaces; Solace records exact baselines, immutable results and reviewed applications to a parent or source project. On 2026-10-29 the installed core advertised `checks.v1` and schema 4; the checked-queue policy in this checkout remains source work until a separate installation and disposable installed acceptance.

## Check the installed core

```bash
solace core status --json
solace doctor --json
```

The core uses a private per-user Unix socket. It opens no network listener and does not launch agents. The installed runtime is a fixed snapshot, so changing this checkout does not change the running service.

## Documentation

| Topic | Guide |
| --- | --- |
| Registration, checkpoints, recovery, installation | [Local core](docs/local-core.md) |
| Parent/child tasks, workspaces, prompts, ready results | [Task and workspace workflow](docs/task-workspaces.md) |
| Current verification, installed acceptance and rollback | [Acceptance report](docs/engineering/2026-10-15-task-workspaces-review-and-acceptance.md), [main closeout](docs/engineering/2026-10-16-task-workspaces-main-closeout.md) |
| Product architecture and delivery order | [Local-first version engine](docs/specs/local-first-agent-version-engine.md) |
| Manual integration: review, approval, apply and recovery | [Operator guide](docs/result-integration.md), [installed acceptance and rollback](docs/engineering/2026-10-23-result-integration-installed-acceptance.md), [source review](docs/engineering/2026-10-16-result-integration-review-and-acceptance.md) |
| Approved checks and checked-queue policy | [Configured checks](docs/configured-checks.md), [checked queue](docs/automatic-policy.md), [policy source handoff](docs/engineering/automatic-policy-handoff.md) |
| Deferred capabilities and prerequisites | [Follow-up register](docs/engineering/2026-10-09-review-findings-and-follow-ups.md) |
| Existing backup GUI, jobs and ZIP-based vault | [Legacy quick reference](USER_QUICK_REF.md) |

The legacy `solace-backup` application remains separate from the installed `solace` core CLI. Existing ZIP vault histories are not automatically imported into the core. The installed runtime creates schema-4 stores and reads schemas 1–4; this checkout creates schema 5 and reads schemas 1–5. Existing stores remain at their version until an explicit upgrade preview/apply. Schema 2 supports tasks; integration requires schema 3, checks require schema 4, and policy requires schema 5. Runtime rollback cannot downgrade databases.

## Verify this checkout

Run all three suites separately:

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

The [policy source handoff](docs/engineering/automatic-policy-handoff.md) records the current source test counts, review, installer preview and remaining installation gate. Earlier integration acceptance passed 22 disposable checks through the installed launcher and user service. Full GUI interaction, logout behavior and physical power-loss behavior have separate verification limits in the acceptance reports.
