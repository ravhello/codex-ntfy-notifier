# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) for public releases.

## [Unreleased]

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

[Unreleased]: https://github.com/ravhello/codex-ntfy-notifier/compare/v2.4.3...HEAD
[2.4.3]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.3
[2.4.2]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.2
[2.4.1]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.1
[2.4.0]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.4.0
[2.3.0]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.3.0
