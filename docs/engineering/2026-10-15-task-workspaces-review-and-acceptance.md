# Task/workspace candidate review and acceptance
Candidate branch: `codex/task-workspace-design`; product baseline: `40c8d22`. The approved [design](../superpowers/specs/2026-10-15-task-lifecycle-workspaces-design.md) and [implementation plan](../superpowers/plans/2026-10-15-task-lifecycle-workspaces.md) govern this candidate. Execution is inline, followed by one fresh independent whole-branch review.
## Candidate verification before independent review

```bash
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 52 tests in 1.907s

OK
```

```bash
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 113 tests in 37.573s

OK
```

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

```text
----------------------------------------------------------------------
Ran 16 tests in 0.446s

OK
```

`git diff --check` passed. Baseline had 122 passing tests. Compatibility assertions were updated deliberately: schema 2 is supported and schema 3 refused; unregister now refuses unreadable catalogs because task ownership cannot be proved absent. No prior test was removed.

The two acceptance tests use a real same-user Unix socket and CLI in disposable directories. They prove two children can edit the same filename independently, source Git/vault/hidden/binary/executable/link data stays unchanged, immutable results restore exact contents, source history stays separate, resuming retains earlier results, parent status awaits integration, and another project proceeds while preparation is blocked.

Recovery tests use actual subprocess exits before/after staging, rename and catalog commit, plus interrupted ready capture; unavailable/relocated storage, mismatched identities, dirty finals, stale revisions and staging cleanup are exercised. Prompt tests cover exact text, logical retention, immediate expiry, replay after redaction, bounded maintenance and body-free events/receipts/watch output. Tk tests do not constitute full interactive GUI or actual logout testing.

## Independent review

Pending. This record does not approve installation, local integration or publication. The installed core and its existing project catalogs have not been modified by candidate testing.

## Implementation rulings

- Task 3: Ruling: /tmp has an existing .git directory on this machine — workspace fixtures use disposable /var/tmp directories rather than bypassing the approved Git-ancestor rule — cost if wrong: fixture portability needs a different non-Git temporary parent.
- Task 4: Ruling: cached bases intentionally live inside the store, whose paths the normal scanner excludes — add an explicit read-only inspect_tree(include_store=True) used only for complete materialized-tree verification — cost if wrong: cache integrity checks could omit data; capture defaults remain unchanged.
- Task 5: Ruling: SQLite publication must acquire the project lock before BEGIN, after scanning — add optional engine publication_guard rather than acquiring the lock inside a hook (which could deadlock with a concurrent lifecycle writer) — cost if wrong: publication coordination can stall; legacy default remains unchanged.
- Task 7: Ruling: the old regression allowed unregister with an unreadable store — change it to fail closed because task ownership cannot be proved absent, as the approved plan requires — cost if wrong: unregister requires storage return/relink even for a task-free unavailable project.
- Task 9: Ruling: auto-fetching a new revision on a retry changes its fingerprint — CLI mutations with an explicit --request-id require pinned --revision/--parent-revision flags; ordinary commands still infer current revisions — cost if wrong: power-user retries require additional flags, preserving exact request replay.
- Task 9: Ruling: keep task/workspace CLI parsing in focused task_cli.py rather than expanding the existing core CLI into one large module — cost if wrong: one additional internal module to navigate.

## Delivery gates

Before installation: finish independent review, fix verified important findings with regression tests, run final complete checks, and produce a fresh installer preview with the candidate runtime hash and current installed runtime. Installation and disposable installed acceptance require explicit approval. Runtime rollback preserves project stores and never downgrades schema 2. No remote push or local main integration has occurred.
