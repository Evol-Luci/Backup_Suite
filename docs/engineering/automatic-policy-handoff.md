# Checked-queue policy source handoff — 2026-10-29

## State and scope

Source worktree: `/home/lucievol/Projects/Backup_Suite/.worktrees/automatic-policy-design`, branch `codex/automatic-policy-design`. The reviewed and tested **code HEAD** is `6e11bc21118d3b7ec311e38b53b94ddaface4f4a`, based directly on local `main` at `80232314dd6f51feb0bc8b3ce563ba923cf36c13`. The documentation-only handoff commit follows the code HEAD; use `git rev-parse main` for the final local-main commit. The worktree and its ignored `.superpowers/sdd/2026-10-27-automatic-integration-policy/` evidence directory are preserved for a fresh agent.

The [approved spec](../superpowers/specs/2026-10-27-automatic-integration-policy-design.md), [implementation plan](../superpowers/plans/2026-10-27-automatic-integration-policy.md), [operator guide](../automatic-policy.md), task reports and execution ledger define the work. Schema 5 adds owner-confirmed project policy, task overrides, exact required-profile binding and durable ready-result intents. The queue prepares and checks an exact candidate, then stops at `pending_apply` for human review. It never approves, applies, or claims that external writers are paused. New and upgraded projects default to manual.

## Review and final source evidence

Independent reviews approved Tasks 1–7. The whole-branch review found one Important queue-status defect: an external destination edit could leave an intent reporting `pending_apply` and `stale=false`, although approval refused it. The fix detects live root and child destination changes, retains a pending reason and never reruns the check. Scoped review then found that the first status fix could create SQLite WAL/SHM sidecars. The follow-up uses read-only catalog access and a scan-only engine adapter; scoped re-review found no remaining Critical or Important issue. A previously observed SQLite `ResourceWarning` was traced to the retained-mount test fixture and fixed. The review reports and focused logs are in the preserved execution directory; code fix commits are `0e8320a` and `6e11bc2`.

Final commands ran **after the last Python code change**, separately with `SOLACE_TEST_TMPDIR=/tmp` and `PYTHONDONTWRITEBYTECODE=1`. Evidence is under `.superpowers/sdd/2026-10-27-automatic-integration-policy/final-source-evidence-20261029/` in the source worktree:

| Evidence file | Result | SHA-256 |
| --- | --- | --- |
| `core.log` | 741/741 in 317.685s | `33656c3bb971c0dd2cb9598a6ff0eae5970a7572ddc635bc83a49025c64d54b0` |
| `engine.log` | 52/52 in 1.844s | `fd12593260ec377a88c1092858d2526e8833f548474032bde55724b2a0034e21` |
| `top.log` | 16/16 in 0.381s | `5ef9ce9448553ac3c3b4beba9b05db6a57d16a37049696091f5bc541a20604fe` |
| `socket-acceptance.log` | 7/7 in 25.332s | `fc60d0b2b86c08c654c01c5dacf619998fd8de4a33f38ec58a714698ad1cfd50` |

All four commands exited 0. Their logs contain no `ResourceWarning`, `ERROR`, `FAIL`, or traceback; `git diff --check` passed. The socket suite uses disposable source/workspace/store paths and the real CLI plus Unix socket. It verifies default manual mode; owner-confirmed policy and task override; required profile selection and refusal of different/missing selection; checked child-to-parent and root-to-source paths without destination mutation before human checked approve/apply; conflict and failed-check pending states; target, policy and profile staleness; restart and request replay without a second command; and a schema-4-to-5 preview/apply. Its upgrade case opens the generated backup read-only, requires `PRAGMA integrity_check = ok`, and verifies the backup still reports schema 4 while the upgraded store reports 5. The fixture removes its temporary backup at cleanup; no real user store was upgraded.

## Installer preview and installed boundary

The read-only `python packaging/install-core.py --preview --preview-dir /tmp/solace-policy-preview-20261029` exited 0 and proposed runtime `/home/lucievol/.local/share/solace/core-runtimes/f984d87c3d8ba24e` with schema support 1–5 and new-store schema 5. Its `installer-preview.log` SHA-256 is `d2c9b241069771b7c4704ffa640363f6281bbd4e209723789e962b74b3f69c14`. `systemd-analyze --user verify` passed for the generated socket and service. The proposed replacements are `~/.local/bin/solace`, `~/.config/systemd/user/solace-core.socket`, and `~/.config/systemd/user/solace-core.service`; the preview reports `backup: null` because nothing was installed.

At handoff, the installed launcher points to runtime `97b4e825499f5bd6`. A live `solace core status --json` returned healthy with no registered projects, `checks.v1`, supported schemas 1–4 and created schema 4; it did **not** advertise `policy.v1`. Source tests and the installer preview do not constitute installed policy acceptance. Installing the previewed runtime, changing user units, upgrading any existing project store, and applying any result to a real source are separate actions. Runtime rollback does not downgrade a schema-5 database.

## Next operator sequence

1. Verify local `main` and this handoff commit with `git status --short --branch` and `git log -3 --oneline`; the source worktree and ignored evidence remain available.
2. If installation is authorized, refresh the installer preview against that exact main tree, review the target paths and rollback/backup output, then install and activate through the documented installer flow. Do not upgrade existing stores during installation.
3. Run disposable **installed** acceptance through the actual user service: confirm `policy.v1`, create a new disposable schema-5 project, observe one checked `pending_apply` result with unchanged destination, then explicitly review, approve, pause writers and apply. Record runtime and backup IDs, service health, receipt and rollback eligibility. Any existing-store upgrade needs its own preview and decision.
4. Continue separately scoped forward undo and GUI review/status design. True automatic apply requires a writer-handoff contract for external agents/editors.

## Rulings retained from implementation

- Tasks 2 and 3 tested domain methods before Task 7 added strict public protocol and CLI validation. Cost if wrong: public defects would surface at Task 7; the final socket suite now covers that boundary.
- A task's checked-queue opt-in is effective only against its confirmed project policy revision. Cost if too strict: an owner must reconfirm after a benign policy change; the rule prevents an old opt-in from silently reactivating.
- Earlier suggestions for broader negative-code and receipt-provenance assertions remained optional test strengthening. Cost if wrong: a specific error code or provenance field could regress despite the public approval/receipt acceptance paths. No open Critical or Important review finding remains.

## Local-main closeout

After source review and the documentation commit, local `main` fast-forwarded from `8023231` to `a06024a6aaae73fa9839432a4eca14ff6b47ed17` without a conflict. A second verification ran from that exact merged tree, with no Python changes after the tested code HEAD:

| Evidence file in the same preserved directory | Result | SHA-256 |
| --- | --- | --- |
| `main-core.log` | 741/741 in 340.783s | `9bbe2134117ba4b06babf00301a2e167605c1bbd2377988a7e5aa1af3c9431f1` |
| `main-engine.log` | 52/52 in 1.916s | `018668bc2d63716005abe936674b330cd943ff9c1008ef45dccaff3776de3236` |
| `main-top.log` | 16/16 in 0.465s | `8e399a653af27d0ff7f7a387916fc1f7d068920074894b2fcb284199de9c298e` |
| `main-socket-acceptance.log` | 7/7 in 28.179s | `c95a2787e432b825e524682494b7531f82a8d02bdb765be8878f3f1e2110125e` |

All four merged-tree commands exited 0, and the logs have no warning, error, failure or traceback. The installer preview repeated from `main` proposed the same runtime `f984d87c3d8ba24e` with the same preview-log hash above; generated user units verified again. This closeout is a documentation-only follow-up to the tested tree. At this handoff, local `main` contains the policy work and is ahead of `origin/main`; no push or installation was performed.
