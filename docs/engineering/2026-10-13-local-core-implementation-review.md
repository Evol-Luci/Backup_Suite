# Local core implementation review, 2026-10-13

Implemented on `codex/local-core-design`, starting from `3e1ddc5`. The eight approved plan tasks are complete in the development branch. No merge, push, release, real-home install, or live user-manager activation was performed.

## Result and evidence

- 49 new core tests passed.
- 49 existing checkpoint-engine tests passed.
- 16 existing legacy/Tk tests passed.
- Total: 114 passing tests; three separate unittest discovery commands.
- `git diff --check` passed.
- `systemd-analyze --user verify` accepted generated units, including an isolated installation under a path containing spaces and percent characters.
- An actual core subprocess consumed an inherited Unix listening descriptor, answered requests, opened no Internet listener, preserved the socket inode, and did not unlink it on exit.
- Installer tests used temporary HOME/XDG roots: preview made no home changes; repeated install reused its fixed runtime; altered runtime content was refused; partial publication failure restored original files.

## Review scope and conclusion

Author review followed the approved inline plan. No independent reviewer was dispatched; this review has the author's independence limitation. Inspected registry/catalog transaction boundaries, engine changes, per-project coordinator locks, socket credentials/framing/subscriptions, CLI dispatch, read-only diagnostics, packaging, and legacy compatibility. No known blocking correctness finding remains after the covered fixes. This is development-branch readiness, not live-deployment approval.

The review covered the plan's five risks: lost mounts cannot initialize stores; lost replies replay durable receipts; replay/live overlap uses durable cursors; relink/unregister share project coordination; corruption/newer schemas are blocked and diagnostics do not change offline catalog bytes. Additional regressions cover source-root replacement, a missing prior registry, configured sync roots, unregister replay after removal, committed results during worker cleanup, and partial installer failure.

## Outstanding acceptance work

Real activation by the user's systemd manager requires separate installation authorization. The inherited-FD test and unit parser checks do not substitute for testing that manager's activation, logout lifecycle, journal output, and permissions on the installed path. Full interactive GUI rendering remains unverified; existing Tk tests pass. Cross-file application-consistent capture, sudden-power-loss restore durability, universal background-sync detection, same-user hostile modification, task workflows, migration, retention/GC, and replication remain outside this milestone.

Use `packaging/install-core.sh --preview --preview-dir /tmp/solace-core-install-preview` to regenerate the concrete install preview. Installation changes two user units and `~/.local/bin/solace`; runtime snapshots live under the XDG data root. The existing installed legacy app is untouched. Installation itself does not activate services; activation commands and a backup-specific rollback command are printed separately.

## Implementation rulings

These record decisions from inline execution; each preserves the approved design boundary.

- Ruling: follow the approved plan's inline review rather than dispatch a reviewer; no delegation selected. Independence limitation will be reported.
- Ruling: consolidate foundation test modules into test_foundations.py to keep shared fixtures compact; same behavior coverage.
- Ruling: corruption fixture must close its SQLite handles before overwriting the main file; an open WAL masks corruption of the main file, demonstrated separately.
- Ruling: registration intents also persist store_uuid, mount_id, actor, created_at. Without these the proposed intent schema cannot validate identity or reproduce a committed response after a crash. Completed intents are deleted atomically with publication so unregister permits fresh registration.
- Ruling: persist approved params_json (destination/checkpoint/exclusions) for restore recovery evidence; messages omitted from events and operations. Hook reads use cursor-local Row factory because engine connections produce tuples.
- Ruling: add a registry-scoped flock so a development server cannot become a second writer authority. Restore retry must consult its receipt before checking an already-published destination; regression RED->GREEN.
- Ruling: durable one-second polling backs up notifications; subscription correctness depends on cursor. Core PROJECT_BUSY now also checks durable nonterminal operations; a worker's cleanup after success must not hide its committed result. Deterministic regression RED->GREEN.
- Ruling: mode=ro alone creates WAL sidecars offline; without WAL use immutable=1. Existing WAL requires existing SHM; otherwise refuse diagnostic open. Empty confirmation reaches CONFIRMATION_REQUIRED.
- Ruling: remove tests/solace_core/__init__.py: unittest -s tests imports it as solace_core, shadowing production. Match the existing engine test directory's separate discovery convention.
- Ruling: /tmp is tmpfs on this machine and correctly rejected for SQLite; activation subprocess uses a temporary checkout-local data directory and separate short /tmp runtime socket.
- Ruling: WorkingDirectory is a scalar path and must not contain surrounding ExecStart-style quotes; percent specifiers remain escaped. Executable path uses quoted ExecStart with : to suppress environment expansion.
- Ruling: add core-config.json sync_roots so planned configured exclusions are available to people, not only embedding callers. Restart required after configuration edits.
- Ruling: protect missing previously used registry using persistent core.lock marker; no empty registry may replace previously recorded state.
- Ruling: stable ID syntax permits unregister receipt replay through CLI after the live registration is gone.
- Ruling: source physical identity is rechecked before existing-store publication; API paths must be absolute because daemon working directory differs from clients.
- Ruling: installer rollback accepts both already-applied and untouched originals, enabling safe rollback after partial file publication. It still refuses unrelated changed files.
