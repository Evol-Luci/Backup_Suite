# Solace Backup

Solace combines the existing backup application and Project Vault with a Linux-first local core for recoverable project history and isolated agent work.

The core and task/workspace subsystem are installed and live-verified as of 2026-10-16. Codex, Claude and other tools execute requests in their own workspaces; Solace records exact baselines, prompts and immutable results. Combining results with a parent or original project is the next milestone.

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
| Deferred capabilities and prerequisites | [Follow-up register](docs/engineering/2026-10-09-review-findings-and-follow-ups.md) |
| Existing backup GUI, jobs and ZIP-based vault | [Legacy quick reference](USER_QUICK_REF.md) |

The legacy `solace-backup` application remains separate from the installed `solace` core CLI. Existing ZIP vault histories are not automatically imported into the core. New core stores use schema 2; existing schema-1 stores need an explicit upgrade preview and apply before task commands work.

## Verify this checkout

Run all three suites separately:

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

The 2026-10-16 installed candidate passed 192 tests. Tk tests need a working desktop display. Full GUI interaction, logout behavior and physical power-loss behavior have separate verification limits documented in the acceptance report.
