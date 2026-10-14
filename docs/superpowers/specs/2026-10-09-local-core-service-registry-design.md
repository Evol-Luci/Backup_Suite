# Local Core Service and Project Registry

**Status:** Approved; implemented in `codex/local-core-design` on 2026-10-13. Live user-manager installation remains pending.
**Platform:** Linux first; systemd user services and Unix domain sockets
**Product boundary:** Solace coordinates projects and local history. Codex, Claude, and other tools execute prompts; the core does not launch agents in this milestone.

## 1. Purpose

Create one local authority that can coordinate project metadata and checkpoint operations for the Solace CLI, GUI, and future direct agent integrations. It must support multiple clients operating at once, provide recoverable task-state events later, and give people and agents clear errors with next steps.

The core is local to one user account. It does not expose a network listener or depend on hosted infrastructure.

## 2. Current repository context

- `solace_engine.ProjectRepository` provides the new checkpoint engine, but it has no production callers yet.
- The current CLI and GUIs still use `ProjectVault` directly.
- `backup_server.py` is a separate loopback HTTP API for legacy scheduled backup jobs. Its port-based API and `/tmp` PID-file lifecycle are not the foundation for the new core.

The core is introduced alongside the current app. Existing vault and backup-job data remain usable while new project/checkpoint commands adopt the core.

## 3. Approved architecture

### Process and local API

Run one Solace core per user, managed by a systemd user service and socket unit. The socket is available for the user's session; systemd starts the core on the first client request. The core then remains available until logout. The first release does not enable lingering to keep the core running after logout.

Clients connect over a permission-restricted Unix domain socket beneath `$XDG_RUNTIME_DIR`. The core verifies the connecting process's user ID. It does not accept remote TCP connections. The existing legacy HTTP server remains separate.

Use a versioned JSON request/response API. The CLI is the main human and agent surface; the GUI and future direct integrations use the same API. Responses include stable machine-readable error codes, a plain-language message, and a safe next step. Human CLI output explains the issue; `--json` exposes the same structured error for scripts.

### User registry and project stores

The user registry is a small SQLite database under `$XDG_DATA_HOME/solace` (defaulting to `~/.local/share/solace` when XDG variables are unset). It maps a stable project ID to the canonical project root and configured store path. The registry contains registration and location information; checkpoint, task, and event records belong to the project's own database.

By default, a project store lives under `$XDG_DATA_HOME/solace/projects/<project-id>`. Users may choose another local store path for each project. The store holds that project's SQLite database and content-addressed objects. Store paths must be on a local filesystem; SQLite databases are not placed on network or synchronized filesystems. A missing or unavailable configured store makes the project unavailable. The core must never silently create a replacement empty store at another path.

The core validates store accessibility and placement during registration and again before operations that depend on the store. The runtime socket is in `$XDG_RUNTIME_DIR`; service output goes to the system journal and excludes prompt contents.

### Project registration lifecycle

- `project add PATH` explicitly registers an existing directory, assigns a stable ID, and creates the project catalog at the chosen store path. Registration does not scan the source tree or make an initial checkpoint.
- The first checkpoint or task-start boundary creates the first content baseline.
- Registering the same canonical project root twice is rejected with a clear explanation.
- If a project root or store moves or becomes unavailable, Solace marks the registration unavailable. An explicit relink names the registered project and supplies the new path. The core validates the store identity and path, then asks the user to confirm the new source root; it does not claim to prove source identity by scanning files or adding a marker to the project.
- Unregistering removes the registry entry only. It preserves the project store. Removing stored data is a separate, explicit operation outside this milestone.

The core does not add an identity file to the project source tree during registration.

### State changes, events, and concurrent clients

Each project database has an append-only event sequence for state changes. A state change and its event are committed together; the core publishes the event only after the transaction succeeds. Events include project ID, sequence, task or operation ID, actor/client label, timestamp, state transition, and limited structured details. Events do not contain prompt text; prompt contents remain in retention-controlled task records when that later subsystem is added.

The socket API supports subscriptions that resume from a per-project sequence cursor. If a client disconnects, it requests events after its last cursor and receives missed updates before live updates. Terminal `--watch` and the GUI consume the same event stream. High-volume file activity is coalesced into current workspace status rather than stored as a durable event for every filesystem notification.

All state-changing client requests go through the core. It serializes conflicting writes within each project, while unrelated projects have independent operation coordination and may make progress concurrently, subject to shared CPU, memory, and storage limits. Database write transactions remain short; scans and other filesystem work happen outside a transaction and publish their result only after validation. Long Solace-owned operations return durable operation IDs so a client can inspect progress after its initial request ends.

On restart, recorded tasks remain in their last durable state. A Solace-owned operation that was in progress is reported as interrupted and made available for safe recovery; the core does not claim success without a committed result. A service restart alone does not mark an external Codex or Claude task failed.

### Health and failure behavior

Provide `solace core status` and a read-only `solace doctor [PROJECT]`, each with human-readable and JSON output. Diagnostics cover service/socket health, registered project paths, configured store reachability and permissions, database integrity, and pending or interrupted operations.

Missing stores, unsupported storage placement, corrupt databases, and incompatible protocol/schema versions produce stable errors with actionable guidance. The core blocks writes to an affected project rather than replacing, deleting, or automatically repairing its data. Repair, relocation, and deletion require explicit operations with their own reviewable results.

## 4. First implementation milestone

Include:

1. systemd socket activation, the per-user core process, and Unix-socket API;
2. user project registry, per-project store selection, add/list/show/relink/unregister operations, and filesystem checks;
3. checkpoint operations routed through the core to `solace_engine` and exposed through the CLI;
4. durable event subscription/replay and durable IDs for long core operations;
5. `core status`, `doctor`, stable error codes, and JSON output.

This milestone establishes infrastructure. It does not implement parent/child task workflows, prompt retention, review gates, the GUI task dashboard, Git history import/export, or remote replication.

## 5. Rollout and compatibility

Add the new core alongside current behavior. New project and checkpoint commands use the core. Existing `ProjectVault` GUI flows and scheduled backup jobs remain unchanged until separate migration work is approved. The core does not dual-write to legacy vault data, and project registration does not automatically import `.solace_vault` or Git history.

After the core milestone, prioritize parent/child tasks, isolated workspaces, terminal review actions, and then GUI task status. Git import/export follows as a non-destructive compatibility stage. Existing `.git` data remains untouched while Solace history begins from the current project baseline. Local-folder replication and Google Drive remain later work after the local engine is reliable.

This subsystem design updates the delivery ordering in `docs/specs/local-first-agent-version-engine.md`: task workflows now precede Git import/export. The broader roadmap remains in effect for later replication and storage work.

## 6. Acceptance criteria

- The core is activated through the user's systemd manager and is reachable only through the same-user Unix socket; it opens no network port.
- Multiple clients can issue requests concurrently without lost project state or out-of-order per-project event sequence numbers. Separate projects use independent operation coordination and can make progress concurrently, subject to shared machine resources.
- A registered project cannot be rebound to an empty store by path disappearance or mount loss. Relink is explicit, checks the registered store identity and new path, and requires confirmation of the selected source root.
- Adding a project does not scan or checkpoint source files. Unregistering preserves store data.
- A state transition and event are either both durable or neither is. A disconnected subscriber can resume from its cursor and receive all retained events in sequence.
- Interrupted Solace-owned operations are visible and distinguishable from successful operations. External agent tasks remain durable across core restarts.
- Human and JSON clients receive the same stable error code and a useful recovery action. Prompt text is absent from service logs and event records.
- Tests cover service activation and socket permissions, API version/error behavior, project registration/relink/unregister, local-store validation and unavailable stores, concurrent clients, event replay, and interruption around durable commits.
- The new commands do not modify legacy vault records or `.git` data.

## 7. Decisions left to the implementation plan

The implementation plan will specify the socket message framing, API method names and command grammar, registry and event schemas, migration numbering, request idempotency behavior, store-relocation mechanics, filesystem detection strategy, operation recovery table, event-retention/cursor behavior, and packaging/install steps. These details must preserve the approved boundaries above.

Cross-file application-consistent snapshots, durability on filesystems that do not honor `fsync`, and protection from hostile processes running as the same user are not promised by this design.
