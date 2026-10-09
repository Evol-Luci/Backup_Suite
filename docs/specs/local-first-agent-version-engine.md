# Local-First Agent Version Engine

**Status:** Approved architecture direction  
**Platform:** Linux first; portable capabilities where practical  
**Product role:** Solace coordinates local project history, workspaces, tasks, checks, and review. Codex, Claude, and other tools execute the prompts.

## 1. Purpose

Solace will grow from a basic local backup and snapshot application into a dependable local-first system for managing parallel developer and agent work. The long-term product goal is to replace Git for users who prefer Solace, while remaining useful as a personal tool even if it never becomes a broad standard.

The first engine should make local work reliable and understandable before Solace adds hosted collaboration or cloud storage. The interface should use clear everyday language for common actions, preserve expert control through precise options and stable machine-readable output, and explain errors with a safe next step.

## 2. Product boundaries

### In scope for the initial engine

- A per-user local core shared by the GUI, CLI, and agent integrations.
- Projects, parent tasks, child tasks, isolated workspaces, immutable checkpoints, checks, review decisions, and integrations.
- Local restore, conflict resolution, Git import and export, and local-folder replication.
- A live GUI view of task state and an equivalent terminal workflow.
- Project-level policy with task-level overrides for automatic integration and prompt retention.

### Deferred

- Solace launching or supervising Codex, Claude, or other agents.
- Remote multi-machine task coordination or a network-facing service.
- Google Drive uploads until the local engine is reliable.
- Live bidirectional synchronization between Git and Solace histories.
- Claims that Solace is universally faster, smaller, simpler, or unique before measurements support them.

## 3. Core architecture

### Local core

Run one per-user Solace core as the authority for project metadata and state transitions. The GUI, CLI, and agent clients use an OS-protected local socket. The initial release does not listen on the network. All clients see and update the same durable task, checkpoint, and review records.

Give each project its own SQLite metadata/event database and content-addressed object namespace. Checkpoints and workspaces within one project share objects; different projects do not share objects in the first engine. Use write-ahead logging for concurrent readers and a coordinated writer. SQLite remains on local storage; it is not a replicated database and is not placed on a network filesystem. The local core may place the per-project store outside the working tree so project history does not pollute source files.

### Client surfaces

- **CLI:** canonical action surface for people and agents; commands have simple defaults, explicit advanced options, and stable JSON output.
- **Local API:** provider-neutral interface used by the CLI, GUI, and future direct integrations.
- **GUI:** live task, workspace, checkpoint, check, review, integration, and storage status; it acts on the same core state as the CLI.
- **Agent execution:** remains external. Solace records task lifecycle updates and observes workspace activity; it does not launch agents in the initial release.

## 4. Project, task, and workspace model

A user request creates a parent task and a parent workspace. Each child agent task receives its own isolated workspace from an exact parent checkpoint. Child results integrate into the parent workspace. The parent then runs its configured checks and is reviewed or integrated into project main according to policy.

Agents receive a task identifier and workspace location. They can report lifecycle states such as started, blocked, ready, and completed through the CLI or local API. Solace also watches workspace changes and records observable file activity. Parent task state rolls up the status of its children.

Workspaces use copy-on-write reflinks when the filesystem supports them and ordinary copies otherwise. Mutable workspaces do not use hardlinks. Solace reports logical, physical, and reclaimable storage so users can understand the cost of parallel work.

## 5. Checkpoint and object storage

Checkpoints are immutable, content-addressed, and verified before they become visible. Store small files as whole objects and use content-defined chunking for larger files, allowing unchanged data to be reused across checkpoints and workspaces.

By default, checkpoints capture every project file except files matched by visible exclusion rules. Solace includes hidden files. It flags likely secrets and unusually large files for review rather than silently omitting them. Users can edit project exclusion rules.

Create automatic checkpoints at these lifecycle boundaries:

1. Task start.
2. Agent marks work ready.
3. Immediately before integration.
4. Immediately after successful integration.

Users can also create manual checkpoints. Live file changes may appear in the GUI without creating a permanent checkpoint for every save.

Garbage collection may reclaim an object only when it is not referenced by any checkpoint, active workspace, pending review, or queued replication. Retention and safety grace rules must prevent cleanup from racing with active work.

## 6. Integration and recovery

Solace uses three-way text merging for task integration. Overlapping text edits require explicit resolution. Binary conflicts preserve all versions and provide preview and selection; projects may opt into advisory locks for files that should not be edited concurrently.

Manual approval is the default integration policy. A project can opt into automatic integration, and a task can override the project setting. Automatic integration requires a configured local check profile to pass. Missing profiles, failed checks, and unresolved conflicts leave the result pending review. Agents cannot silently change user policy.

History is immutable by default. Undoing an integrated task creates a new forward change that reverses it. Restoring an older checkpoint can open a separate workspace or preview and copy files into the current workspace. Solace shows the affected files before any replacement.

## 7. Prompt retention and change receipts

Store the exact prompt in the local task record. Each project defines a retention policy that tasks inherit; an individual task can override it. Users can retain a prompt, set a retention period, or delete it immediately.

Deleting a prompt removes the prompt content while preserving a redacted task record and associated code history. Retention behavior must account for local replication and any future remote targets, including deletion or expiration of replicated prompt content according to policy.

Each integrated task receives a readable change receipt linking:

- Parent and child task identifiers and agent/client identity.
- The starting checkpoint and resulting checkpoint.
- Changed files and the reviewable diff.
- Check results and the review or automatic-integration decision.
- Prompt content only while it remains within its retention policy.

Receipts record observable actions and outcomes. They do not attempt to store private agent reasoning.

## 8. Replication and storage adapters

Define a storage-adapter interface before adding remote storage. The initial replication target is a local folder or removable drive. Local checkpointing and task operations continue offline; a durable queue retries replication when the target is unavailable.

The local object store remains authoritative. Verify replicated objects before publishing their manifests at the destination. Encrypt data before it leaves the machine for any remote destination. Add Google Drive only after the local engine is reliable; Drive stores replicated objects and manifests, not the live SQLite database or active workspaces.

## 9. Git migration and compatibility

Solace supports importing existing Git projects and their reachable history. Import is non-destructive: retain the original `.git` data until the user verifies the Solace project and its history. Preserve commit relationships and metadata to the extent the importer can represent them, and report unsupported Git features clearly rather than silently discarding them.

Solace can export selected checkpoints to Git format as a compatibility bridge. The first version does not maintain live bidirectional synchronization between independently changing Solace and Git histories.

## 10. Competitive position and product claims

Git and several alternative systems already provide isolated workspaces, branching or equivalent history workflows, binary-file strategies, review, and local operation. Solace should learn from those systems and should not describe those individual capabilities as unique.

The candidate differentiation is a provider-neutral local task record that ties prompts, parent-child work, workspaces, checkpoint ancestry, checks, review policy, and outcomes together across CLI, GUI, and agent clients. This is a promising product direction, not a claim that no competitor or extension offers the same combination.

## 11. Delivery order and evidence

The implementation should be staged so that each stage leaves a usable, recoverable result:

1. Establish the object store, checkpoint publication, verification, restore, and conservative garbage collection.
2. Add Git import/export and validate non-destructive adoption on real projects.
3. Add task records, parent-child workspaces, lifecycle updates, and CLI/API access.
4. Add integration, configured checks, review policy, change receipts, and GUI status.
5. Add local-folder replication and durable retry behavior.
6. Add Google Drive only after local checkpoint, restore, and replication behavior is reliable.

Compare Solace against Git worktrees plus native Codex/Claude workflows and relevant alternative systems using real project workloads. Measure checkpoint and restore integrity, disk growth with binary-heavy histories, crash recovery, conflict-resolution effort, and time needed to understand task state. Make performance and usability claims only after those comparisons.

## 12. Decisions intentionally left to implementation design

This architecture does not yet select chunk sizes or object thresholds, exact retention presets, the complete CLI command grammar, check-profile syntax, the detailed Git feature compatibility matrix, the merge-resolution UI, or the key-management design for encrypted remote copies. Those choices should be specified in the subsystem designs and implementation plans, without changing the approved product boundaries above.
