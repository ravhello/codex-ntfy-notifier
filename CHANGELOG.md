# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) for public releases.

## [Unreleased]

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

[Unreleased]: https://github.com/ravhello/codex-ntfy-notifier/compare/v2.3.0...HEAD
[2.3.0]: https://github.com/ravhello/codex-ntfy-notifier/releases/tag/v2.3.0
