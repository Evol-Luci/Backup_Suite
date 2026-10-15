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

## Installed acceptance, 2026-10-15

Installed the reviewed/remediated source at `b1b5091` into the fixed runtime `/home/lucievol/.local/share/solace/core-runtimes/152406111fa424cb`. Installer backup: `/home/lucievol/.local/share/solace/core-install-backups/20261015T111851Z-76a1c4a2`. The installer changed only the two core user units and `~/.local/bin/solace`; the pre-existing legacy app remains alongside this installation.

The real systemd user manager loaded and enabled `solace-core.socket`. Its first CLI request activated the static `solace-core.service`. Socket mode is 0600, its parent is 0700, and project store/catalog modes are 0700/0600. The manager and client agree on HOME and all XDG paths. `Linger=no`; this work did not alter lingering. No active desktop logout was performed, so actual logout/session-end behavior remains untested.

All **28 live checks passed** using a disposable source project. The source included hidden files, binary data, an executable, a symlink, and fixture Git/vault metadata. Restore reproduced content, modes, and links without changing the source. Event watching delivered ordered durable events and omitted checkpoint message contents. The service owned no Internet socket; the journal omitted the checkpoint-message marker.

A real checkpoint capture was running when its systemd-owned process was deliberately terminated with SIGKILL. The user manager restarted it automatically. The uncommitted operation became interrupted, the committed checkpoint stayed successful, no partial checkpoint appeared, and a new checkpoint then succeeded. The journal's single expected signal failure/restart is acceptance evidence, not an unexplained application failure.

The fixture project was unregistered at the end. The registry returned to its original zero projects, while its store was preserved at `/home/lucievol/.local/share/solace/projects/6d4b23da7086452e950d6d84a039e35e`. Receipt replay still returned its original operation ID after unregister. Final installed `solace doctor --json` was healthy. Source and restored fixture trees plus raw evidence are retained under `/home/lucievol/Projects/Backup_Suite/.worktrees/local-core-design/.superpowers/sdd/2026-10-13-local-core-service-registry/acceptance-20261015-b4aeec3f` for inspection; no real user project was captured or restored.

### Live check detail

- socket is same user 0600: passed.
- socket parent is 0700: passed.
- registration has no checkpoint: passed.
- registration does not modify source: passed.
- store and database are private: passed.
- installed checkpoint succeeds: passed.
- live watch events are ordered: passed.
- events exclude checkpoint message: passed.
- checkpoint retry is idempotent: passed.
- installed verification succeeds: passed.
- installed restore succeeds: passed.
- restored content modes and links match: passed.
- checkpoint restore does not modify source git or vault: passed.
- installed doctor is healthy: passed.
- capture is running before termination: passed.
- systemd restarts the core: passed.
- abandoned operation is interrupted: passed.
- committed checkpoint remains successful: passed.
- interrupted capture published no checkpoint: passed.
- new checkpoint succeeds after recovery: passed.
- post recovery doctor is healthy: passed.
- core opens no internet socket: passed.
- service and cli share data root: passed.
- service journal excludes checkpoint message: passed.
- unregister preserves store: passed.
- registry returns to initial project count: passed.
- unregistered checkpoint receipt still replays: passed.
- final global doctor is healthy: passed.

### Rollback

Disable/stop the newly installed socket and core, run the installer-generated backup-specific rollback command, then reload the user manager. Project stores and runtime snapshots are preserved. The exact command for this installation is:

```bash
systemctl --user disable --now solace-core.socket
systemctl --user stop solace-core.service
/home/lucievol/.local/share/mise/installs/python/latest/bin/python3 /home/lucievol/Projects/Backup_Suite/.worktrees/local-core-design/packaging/install-core.py --rollback /home/lucievol/.local/share/solace/core-install-backups/20261015T111851Z-76a1c4a2
systemctl --user daemon-reload
```

Live installation and acceptance are complete. Local branch integration follows this evidence commit. Remote publication and the next task/workspace feature milestone are separate work.
