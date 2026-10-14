# Local core independent review and live acceptance, 2026-10-14

The user authorized independent review, installation/live acceptance, and integration of the verified branch. This record distinguishes the pinned review from remediation and installed evidence.

## Independent review

A separate read-only reviewer inspected `3e1ddc5..2f565fb`. No Critical finding was confirmed. Five Important findings blocked installation and merge:

1. Catalog lookup before project coordination could race relink/unregister; a checkpoint could capture the previous root after relink succeeded.
2. Offline registry diagnostics used ordinary SQLite read-only opening and created WAL/SHM sidecars.
3. Checkpoint receipt lookup followed source validation; a source move or unregister could prevent replay of the original accepted operation ID.
4. Stores unavailable at startup were never reconciled on return, leaving abandoned operations running indefinitely and allowing them past lifecycle guards.
5. Runtime-path discovery could fail before doctor entered its offline registry/store diagnostic fallback.

The reviewer declined live systemd activation, XDG manager agreement, logout lifecycle, and interactive GUI rendering because these require runtime acceptance. Explicitly deferred product features and threat-model limits remain outside the milestone.

## Verified remediation

All six initial reproductions failed on the reviewed implementation, then passed with bounded fixes. Two further regression tests preserve relink/unregister of unavailable stores while recovery is deferred.

- Resolve current registration/catalog while holding the project's coordinator lock.
- Use protected no-sidecar connections for read-only registry views, including active doctor's diagnostic view.
- Consult retained receipts before requiring source availability. Existing registry receipts provide preserved project/store identity for replay and outcome inspection after unregister; no live registration or new operation is created.
- Keep a per-project deferred-reconciliation flag. Recover abandoned core operations when their original store returns or validated relink makes it available. Check durable nonterminal state independently of the worker queue. Explicit unavailable-store lifecycle changes remain possible when no in-process worker owns the project.
- Feed runtime discovery failures into doctor's read-only fallback.

Commands: `python -m unittest discover -s tests/solace_core -p 'test_*.py'`, the equivalent engine suite, and top-level legacy/Tk suite. Fresh result: **57 core + 49 engine + 16 legacy/Tk = 122 passing tests**. `git diff --check` passed. No independent re-review was used; the independent findings were verified by regression tests and full-suite checks.

## Live acceptance

Installation and user-manager checks follow remediation. Results will be appended after actual execution. No logout will be triggered during an active desktop session; unit/session lifecycle wiring will be inspected and actual logout behavior remains an explicit limitation.
