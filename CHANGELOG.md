# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) for public releases.

## [Unreleased]

## [2.6.0] - 2026-08-30

### Added

- Added opt-in AudnCode support on Windows with hook shape 9: seven synchronous 60-second handlers for `SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`, `Notification`, `PostToolUse`, and `SubagentStart`. `SessionStart` matches `^(startup|resume|clear)$`, `Notification` matches `^(idle_prompt|permission_prompt)$`, and `PostToolUse` matches exactly `^(Agent|AskUserQuestion|Bash|PowerShell|Monitor|TaskStop|KillShell|CronCreate|CronDelete|SendMessage)$`.
- Added one compact `permission_prompt` notification for a transcript-proven root `AskUserQuestion`. Its durable session, root prompt epoch, and `tool_use.id` identity deduplicates resumes; generic permissions, nested or sidechain questions, ambiguous tails, and already answered questions fail closed. A correlated answer opens a new epoch before execution continues, so a later final result can notify exactly once.
- Added an optional per-attempt managed-recovery contract for AudnCode launchers. A declared revision-1 marker holds `StopFailure`, `recovered` cancels it, and only an exact revision-2 `exhausted` marker with the same transcript error UUID may continue through the ordinary finality gates. Missing, malformed, rewritten, path-escaped, reparse, permission-broadened, process-reused, or identity-mismatched declared markers never fall back to a false terminal notification. Recovery root, manager directory, and marker DACLs are protected and restricted to the current user, SYSTEM, and Administrators; marker age alone never rejects or promotes an attempt.
- Added AudnCode correlation using a synthesized prompt identity, session epoch, validated transcript path, hook-process ordering, and a host-runtime identity derived from AudnCode home, PID, and `startedAt`. The runtime-scoped background registry survives `/clear` and `/resume` while rejecting stale markers and PID reuse.
- Added final-idle probes for persisted command-queue operations (`enqueue`, `dequeue`, `remove`, and `popAll`) aggregated across the ordered session lineage of one host runtime, background tool IDs and explicit terminal IDs, session/team/custom task lists, team activity, and main-session Ctrl+B sidechains.
- Added `-EnableAudnCode`, `-AudnCodeHome`, and `-AudnCodeIdleThresholdMs` installer options. The default 1,000 ms final-idle threshold is written to the active AudnCode global configuration.
- Added AudnCode `CronCreate`/`CronDelete` observation, a host-scoped cron registry, and causal reconciliation against `.claude/scheduled_tasks.json` plus the native `scheduled_tasks.lock` owner/lifetime. An exact correlated `CronDelete` closes only a session-only incarnation owned by that host; durable deletes remain diagnostic, and an empty task file is not standalone completion proof.
- Added the private, generation-stamped AudnCode hook-observation marker `.codex-ntfy-hooks.json`. Hosts predating a new or repaired hook shape remain fail closed until restart, while an identical reinstall preserves the generation and needs no restart.
- Added installer-controlled expected-event arguments to every managed AudnCode hook. Prompt/tool events arm a durable per-home ingress token before host discovery, stdin read, or JSON parsing and transfer it to the exact host-runtime guard without an unprotected interval.
- Added bounded AudnCode evidence processing: 8 MiB raw hook input, 512 MiB/4,096 relevant records across queue lineage, 1,048,576-character queue lines, 65,536-character queue content, 1 MiB team/task files, and 1,024 team members. Exceeding a limit fails closed.
- Added guarded `SessionStart` coverage for direct initial-plan/complex-content queries that can bypass `UserPromptSubmit`, counted `SubagentStart` coverage for resumed local-agent incarnations, and pre-armed CCR claims for `/ultrareview` remote work.

### Changed

- AudnCode `Stop` is treated only as a completion candidate. A normal result is promoted only after a later, matching `idle_prompt`; `StopFailure` replaces only that idle proof after a unique current-prompt transcript error is correlated, while every other final-idle gate remains required. `agent_completed` is ignored because it does not prove that the complete AudnCode query loop has returned.
- All managed AudnCode hooks run synchronously with a 60-second upstream timeout because AudnCode 0.9.x notifications do not carry `prompt_id`. Process-start ordering prevents a delayed idle process from an older prompt from releasing newer work.
- A retained non-lead team directory blocks until AudnCode's `TeamDelete` removes it. `isActive: false`, a missing member, or an empty member list is not terminal proof. Explicit session, team, and valid `CLAUDE_CODE_TASK_LIST_ID` task lists must contain only completed tasks.
- A Ctrl+B main-session sidechain remains active until a correlated terminal `<task-notification>` appears in either native external main-session form: a `user` record with `origin.kind: task-notification`, or an `attachment` record with `type: queued_command` and `commandMode: task-notification`. Control lines remain anchored while raw XML characters in result/summary text are tolerated; pasted or sidechain-local records are not proof.
- AudnCode background evidence receives a minimum 1.25-second settle window after normal `idle_prompt` or correlated `StopFailure` terminal proof, independent of the general idle grace. The complete gate runs three times while the record is still pending; the third pass precedes its locked outbox commit.
- AudnCode settings and the final-idle threshold normally hot-reload for the next turn, but a first installation or changed hook shape requires AudnCode to be closed before installation and reopened after the observation marker is committed. Hook execution still requires the current workspace to be trusted by AudnCode.
- AudnCode notifications reuse the compact provider-neutral presentation: one ntfy status emoji plus the conversation/project title, no `done` word or model name, a label-free body, and strict UTF-8 handling.
- The adapter documents the public-build boundary: exact counted terminal evidence can close a fresh asynchronous local agent, but successful/malformed `SendMessage`, overlapping same-ID resumed agents, and unresolved CCR work remain sticky when the public runtime provides no durable consumption/identity proof.
- Outgoing notifications now always contain exactly one tag: `warning` for every terminal non-success, otherwise the first valid configured tag or `white_check_mark`. Legacy multi-value settings are normalized instead of blocking an upgrade.
- Display text is normalized to NFC, strips unsafe control/bidi formatting, preserves complete grapheme-like emoji sequences, and never emits invalid scalars or U+FFFD. Titles are capped at 60 display clusters and 240 UTF-8 bytes; messages remain capped at 3,500 bytes without splitting a cluster.

### Fixed

- Hardened bounded stdin handling on Windows PowerShell 5.1 so oversized or stalled AudnCode hook payloads fail closed without racing an asynchronous Console read during process shutdown.
- Made the recovered-failure write-ahead journal portable between Windows PowerShell 5.1 and PowerShell 7 by hashing the producer's exact serialized JSON instead of engine-specific reserialization. Cleanup uses immutable logical Stop identity rather than mutable queue fields, accepts a newer valid duplicate Stop, and recognizes exact delivery or terminal suppression receipts. A content-free active index limits reconciliation to live journals; deterministic restart repair erases the content-bearing successor across every two-file crash gap while the identity-only replay tombstone remains permanent.
- Prevented intermediate AudnCode stops, recursive stops, subagent events, mismatched transcripts, stale session epochs, delayed old idle events, and unordered hook launches from producing a final notification.
- Prevented missing, ambiguous, delayed-old, duplicated, malformed, unstable, or payload-mismatched AudnCode `StopFailure` transcript evidence from being treated as a terminal chat.
- Prevented queued commands, background Agent/Bash/PowerShell/Monitor work, successful or malformed `SendMessage`, unresolved same-ID incarnations, explicit/custom goal tasks, retained non-lead teams, CCR work, and Ctrl+B sidechains from being mistaken for a final idle state.
- Preserved independent delivery and deduplication across simultaneous AudnCode sessions and existing Codex or Claude Code sessions.
- Resolved a shared-session-UUID collision between live AudnCode windows without cross-attributing output: the superseded host remains a hard gate until that exact process lifetime supplies an ordered `Stop` then `idle_prompt`, after which only its lifetime is retired and the current owner can complete.
- Kept a superseded host as a hard gate after `Stop` plus `idle_prompt` until that exact host runtime also proves its background, cron, and lifecycle guards clear; foreground idle alone can no longer forget work owned by the previous window.
- Made the AudnCode installer follow a nonblank `CLAUDE_CONFIG_DIR` by default while keeping explicit `-AudnCodeHome` precedence, so separate configuration profiles can be installed per home without silently wiring the default profile instead.
- Prevented AudnCode `Stop` plus foreground idle from notifying while scheduled work remains. Only a non-replayed same-runtime `CronDelete`, proven by its stable post-cursor transcript `tool_use`/`tool_result` pair, can close a session-only incarnation; durable delete requests and empty durable files cannot release work without the causal native scheduler lease/file boundary. Reused IDs and late, foreign, missing, malformed, synthetic, or ambiguous evidence remain fail closed.
- Pre-armed `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `SubagentStart` before slower correlation as applicable, then pre-armed background/cron lifecycle mutations in session and host state. A killed, timed-out, oversized, malformed, event-mismatched, or uncorrelated hook leaves durable busy/lost evidence that later terminal events cannot clear.
- Tracked overlapping background and cron hooks with independent bounded guard tokens; one completed mutation can no longer clear another launch/create/delete still in flight.
- Kept uncorrelated or failed lifecycle observations sticky across concurrent successful commits, and reconstructed the exact host guard from fallback session correlation whenever possible, so one valid hook cannot heal evidence lost by another.
- Treated every successful `Monitor` result as a background launch and recognized Agent `isAsync` plus Bash/PowerShell user/automatic-background flags. A claimed asynchronous launch without a valid ID now fails closed.
- Rechecked live session pre-arm, registry-validity, lifecycle-loss, pending-token, and all exact host-guard states under fixed-order locks through final outbox promotion, closing the race in which a new launch could begin after the last idle snapshot.
- Held the per-home ingress fallback lock ahead of the exact ingress, background, and cron guards through the pending-to-outbox move. A hook that has started discovery can no longer be hidden in the gap between the first fallback read and the host-guard lock.
- Preserved every exited superseded host tuple until that exact runtime independently proves ingress, background, cron-lifecycle, remote, team/task, sidechain, and durable-cron finality. A later valid hook from the surviving window can no longer prune detached work merely because the former parent process exited.
- Rechecked observed host lifetimes while the session lock is held and made failed fresh or resumed `SubagentStart` registration leave sticky lifecycle-loss evidence before ingress completes, so a late event cannot disappear during a handoff.
- Required the exact cron lifecycle guard to be clear in exited-host finality; an empty cron registry or task file cannot mask a pending, lost, or malformed cron mutation.
- Released process-bound and session-only uncertainty after the exact AudnCode host lifetime exits, including the otherwise unobservable memory-only cron state of a host that predates hook installation. Observability uses the independently verified Windows process start rather than a potentially delayed session-marker timestamp; structural runtime validity is tracked separately and must validate canonically without repair before finality. Every host guard must still be clear, and active, missing-when-expected, malformed, or unstable durable cron evidence across every project in the runtime lineage remains blocking.
- Compacted no-cron logical session history without dropping the current registration or any active cron owner, so more than 256 `/clear` lineages do not permanently mute an otherwise idle runtime.
- Compacted long AudnCode session lineages only after every discarded session independently proves terminal local state, empty remote claims, no live sidecars, no pending lifecycle guard, and tombstoned sidechains. Reused sessions move to the durable lineage tail, while an old live remote agent keeps the complete history fail closed.
- Avoided rewriting unchanged empty AudnCode remote-claim state on every idle probe, reducing needless disk churn and notification latency on long-running sessions.
- Preserved cron registry identity across the busy-to-idle state rewrite.
- Preserved live AudnCode background, cron, and lifecycle-guard records during retention cleanup, including windows open beyond the normal state-retention age.
- Installer upgrades stop only verified orphaned legacy AudnCode hook processes; current-shape or still-parented processes are preserved.
- Canonicalized AudnCode home and transcript paths before any durable ingress arm, preventing Windows 8.3 aliases such as `RUNNER~1` from splitting one session into unrelated host/runtime records under long-running CI or multi-window use.
- Preserved inherited and explicit Windows ACLs across atomic Claude/AudnCode settings updates without materializing inherited entries as duplicate explicit permissions. Replacement rollback now releases its compare handle before restoring, and the documented uninstall follows the same metadata-safe algorithm on Windows PowerShell 5.1 and PowerShell 7.
- Made legacy AudnCode orphan cleanup compare the parent process creation time as well as its PID, so PID reuse cannot keep an obsolete hook alive while an unverifiable lifetime remains fail closed.

### Security and rollback

- AudnCode transcript paths must match the session UUID, exist below the configured AudnCode `projects` directory, and correlate with the locked session state. Malformed UTF-8, payloads above 8 MiB, and trusted-event mismatches fail closed.
- The public AudnCode 0.9.x build can omit a correlatable ID or durable consumption proof for `SendMessage`, overlapping same-ID resumes, `clearCommandQueue`, kill-all, some Ctrl+B paths, and session-only cron firing. The notifier may withhold a true final notification when exact evidence is absent; it never promotes uncertainty into an intermediate notification.
- The Windows installer preserves unrelated AudnCode settings/hooks, atomically replaces the settings file, and creates private reference snapshots of pre-existing settings, global configuration, and the hook-observation marker. One global installer mutex serializes the shared task and `CodexHome` transaction before the nested per-home marker lock. Process shutdown and task ownership require exact canonical paths and process lifetimes. After a later failure, conditional per-event/per-field rollback changes only managed hooks and `messageIdleNotifThresholdMs` values that still match this installation; even a post-write marker ACL/verification failure is rolled back under the same lock and only while the full content still equals the value written by that run. AudnCode itself exposes no shared settings lock, so it should be closed during hook-changing installs.
- The Remote Windows helper now performs strict UTF-8 and semantic preflight before any mutation, rejects non-object JSON roots, removes foreign explicit ACL entries before staging secrets, and rolls back file bytes, ACLs, state, backups, and scheduled-task state with compare-and-swap checks after any late failure.
- The selective AudnCode uninstall procedure now serializes shared settings at PowerShell's full supported JSON depth instead of risking truncation of deeply nested unrelated configuration.
- JSON configuration, queue/state, lifecycle, title, and watcher paths now reject invalid UTF-8/Unicode scalars. Invalid source titles fall back to the project and invalid optional message text is omitted rather than repaired with replacement characters.
- Canonicalized valid JSON Unicode escapes before applying Codex lifecycle semantics in both runtimes, so an escaped later `task_started` invalidates an earlier staged terminal instead of producing an intermediate notification.
- Made authoritative SQLite spawn edges override generic source metadata, preventing a child session recorded as `source=vscode` from bypassing default subagent suppression.
- Confined database-derived rollout paths to canonical `sessions` or `archived_sessions` roots, rejected traversal/reparse escapes, and required the embedded session identity to match before lifecycle evidence is trusted.
- Upgrades now add missing `include_message` and `include_thread_title` fields as `false` on local Windows, Remote Windows, Linux, and WSL. Existing explicit opt-ins are preserved.

## [2.5.2] - 2026-07-18

### Fixed

- Windows cold lifecycle probes now parse relevant rollout records structurally in C# one line at a time and return a fixed 23-field summary. Large Codex conversations no longer marshal their complete lifecycle history back into PowerShell and deserialize every record a second time before a pending completion can be evaluated; an incomplete final JSONL record returns a bounded busy summary without invoking the full compatibility parser.
- The conservative incremental rollout fallback now processes 64 KiB chunks while retaining at most one capped incomplete line, instead of repeatedly concatenating the complete unread tail; descendant lifecycle checks use the same fixed-size native summary on their normal path.
- WSL/Linux probes check the snapshot's final byte before materializing unread data, so a large half-written JSONL record remains pending without allocating or rescanning the complete tail.
- Windows and WSL/Linux lifecycle probes now fail closed on malformed UTF-8, invalid lifecycle shapes, missing string turn IDs, and incomplete trailing JSONL records in both root and active descendant rollouts. A corrupt or half-written later turn can no longer release an earlier completion, even when balanced fallback is enabled.
- Summary message limits no longer split a UTF-16 surrogate pair, and message-disabled native or incremental probes retain only presence facts rather than private assistant text; changing the privacy mode forces a safe replay instead of reusing incompatible cached state.
- A latest-only probe cached while checking descendants now retains its terminal turn and event type, so a later turn-less `Stop` can still recover the completed root turn.
- Remote-scan timeout accounting now starts after the child process has actually been created, so slow PowerShell startup is not incorrectly charged against the remote scan's runtime budget.

## [2.5.1] - 2026-07-18

### Changed

- With `markdown: false`, an opted-in final-message excerpt is converted to compact plain text before one-line normalization and size limiting: presentational Markdown is removed while link labels and table-cell text are retained. Explicit `markdown: true` keeps Markdown behavior.

### Fixed

- Windows hook payloads are read from raw standard input with strict UTF-8 decoding instead of the active console code page, preventing non-ASCII JSON from becoming mojibake and rejecting malformed byte sequences rather than silently replacing them.
- Bounded Claude transcript-tail reads align their starting offset to a valid UTF-8 code-point boundary, so a window beginning inside a multibyte character cannot corrupt title or goal-state parsing.
- An epoch-anchored Claude candidate that cannot pass the final session/epoch commit check is terminalized locally with receipt reason `claude-session-unverifiable` and is never sent, instead of remaining in an unproductive retry loop. Maintenance also preserves session state referenced by a pending Claude candidate.

## [2.5.0] - 2026-07-17

### Added

- Added opt-in Claude Code support on Windows for Claude Desktop's Code tab, the CLI, and VS Code through managed `Stop`, `StopFailure`, ordered `UserPromptSubmit`, and optional `Notification` hooks.
- Added stable Claude deduplication with `session_id + prompt_id`, provider-isolated sequence IDs, bounded local Claude title lookup, and multi-session coverage.
- Added a transcript-backed `/goal` gate that follows Claude's newest `attachment.goal_status`: active loops wait, achieved/failed goals release, and manual clear discards without notifying.

### Changed

- Claude main-agent stops notify only when both authoritative `background_tasks` and `session_crons` registries are present and empty. Missing registries, subagent stops, and stops with active work fail closed without creating terminal receipts.
- `Stop`, `StopFailure`, and `UserPromptSubmit` run synchronously to preserve Claude's lifecycle order. Their initial transcript reads are bounded, while `idle_prompt` and `agent_completed` remain asynchronous accelerators rather than correctness requirements.
- ChatGPT task links are now explicitly limited to Codex records; Claude notifications never generate a misleading Codex task URL.
- The Windows installer can merge Claude handlers with `-EnableClaudeCode`, preserves unrelated settings/hooks, writes atomically, remains idempotent, restores the original Claude settings file if installation later fails, and validates the newest binary for each detected Claude surface separately.
- Claude prompt-start baseline reads are capped at 1 MiB; the reverse transcript scanner skips proven non-goal oversized records in bounded memory and fails closed on oversized lifecycle records.

### Fixed

- Claude Code on Windows no longer appears configured while silently producing no notification: the installer and runtime now provide the missing provider-specific lifecycle path instead of attempting to classify Claude payloads as Codex rollouts.
- Prevented intermediate `/goal` stops, same-prompt async reordering, stale stops from superseded prompts, stale terminal markers, session-epoch promotion races, and manual goal clears (including after a transient baseline read) from producing misleading or poisoned notifications.

### Security

- Claude hooks use shell-free executable/argument form, an absolute PowerShell path, and no ntfy credential in `settings.json`. Transcript inspection extracts only lifecycle booleans plus an opaque marker, never the goal condition or reason.

## [2.4.3] - 2026-07-13

### Added

- Added an opt-in authenticated ChatGPT task URL as the ntfy `click` target, with canonical UUID validation and a browser fallback when the mobile app does not claim the link.
- Added a separate, default-off `include_task_link_action` option for one visible **Open task** button without consuming notification space by default.

### Changed

- `strict` still never fails open: unverifiable root classification or completion evidence is now retried with exponential backoff, capped by `unknown_retry_max_seconds` (60 seconds by default), then recorded locally as `unverifiable` after `idle_probe_grace_seconds` instead of remaining pending forever.
- A completion followed by a later open task is now suppressed immediately as an obsolete predecessor; only that later task's terminal candidate can notify.
- The persistent Windows local scanner now gets active and recently resumed rollout paths from Codex's read-only SQLite index plus hot current-day paths. Continuous scans no longer recursively walk multi-gigabyte `sessions/` and `archived_sessions/` trees—including the 23 GB history that exposed the regression; a full archive walk remains available only through an explicit manual all-scope scan.

### Fixed

- Preserved existing custom ntfy tag arrays with more than three entries instead of rejecting the entire notifier configuration during an upgrade.
- Moved rollout recovery scans and HTTP delivery into separate supervised background workers so slow local/WSL discovery or idle probes can no longer block ready ntfy deliveries; the scheduled-task supervisor now survives lock collisions and worker exits, with worker leases, scan health, and queued-item age diagnostics.
- Isolated UNC/WSL fallback discovery in its own timeout-bounded scanner, with independent remote cursor handling, so a suspended distro or slow share cannot delay local lost-hook recovery or delivery.
- Added a native streaming lifecycle summary for large Windows rollout files, avoiding slow line-by-line JSON replay in PowerShell while preserving the same idle checks.
- Changed the Windows scheduled task to launch the hidden VBS supervisor directly, eliminating two cold PowerShell launcher starts before the notifier worker becomes ready.
- Deferred receipt-retention cleanup to an isolated maintenance process that starts only after delivery and the applicable local/remote scanners have reported readiness, so large historical state cannot delay worker startup or a completion notification.
- Made WSL classification side-effect free, so a successful Windows bridge cannot accidentally start the native fallback worker and flush old fallback or test records.
- Replaced per-event mutation-lock files in the state root with a fixed set of sharded locks and cleanup for legacy lock debris.
- Windows/WSL upgrades remove only records explicitly created by the notifier's synthetic test command before restarting delivery.

### Documentation

- Redesigned the README opening around the final-only value proposition, an anonymized notification preview, and an earlier install-to-test path.
- Added a GitHub Pages landing page, a reusable 1280×640 social preview, support guidance, and a Contributor Covenant code of conduct.
- Clarified ntfy topic setup and added concise, verifiable discovery language without changing notifier behavior or delivery guarantees.
- Documented the final-only rule and compact title explicitly: one ntfy status emoji plus the conversation title (or privacy-preserving project fallback), with no completion word or model name.

## [2.4.2] - 2026-07-11

### Changed

- The ntfy JSON title now contains only the local task title, or the project name when task-title sharing is disabled or unavailable.
- Task-title lookup now queries the read-only `threads.title` field by exact thread ID before falling back to `session_index.jsonl`, improving freshness and coverage across app and VS Code sessions.
- Removed `Codex`, completion words such as `done` or `stopped`, lifecycle status, and any model-style prefix from the title. The single default `white_check_mark` tag remains the only notifier-supplied emoji.
- Expanded the available display-name budget from 42 to 60 characters now that the redundant prefix no longer consumes title space.
- Idle detection, goal/subagent waiting, continuation coalescing, durable delivery, and the compact body format are unchanged.

## [2.4.1] - 2026-07-10

### Added

- Status-aware compact titles for normal completion, aborted turns, and terminal goal states (`blocked`, `paused`, usage-limited, and budget-limited).
- A 3,500-byte UTF-8 body ceiling with Unicode-safe truncation in both notifier implementations.
- Exact cross-platform payload tests covering Unicode task titles, very large emoji summaries, stopped turns, and already-queued privacy opt-outs.

### Changed

- Notification bodies now use label-free compact context (`origin · #thread8`) and include the project only when a distinct task title occupies the title; full paths remain explicit opt-in data.
- Fresh installations use one `white_check_mark` tag, `max_message_chars: 180`, and plain-text `markdown: false`. Installers migrate only the exact former two-tag default and preserve custom tag sets and existing message-length choices.
- Default priority 3, empty tags, and inactive Markdown fields are omitted from the ntfy JSON request.
- The adjacent-project review now records the compact 120–200-character and single-tag patterns used by comparable notifiers.

### Security and privacy

- `include_message: false` is enforced again at send time, so content in an older pending or outbox record cannot leave the host after the operator opts out.
- Tag, priority, and message-length configuration now has matching validation on Python and PowerShell.

### Fixed

- PowerShell 5.1 now reads UTF-8 task-title indexes with shared-file access and requires an exact thread ID, preventing mojibake and accidental title selection from a different record.
- Python and PowerShell now normalize string/array tags consistently and produce the same compact payload.
- Aborted turns use a `stopped` title without a redundant synthesized body message.
- Removed repeated `Project:`, `Source:`, `Thread:`, and generic `Turn completed.` text from normal pushes.

## [2.4.0] - 2026-07-10

### Added

- Logical root-task idle detection: a completion candidate waits until its matching root turn is complete, no later turn is open, and the rollout has remained quiet for a configurable settling window.
- A modern Codex `Stop` hook source alongside the legacy root-level `notify` compatibility source.
- A continuous incremental rollout watcher that can recover locally persisted `task_complete` and `turn_aborted` events missed by hooks, retains per-file byte offsets, and never advances over an incomplete JSONL tail.
- Bounded slow discovery for recently modified old-date and archived rollouts, while existing cursors remain watched across day boundaries.
- A private `pending/` stage before the network-ready outbox and a `watch/` directory for rollout cursors.
- Per-root-thread coalescing that suppresses older candidates as `superseded` instead of publishing automatic-continuation results.
- Goal awareness that holds a root candidate while its goal status is `active`.
- Recursive descendant awareness that waits for active subagents and uses `subagent_orphan_seconds` to bound abandoned child state.
- `strict`, `balanced`, and `off` detection modes, with strict fail-closed behavior as the default.
- Technical-turn suppression for legacy/watcher candidates that lack user-facing turn evidence.
- Doctor fields for `pending_idle`, `watched_rollouts`, `idle_detection_mode`, `idle_grace_seconds`, `goal_aware`, and `watch_rollouts`.
- Configuration for idle grace/probe timing, goal polling, child-orphan timeout, technical-turn filtering, watcher cadence, and initial replay.
- Installer-managed multi-root rollout recovery for selected WSL distributions, preserving separate Codex and SQLite homes.
- Regression coverage for modern `Stop`, automatic continuation coalescing, active goals, active descendants, missed-hook rollout recovery, and cross-platform hook installation.

### Changed

- Notifications now represent the newest locally verifiable idle epoch of a root Codex task rather than every intermediate `agent-turn-complete` event.
- The idle gate takes a second fresh snapshot before atomic promotion. Once promoted, an outbox record is an immutable delivery epoch and cannot be coalesced with later pending work, including during network retries.
- An event name of `Stop` is no longer treated as proof of a root session: explicitly named `SubagentStop` events are ignored, and descendant `Stop`/legacy/watcher completions are classified from local Codex state and suppressed while still delaying their root.
- Installers now manage a single `hooks.Stop` command in `hooks.json`, remove obsolete managed notifier handlers from other lifecycle events, and preserve unrelated hook groups, handlers, and metadata.
- The legacy `notify` command remains installed as a redundant signal instead of being replaced by the modern hook.
- WSL bridging preserves the originating Codex and SQLite homes, and the Windows worker watches registered WSL roots when both live hook sources are missed.
- Continuous workers also scan rollout state; on-demand workers remain the delivery fallback when a service/scheduled task is unavailable.
- Documentation now covers Codex app, VS Code, CLI, WSL, and Remote SSH semantics, plus selective hook uninstall and rollback.

### Security and privacy

- Newly installed modern hooks require explicit review through `/hooks`. Installers do not modify the Codex trust store.
- Codex SQLite databases are queried read-only/query-only; goal integration selects status and never the goal objective.
- Rollout watcher state stores cursor metadata rather than prompt bodies. User `input-messages` remain excluded from notifier state.
- Strict mode withholds uncertain root/rollout candidates instead of failing open into a possibly premature network notification.
- `hooks.json` and new state directories receive host-native private permissions and are included in protected rollback handling.

### Fixed

- Prevented intermediate notifications when Codex completes one turn and starts an automatic continuation immediately afterwards.
- Prevented a root notification while an active goal or descendant still has work.
- Prevented a descendant session reported through the generic modern `Stop` hook from producing its own notification, including when its rollout appears only after the hook fires.
- Removed global-newest-session assumptions that race across simultaneous Codex app, VS Code, and CLI tasks.
- Added persisted-state recovery for completion signals that never launch a hook process when a continuous worker watches the same `CODEX_HOME`.

### Known limitations

- Pure cloud tasks are not guaranteed unless their lifecycle state is mirrored into the installed local environment.
- Rollout recovery requires a continuous worker; Windows watches only WSL roots registered by the installer, not arbitrary distributions.
- Modern hooks remain inactive until reviewed by the user.
- Codex rollout JSONL and local SQLite schemas are upstream interfaces that may require future adapters.
- Strict mode can intentionally retain a true completion when required local evidence is missing; `balanced` trades a higher false-positive risk for timed fallback.
- Delivery after idle confirmation remains durable at-least-once rather than transactional exactly-once.

## [2.3.0] - 2026-07-10

Initial public release. Earlier iterations were private and are not supported public versions.

### Added

- Durable per-host disk outbox for Codex turn-completion events.
- Windows PowerShell 5.1 and Python 3.10+ notifier implementations with matching queue schema and delivery behavior.
- Concurrent-session deduplication based on Codex thread and turn IDs.
- Stable ntfy sequence IDs across retries after ambiguous timeouts.
- Exponential backoff with jitter, `Retry-After` support, indefinite transient retry by default, and configurable attempt limits.
- Sent and suppressed receipts, poison-record isolation, dead-letter retention, and bounded operational logging.
- Subagent classification using payload and local rollout metadata, including a short grace period for files still being written.
- Windows Task Scheduler worker, native Linux systemd user worker, and on-demand fallback workers.
- WSL classifier/Windows bridge with native Python fallback.
- Staged local and Remote SSH installers for Windows and Linux, private permissions, timestamped backups, and rollback on installation failure.
- Doctor and real-delivery test commands for both notifier implementations.
- Unit coverage using a local in-process HTTP server, including concurrency, retry, redirect, auth transport, privacy, installer, and poison-queue cases.
- English and Italian setup documentation, architecture, security/privacy, troubleshooting, alternatives, uninstall/rollback, contribution, security-reporting, and release guides.

### Changed

- Fresh installations default to `include_message: false` and `include_thread_title: false`; final assistant content is neither queued nor sent, and prompt-derived thread titles are not used, unless explicitly enabled.
- Fresh installations default to project basename rather than full path, subagent suppression, unlimited transient retry, 14-day receipt retention, and 30-day dead-letter retention.
- Existing configurations are migrated conservatively; an older config without `include_message` or `include_thread_title` retains its previous behavior and should be reviewed after upgrade.
- Notification wording and configuration examples are suitable for a public, host-neutral release.

### Security

- Refuse token/basic credentials over non-HTTPS non-loopback connections unless `allow_insecure_auth` is explicitly enabled.
- Refuse HTTP redirects so authorization is not forwarded to another endpoint.
- Keep topics and credentials outside notifier source files and protect configs, state, staging, and backups with platform-native permissions.
- Copy private configuration to remote hosts through restricted staging paths, verify every staged file by SHA-256 before cutover, and recommend one publish-only token per host.
- Store no Codex user prompt fields and apply best-effort redaction/truncation when final assistant content is explicitly enabled.

### Known limitations

- Delivery is durable at-least-once, not transactional exactly-once.
- The upstream external hook reports turn completion rather than every approval/input event.
- Extremely large Windows hook payloads may fail before the notifier process is launched.
- Subagent classification depends partly on local Codex rollout metadata and fails open after its grace period.

[Unreleased]: https://github.com/ravhello/codex-ntfy-notifier/compare/v2.6.0...HEAD
[2.6.0]: https://github.com/ravhello/codex-ntfy-notifier/compare/v2.5.2...v2.6.0
[2.5.2]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.5.2
[2.5.1]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.5.1
[2.5.0]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.5.0
[2.4.3]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.3
[2.4.2]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.2
[2.4.1]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.1
[2.4.0]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.0
[2.3.0]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.3.0
