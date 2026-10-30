# GUI review and status design

Status: design for human visual review and later implementation. Base: `bbf6c1a2541c8eeb32330d42c1f136f9280ac74b` (2026-10-30). This document changes no runtime or project store.

## Scope hypothesis, falsifier, and observed result

Hypothesis: a person needs a visible way to find a checked result, inspect its exact candidate and check evidence, then explicitly approve and apply it. **This scope is wrong if the current GUI already lets a user find an integration queue intent, distinguish `pending_apply` from pending/stale states, inspect the exact plan diff and check run, and separately approve and apply the same revision with a writer-paused acknowledgement.** That observation was chosen before source inspection.

The current GTK4 `BackupMainWindow` builds only Backups, Project Vault, and Settings views; the navigation has those same three entries. The GUI files have no references to `CoreClient`, `integration.queue`, or policy methods. The older Project Vault view uses `ProjectVault`, not the installed local core (`omarchy_backup_suite/ui/main_window.py:677-772,1083-1097`; source search over `backup_suite.pyw`, `backup_suite_gtk.py`, and `omarchy_backup_suite`). The scope remains a new core-backed review view. The `solace` core installer packages only `solace_core` and `solace_engine`; delivering GTK4 UI requires a separate packaging and installed-UI gate (`packaging/install-core.py:45-70`).

Reject a later implementation if it treats `pending_apply` as permission to publish, implies external writers were paused, silently reruns a failed command, or shows an active approval control when a fresh queue read reports staleness.

## Intent and authority boundary

Add an **Integrations** view to the existing GTK4 desktop app, backed by the same-user `CoreClient` Unix-socket protocol. It is for a solo operator or agent-assisted operator who may not know plan IDs or CLI syntax. It shows registered core projects and their checked queue, gives exact review evidence, and requires two distinct human actions: **Approve checked result** and **Apply to destination**. Expert identifiers, error codes, page cursors, and copyable CLI equivalents remain in a Details drawer.

The core remains the sole authority for policy, plan validity, check evidence, approval, publication, and recovery. The GUI never opens project SQLite, edits a destination directly, marks a check passed, or pretends to authenticate an actor by setting `actor='gui'`. The Unix-socket server checks same-user peer credentials; actor labels are audit metadata, not roles (`solace_core/server.py:12-13,83`; `docs/automatic-policy.md:1`).

The first view includes checked-queue review, not policy authoring, check-profile editing, new check execution, conflict resolution, forward undo, or automatic apply. Manual integration plans that never entered the checked queue need a later discovery surface; label this section **Checked queue** rather than implying it lists every result. The CLI remains available for those operations. Browsing never enables checked-queue policy (`docs/automatic-policy.md:1-18`).

## Screen and novice journey

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Integrations       Project: [source name · /full/path ▾]   [Refresh]      │
│ Core connected · Checked queue · Policy: checked_queue                   │
├───────────────────────┬─────────────────────────────────────────────────────┤
│ Results to review     │ Result: task title · source or parent destination │
│                       │ Needs your review · Check passed                   │
│ [Needs review] task A │ Destination: /full/path                            │
│ [Checking]     task B │                                                     │
│ [Needs work]   task C │ Changes [All 12 loaded]  Check [Passed, 1 step]    │
│ [Applied]      task D │  path             decision   B / C / R / M        │
│ [Load more]           │  candidate.txt    add        view exact bytes     │
│                       │                                                     │
│                       │ [Approve checked result]                           │
│                       │ After approval: Pause writers, then [Apply]        │
│                       │ [Details: IDs · revisions · CLI commands ▾]       │
└───────────────────────┴─────────────────────────────────────────────────────┘
```

This is a structural wireframe, not approved styling. Exact spacing, typography, color, iconography, and comfort at different sizes require human visual review on the real GTK4 display.

1. Open **Integrations**. A service badge says **Core connected** or **Core unavailable**. The project selector shows the registered source name and full path on focus/tooltip; it never defaults to an unrelated project. A schema below 5 says **This project needs a reviewed upgrade before checked-queue status is available** and links to the CLI preview flow. A manual-policy project says **Checked queue is off for this project; manual integration remains available in the command tool**.
2. Select a queue item. The heading states its destination type and exact path, task title/ID, saved result ID, stage, pending reason and freshness. The list is navigation; a fresh `integration.queue.show` supplies detail state. An item with `pending_apply` and `stale=true` says **Destination or evidence changed; review a fresh result before approval** and has no active approval button.
3. Review **Changes**. Explain B (baseline), C (current destination), R (incoming result), and M (candidate) in plain language, then show path decisions. Fetch every metadata page using returned `next_after` with one pinned plan revision. A selected path shows exact version bytes, size, digest and mode through bounded content pages; do not call this a generated text patch. Binary or oversized records show metadata and an explicit export path for external review. A revision change or `RESPONSE_TOO_LARGE` interrupts review with a restart/export action; never approve against partially loaded or mixed-revision pages.
4. Review **Check**. Show required profile ID/revision, selected run state and each step's command, exit code, timing and retained output status. A missing, failed, cancelled, interrupted or stale run says **Check did not pass** with the actual reason. A passing run remains bound to an exact candidate/profile; it does not prove the destination is still safe.
5. Press **Approve checked result** only after complete plan metadata is loaded and the person explicitly confirms that they inspected this candidate and check. Re-read queue, plan, and run immediately before sending `integration.approve` with exact `project`, `plan`, `revision`, `token` (the plan's `review_token`), and `check_run`; never use `accept_unchecked` for this queue. The server's refusal is authoritative. An accepted approval says **Approved for this exact revision**, not **Applied**.
6. **Apply to destination** is a separate action with the full target path, revision, and change summary. It requires an unchecked-by-default acknowledgement: **I paused editors and agents writing this destination and will keep them paused until Solace finishes or recovery is resolved.** The GUI cannot verify that statement; it sends `writers_paused=true` only on confirmation. Re-read queue/plan before `integration.apply`, then show accepted operation and poll `operation.show` until terminal. Timeout or socket loss means **Outcome unknown; inspect the same operation**, not success or a new apply request.
7. On terminal success, show the immutable receipt and applied result, with copyable run/plan/receipt IDs. A later view may offer forward undo only after that separate feature ships.

An expert can open **Details** at any stage to copy the intent, task, result, plan, run, operation and receipt IDs; inspect exact revision and digest bindings; read the server reason code and page cursor; and copy the equivalent CLI show/diff/check command for independent inspection. The expert follows the same approval and writer-pause gates. Details may explain a pending conflict or failed check, but it must not offer a bypass button.

## State and controls

| Public state | Novice-facing label | Available control |
| --- | --- | --- |
| No registered projects / no checked intents | **No checked results to review**; explain manual mode separately | Refresh; select another project |
| `queued`, `preparing`, `checking` | **Preparing result** / **Running approved check** | Inspect operation/run; refresh; no approve/apply |
| `pending` from profile, conflict, check failure/interruption, policy or target change | **Needs work**, with exact reason in words and code in Details | Inspect retained plan/run and copy the safe next CLI step; no approve/apply or automatic rerun |
| `pending_apply`, `stale=false`, plan `prepared`, run `passed` | **Needs your review** | Inspect all pages; then Approve checked result |
| Any `stale=true` or fresh refusal | **Changed since review** | Refresh and begin a new review; no stale-token reuse |
| Approved plan with retained client approval | **Approved, not applied** | Separate Apply with writer-pause confirmation |
| Accepted apply / `recovery_required` | **Applying** / **Recovery needs attention** | Inspect original operation and recovery evidence; never submit a second apply with a new ID |
| `done` with receipt | **Applied** | Inspect receipt; no mutation |

The queue exposes `stage`, `reason`, `stale`, `stale_reasons`, `next_action`, plan/run IDs and receipt ID. Its `pending_apply` stale status includes a live target scan (`solace_core/policy_queue.py:68-158`). Display precise server reasons, not a generic failure. An approved plan can still have a `pending_apply` intent until publication is recorded, so the GUI's approved-in-flight state must be tied to the exact plan/revision/request, not inferred from stage alone.

For an empty queue, keep the project selector visible and say **No checked results to review in this project**; do not render an empty approval panel. For a disconnected core, retain the last rendered item as clearly marked **Last seen, possibly outdated**, disable all mutation controls, and offer **Reconnect**. For a failed page, show the page error next to the affected list or Changes tab and leave review incomplete. On restart, load any pending-action record before enabling controls: show **Checking the outcome of your previous action** and inspect/replay its exact request, then restore the associated selected item or show a recoverable error. If the record is unreadable, show its path and request ID when available, disable new apply, and direct the operator to inspect the original operation through the CLI. Keyboard focus must land on the status or error heading after these transitions; long paths and identifiers remain selectable and copyable at narrow widths and large text sizes.

## Data, refresh, and implementation dependencies

Use `core.status` capability `policy.v1`, `project.list/show`, `task.show`, `project.policy.show`, `integration.queue.list/show`, `integration.show/diff`, `integration.check.show`, `integration.approve/apply`, `operation.show`, and `integration.receipt` through `CoreClient` (`solace_core/service.py:196-248,330-365`; `solace_core/integration_protocol.py:4-20`). Resolve the full source destination from `project.show`; resolve a parent workspace destination from the parent `task.show.workspace.path`, using the intent's `target_key`. If the path cannot be resolved, show the target key in Details and disable Apply until the full path is known; the writer-pause acknowledgement must name the actual destination. Queue and diff content are bounded: queue pages allow 1–256 rows, metadata diff pages allow 1–256 entries, and byte pages return at most 48 KiB raw with `next_offset` (`solace_core/policy_queue.py:139-146`; `solace_core/integration_review.py:143-200`). Keep project, intent, plan ID/revision, run ID, profile revision/digest, review token and approval token associated with one review session; never reuse tokens after a mismatch.

**Reviewer ruling:** Poll bounded pages only while this view is visible, and refresh the selected intent/plan/run immediately before approve or apply. Start with a five-second visible-view interval and one in-flight query per project; pause while hidden, back off on errors, and discard out-of-order responses using a view generation number. This trades a small status delay and read/scan cost for a simple first implementation. Queue worker transitions do not emit queue-specific events, so `event.subscribe` cannot replace polling here (`solace_core/policy_queue.py:229-351`; `solace_core/server.py:112-130`). Event-driven refresh remains later work. Move socket I/O and content decoding off the GTK main loop; dispatch bounded presentation updates back to it. Never poll byte pages or run a command automatically.

**Actionable-list gap:** Current `integration.queue.list` reads `rowid > after ORDER BY rowid`, with no newest-first or stage filter. A bounded client poll can miss a recent actionable intent behind retained history. Before claiming reliable discovery, run this predeclared scale probe: seed **1,024 historical done intents plus one newest `pending_apply` intent**; the first view must expose the actionable item in **at most two bounded queue-list calls**, without scanning all history. The current API cannot meet that request-count oracle. A later implementation plan must add a server-side recent/actionable page or equivalent indexed query, preserving existing forward-cursor behavior for compatibility, then pass the probe. Until then, a prototype may show loaded pages and **More history**, but must not claim complete actionable discovery.

**Reviewer ruling on ambiguous mutation replies:** The v1 mutation UI requires a private durable pending-action record under a mode-0700 directory in its XDG data area. Give each approve/apply action one UUID request ID. Before sending either request, atomically write its exact method, project, request ID and parameters to a mode-0600 file; fsync the file and containing directory. Parameters include the review or approval token, so protect the record as a credential. Keep the record, including any observed operation ID, until identical replay resolves an ambiguous reply or the original operation reaches a terminal outcome. `CoreClient.request` accepts an explicit ID and the core supports same-request replay (`solace_core/client.py:35-55`; `docs/result-integration.md:34-45`); a new request-status endpoint is not a prerequisite. Preserve the approved response token securely, or retain the approval request for same-ID replay, until apply completes or the approval is invalidated. Never generate a replacement request ID for an unknown outcome. Do not store prompt bodies or file bytes. This decision costs careful local-state permissions, atomicity, cleanup, and a restart recovery UI; a transient in-memory token is insufficient.

The existing core installer does not package GTK4 UI files. Installation, UI dependencies, and real-display acceptance therefore need a separate implementation/release plan. No GUI delivery is implied by the installed core's `policy.v1` capability.

## Acceptance probes for implementation

1. **Source/UI parity:** On a disposable project, compare GUI project, queue, plan, diff page, run and receipt fields to exact CLI JSON. Test empty/manual/schema-4 and >256-entry pages; a mismatched revision must discard all mixed pages.
2. **Actionable discovery:** Run the 1,024-plus-one scale probe and count actual queue-list calls. A first page of old done entries that hides the new actionable intent fails.
3. **Safety controls:** Inject a live destination edit after GUI shows `pending_apply`. The next fresh read must show stale and disable Approve; a refusal must never trigger automatic prepare/check/apply retry. With a clean destination, Approve and Apply are separate clicks; omitting writer-pause acknowledgement sends no apply request.
4. **Loss and restart:** Before each approve/apply send, verify the mode-0600 pending-action file and its fsynced parent directory exist with the exact request ID and parameters. Drop the socket after core acceptance but before GUI response handling; restart the GUI. It must load that record before enabling new mutations, replay only the identical request, recover the original approval token or operation ID, and inspect the original operation/receipt through terminal outcome. Count one approved revision and one receipt, with no duplicate publication. Repeat with a crash immediately after record fsync but before send; identical replay must still be safe. Verify eventual record cleanup and no prompt or file bytes in the record.
5. **Read-only browsing:** Repeated queue/diff/status polling on a disposable idle store must not create SQLite WAL/SHM sidecars or mutate source/workspace files. Measure before/after file identities and store sidecars; a no-op poll count alone is insufficient.
6. **Real display and accessibility:** On a permitted GTK4 display, use keyboard only to select project, navigate queue, read full paths/reasons, traverse diff pages, inspect check evidence and reach both confirmation dialogs. Text and accessible names carry meaning without color; focus returns to the originating item after dialogs. Test narrow window, large text, long paths and disconnected service. A human reviews hierarchy and wording; automated checks do not approve visual feel.

## Decisions and handoff

- **Decided by reviewer:** visible-view bounded polling with fresh reads before mutations; event streaming later. Tradeoff: up to one poll interval of stale list status and repeated read/scan cost, bounded by view visibility and page size.
- **Required implementation prerequisite:** server-side recent/actionable paging if the 1,024-plus-one probe fails, as expected from current ascending-only pagination. Do not hide this dependency in GUI code.
- **Decided by reviewer:** v1 approve/apply UI requires the private, fsynced pending-action record and identical request replay. A new request-status endpoint is not required. Cost: sensitive local state, atomic write/cleanup care, and explicit restart recovery UI. Mutation controls remain disabled until the record's recovery behavior passes the loss-and-restart probe.
- **Human visual gate:** review wireframe placement, labels, density, and large-text behavior on a real display before accepting GUI implementation.
- **Separate scopes:** forward undo and external-writer handoff are independent designs. This GUI never treats checked queue as automatic apply authority.
