# Solace Backup

Solace combines the existing backup application and Project Vault with a Linux-first local core for recoverable project history and isolated agent work.

The core and task/workspace subsystem are installed and live-verified as of 2026-10-16. Codex, Claude and other tools execute requests in their own workspaces; Solace records exact baselines, prompts and immutable results. Manual result integration is implemented and source-tested in this checkout. Its required source runtime gates and independent whole-branch review passed on 2026-10-23. Installation remains a separate milestone.

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
| Manual integration candidate: review, approval, apply and recovery | [Operator guide](docs/result-integration.md), [candidate evidence and install boundary](docs/engineering/2026-10-16-result-integration-review-and-acceptance.md), [continuation handoff](docs/engineering/result-integration-handoff.md) |
| Deferred capabilities and prerequisites | [Follow-up register](docs/engineering/2026-10-09-review-findings-and-follow-ups.md) |
| Existing backup GUI, jobs and ZIP-based vault | [Legacy quick reference](USER_QUICK_REF.md) |

The legacy `solace-backup` application remains separate from the installed `solace` core CLI. Existing ZIP vault histories are not automatically imported into the core. The installed runtime creates schema-2 stores. The integration candidate creates schema-3 stores and reads schemas 1–3; existing stores remain at their version until an explicit upgrade preview/apply. Schema 2 supports tasks; integration requires schema 3. Old runtimes refuse schema 3, and runtime rollback cannot downgrade databases.

## Verify this checkout

Run all three suites separately:

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

The 2026-10-16 installed candidate passed 192 tests. On 2026-10-23 the integration candidate passed integration 441/441, engine 52/52, complete core 570/570 and legacy 16/16 in a permitted runtime environment. Integration and core scopes overlap. All 168 real process-crash cases passed in both integration/core gates. See the candidate evidence above. Full GUI interaction, logout behavior and physical power-loss behavior have separate verification limits documented in the acceptance report.
