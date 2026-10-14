# Solace local core

The local core coordinates registrations and checkpoint operations for one Linux user. It uses a private Unix socket, a SQLite user registry, and separate identity-checked project stores. It opens no network port and does not launch agents. Legacy vault commands and scheduled backup jobs retain their existing behavior.

## Install preview

Run from this checkout:

```bash
./packaging/install-core.sh --preview --preview-dir /tmp/solace-core-install-preview
systemd-analyze --user verify /tmp/solace-core-install-preview/solace-core.socket /tmp/solace-core-install-preview/solace-core.service
```

Preview prints exact destinations, runtime contents, and activation commands; it does not modify your home directory. This milestone has been tested with inherited listening descriptors in subprocesses and an isolated temporary install. Installation and activation in the real user manager remain a separate authorization step.

After reviewing the preview and authorizing installation:

```bash
./packaging/install-core.sh --apply
systemctl --user daemon-reload
systemctl --user enable --now solace-core.socket
solace core status
```

The installer snapshots the standard-library-only core and engine under `$XDG_DATA_HOME/solace/core-runtimes/<content-hash>`, makes the runtime files read-only, installs `~/.local/bin/solace`, and renders units under `$XDG_CONFIG_HOME/systemd/user`. It preserves previous launchers/units in a timestamped backup and prints a rollback command. It never enables a service, starts an agent, enables lingering, migrates histories, or changes the installed legacy application. An unknown or altered runtime is preserved and refused. Repeat installation of the same build is idempotent. If updating an already running core, stop the service/socket before installing and reenable the socket afterward so the process uses the new runtime.

To roll back after activation, stop the service and disable the new socket, run the exact backup-specific rollback command printed by installation, then reload the user manager and restore your previous activation settings. The rollback refuses to overwrite files changed since installation. Runtime snapshots and project stores remain intact.

## Start an isolated development server

Set `XDG_RUNTIME_DIR` to a private directory owned by your user. Set `XDG_DATA_HOME` to a supported local filesystem; `/tmp` is often tmpfs and is deliberately unsupported for databases. Neither should point to production data for tests.

```bash
python -m solace_core --socket "$XDG_RUNTIME_DIR/solace/core.sock"
python -m solace_core.cli core status
```

The process refuses an existing socket path and a second core using the same registry. It removes only its own development socket on exit. Production uses systemd's listening descriptor and leaves socket cleanup to systemd.

## Commands

```bash
solace project add /absolute/source
solace project add /absolute/source --store /local/drive/new-store
solace project list --json
solace project show PROJECT
solace checkpoint create PROJECT 'Before editing' --exclude '*.tmp' --json
solace operation show PROJECT OPERATION --watch --json
solace checkpoint list PROJECT
solace checkpoint verify PROJECT CHECKPOINT --json
solace checkpoint restore PROJECT CHECKPOINT --to /new/empty-destination
solace checkpoint usage PROJECT
solace project show PROJECT --watch --after 0 --json
solace doctor PROJECT --json
solace project relink PROJECT --root /new/source --store /moved/store --confirm-root /new/source
solace project unregister PROJECT
```

`PROJECT` is an exact project ID or canonical registered source path; no fuzzy matching. `--json` and `--request-id UUID` can appear before or after subcommands. `--socket PATH` selects a development socket. The checkout's `solace-backup` script also routes the new command families to the core; the separately installed legacy launcher has not been updated by this work.

Registration creates an empty catalog without scanning files, adding source markers, or capturing `.git` or legacy vault history. Checkpoints subsequently capture source files according to the existing engine's explicit exclusions, including hidden files by default. Content is verified before checkpoint publication. Captures do not promise application-consistent state across multiple simultaneously edited files.

Long checkpoint creation, verification, and restore requests return an accepted operation ID. Inspect or watch that operation for completion; acceptance alone does not mean success. Mutations within a project run in FIFO order; independent projects share a bounded worker pool and can progress concurrently. Busy read and registration-change requests return `PROJECT_BUSY` with retry guidance.

Restore requires a new destination and refuses existing files, symlinks, managed stores, registry data, and registered source roots. It does not overwrite your current project. Unregister removes only the registration; it preserves the store. Relink confirms an exact canonical root and checks persistent store identity. It can point to an already moved store but does not copy or move data. Registering into any existing store destination is refused; automatic store adoption is not implemented.

## Recovery and storage

The registry lives at `$XDG_DATA_HOME/solace/registry.sqlite3` (default `~/.local/share/solace`). Default stores live under `solace/projects/<project-id>`. Each store contains `checkpoints.sqlite3` and content-addressed objects. Existing stores are never initialized by ordinary operations, and missing mounts never trigger an empty replacement. Store UUID, directory identity, and mount identity are checked. A changed mount requires explicit relink, even if the same drive was reattached.

SQLite placement permits writable ext2/ext3/ext4, btrfs, xfs, f2fs, and zfs. Network mounts, FUSE, tmpfs, overlay, and unknown filesystems are refused. Conventional Dropbox, Google Drive, and Nextcloud roots are refused. Add `{"sync_roots":["/absolute/custom-sync-root"]}` to `$XDG_DATA_HOME/solace/core-config.json` for other sync roots, then restart the core. Config paths must be absolute; unknown fields or malformed configuration are refused. Arbitrary background synchronization cannot be detected from filesystem metadata: choose an unsynchronized local store.

A checkpoint, its successful operation result, and its event commit together. Restart marks only queued/running core-owned operations interrupted. A committed checkpoint remains successful even if the reply was lost. An interrupted restore may have a published destination; Solace preserves it and never infers success or retries the restore automatically. Inspect the operation's saved destination before deciding what to do next. Failed registration intents are visible in diagnostics; matching initialized stores can be reconciled at startup, while missing stores are never recreated. Orphaned stores from failed registration are preserved.

Retry a mutation with the same request ID and arguments to recover its original response. Different arguments with that ID are refused. Operation request receipts are scoped to a project catalog; registry mutation receipts are scoped to the user registry. Keep your own request ID if you need recovery after a disconnect. CLI-generated IDs are included in JSON responses. Events and request receipts are retained without pruning in version 1. Checkpoint messages are stored locally; events and service logs omit messages, prompts, and raw request bodies.

`doctor` uses read-only access and does not initialize schemas or repair databases. Offline databases without WAL use immutable read-only inspection to avoid creating SQLite sidecars. If WAL exists but SHM is missing, diagnostics refuse to create it and ask for a clean stopped inspection. Offline results include core connectivity failure as well as registry/store evidence.

## Events, errors, and exit codes

Subscriptions replay durable project events strictly after the supplied sequence, then deliver live updates in order. Save the last delivered cursor and pass it to `--after` on reconnect. Notifications are hints; a one-second durable poll prevents lost notifications from losing events. Heartbeats do not create history records. Future/negative/noninteger cursors are rejected. Unregister ends the stream. Slow/disconnected subscribers can reconnect from their cursor; writes have bounded timeouts.

Errors expose a stable `code`, plain `message`, `next_step`, and `retryable`. Common codes include `CORE_UNAVAILABLE`, `PROJECT_BUSY`, `STORE_UNAVAILABLE`, `STORE_IDENTITY_MISMATCH`, `REQUEST_ID_REUSED`, `OPERATION_INTERRUPTED`, `CORRUPT_OBJECT`, and `SCHEMA_VERSION_UNSUPPORTED`. Writes never automatically repair corruption or migrate newer schemas.

Exit codes: 0 = successful request or accepted operation; 2 = invalid arguments/confirmation/cursor; 3 = unavailable core/project/store or unhealthy doctor report; 4 = integrity/schema failure; 1 = another failure. Operation watch returns 1 for failed or interrupted work.

## Verification

Run the three suites separately; top-level discovery does not collect the engine or core directories:

```bash
python -m unittest discover -s tests/solace_core -p 'test_*.py' -v
python -m unittest discover -s tests/solace_engine -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

Tk tests require desktop-display access. Activation tests keep the runtime socket in a private temporary directory and place their temporary database on the checkout's local filesystem. Tests do not change installed user units. Full interactive GUI rendering, live user-manager activation, and sudden-power-loss behavior remain separate validation items. Processes running maliciously as the same user are outside the threat model.
