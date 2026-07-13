# Codex ntfy Notifier — idle-aware completion notifications

[![CI](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/ravhello/codex-ntfy-notifier?display_name=tag&sort=semver)](https://github.com/ravhello/codex-ntfy-notifier/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Windows PowerShell 5.1](https://img.shields.io/badge/Windows%20PowerShell-5.1-5391FE.svg)](https://learn.microsoft.com/powershell/)

One compact ntfy push when a local OpenAI Codex root task is verifiably idle—not for intermediate turns. Works with the Codex app, VS Code extension, CLI, WSL, and Remote SSH hosts.

![Codex ntfy Notifier waits for locally verifiable idle before sending one compact completion notification](docs/assets/hero.svg)

[Italiano](README.it.md) · [Architecture](docs/architecture.md) · [Privacy and security](docs/security-and-privacy.md) · [Support](SUPPORT.md) · [Alternatives](docs/alternatives.md)

> [!IMPORTANT]
> This is an unofficial community project. It is not affiliated with or endorsed by OpenAI or ntfy.

## What makes it different

- **Idle-aware:** the root task must be locally verifiable as idle; intermediate turns, active goals, and running subagents keep the notification pending.
- **Durable delivery:** an atomic outbox, stable deduplication, and retry with backoff provide at-least-once delivery after idle confirmation.
- **Multi-environment:** the same classifier supports Codex app, VS Code, CLI, Windows, WSL, native Linux, and host-local Remote SSH installs.
- **Fast isolated recovery:** on Windows, the persistent local scanner follows recent Codex SQLite entries instead of repeatedly walking the full session archive, while UNC/WSL recovery runs separately and cannot block local delivery. Remote SSH installs keep their worker and queue on the remote host.
- **Privacy by default:** prompts and final messages are excluded; task titles, message excerpts, and full paths each require an explicit opt-in.

## Quick start

The setup path is **install → `/hooks` → doctor → test**. Each real Windows, WSL, Linux, or SSH environment has its own `CODEX_HOME` and must be installed there.

Before installing, [subscribe with an ntfy client](https://docs.ntfy.sh/subscribe/phone/) to a hard-to-guess topic or a topic protected by [ntfy access control](https://docs.ntfy.sh/config/#access-control). You also need Git; native Linux requires Python 3.10 or later.

### 1a. Install on Windows and WSL

Open PowerShell:

```powershell
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
.\install.ps1 -WslDistro Ubuntu
```

Use `.\install.ps1 -NoWsl` for Windows only. Reload Codex and any open VS Code windows after installation.

### 1b. Install on native Linux

```sh
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
CODEX_NTFY_TOPIC=$(python3 -c 'import getpass; print(getpass.getpass("Private ntfy topic: "))')
export CODEX_NTFY_TOPIC
./install-linux.sh
unset CODEX_NTFY_TOPIC
```

### 2. Review the hook

In every installed Codex environment, run `/hooks`, inspect the managed `Stop` command, and approve it. The installer never modifies the Codex trust store.

### 3. Run the doctor

Windows:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Doctor
```

Linux or WSL:

```sh
python3 ~/.codex/notify-ntfy.py --doctor
```

### 4. Send one test notification

Windows:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Test
```

Linux or WSL:

```sh
python3 ~/.codex/notify-ntfy.py --test
```

> If this solves a real notification gap in your workflow, consider [starring the repository](https://github.com/ravhello/codex-ntfy-notifier).

## Why this project exists

Codex may emit several turn-completion signals while one task is still progressing: an automatic continuation can start immediately, a goal can remain active, or a delegated subagent can still be working. Publishing every signal produces noisy “finished” notifications that are not actually final.

Version 2.4 introduced the logical **idle epoch** used by 2.4.3:

- the modern Codex `Stop` hook contributes a candidate; it never publishes directly;
- the legacy `agent-turn-complete` notification remains a compatibility signal;
- a continuous rollout watcher can recover a completion missed by either hook when it watches that same `CODEX_HOME`;
- on Windows, the persistent local watcher obtains active and recently resumed rollout paths from Codex's read-only SQLite index and checks only hot current-day paths; its continuous path does not recursively rescan a multi-gigabyte `sessions/` and `archived_sessions/` tree;
- UNC/WSL fallback recovery runs as a separate timeout-bounded scanner, so a suspended distro or slow share cannot delay the local scanner or an already-ready ntfy delivery;
- candidates first enter a private `pending/` area;
- the idle gate confirms the same turn completed, no later turn is open, any goal is no longer active, descendants are no longer running, and the rollout stayed quiet for a short settling window; on Windows, a native streaming lifecycle summary avoids replaying a large rollout line by line in PowerShell;
- pending candidates for one root thread are coalesced, and a completion followed by a later open task is suppressed as an obsolete predecessor; an already promoted outbox epoch remains immutable;
- `strict` mode never fails open: incomplete evidence is retried during `idle_probe_grace_seconds`, then an unverifiable candidate is suppressed locally instead of becoming a false “done” alert.

After the idle gate, the existing durable delivery engine takes over:

- the event moves atomically to a per-host outbox before any network request;
- one worker per host retries transient failures with exponential backoff and jitter;
- `thread-id + turn-id` provides deterministic deduplication;
- the same ntfy `sequence_id` is reused after an ambiguous timeout;
- malformed and poison records cannot stop later events;
- user prompts are never copied into notifier state, and the final assistant message is excluded by default.

The delivery guarantee after idle confirmation is **durable at-least-once**, not transactional exactly-once. See [Architecture](docs/architecture.md) for the complete state machine and failure model.

## Minimal notification title

Since version 2.4.2, the 2.4 idle-only delivery rule uses a title without redundant prefixes:

```text
Visible title: ✅ <conversation-or-project>
Body:  [final message ·] [project ·] origin · #thread8
```

The visible title is exactly one completion/status emoji supplied by ntfy plus the local conversation title, or the project directory when task-title sharing is disabled or unavailable. The JSON `title` contains only that text value. It is resolved by exact thread ID from the read-only Codex state database, then from the local session index as a compatibility fallback. The single default `white_check_mark` tag supplies the emoji; the notifier does not add `Codex`, `done`, a model name, a status label, or another decorative emoji. If `include_thread_title: true` opts into an available local task title and that title differs from the project, the project moves into the body so the location is not duplicated or lost.

With the default `markdown: false`, the body is one line and its context has no labels such as `Project:`, `Source:`, or `Thread:`. With the privacy default `include_message: false`, it contains only the necessary project (when not already in the title), origin, and `#` plus the first eight thread-ID characters. With `include_message: true`, a redacted final-message excerpt is prepended; `max_message_chars` defaults to 180. The complete ntfy `message` is hard-capped at 3,500 UTF-8 bytes regardless of that character setting. An explicit Markdown opt-in can preserve lines in the optional excerpt.

Notification taps do nothing extra by default. Setting `include_task_link: true` adds the authenticated HTTPS task URL `https://chatgpt.com/codex/tasks/<thread-id>` as [ntfy's `click` target](https://docs.ntfy.sh/publish/#click-action) without changing the visible title or body. On a supported phone, that URL can open the task in the ChatGPT mobile app; otherwise it has a browser fallback. The phone must use the same ChatGPT account and workspace, and local tasks require [Remote](https://learn.chatgpt.com/docs/remote-connections) access to the host. The separate `include_task_link_action` option adds one visible [**Open task** `view` action](https://docs.ntfy.sh/publish/#open-websiteapp) and remains off by default.

Fresh installs use one ntfy tag, `white_check_mark`. Apart from the emoji rendered from that tag, the templates add no decorative emoji to title or body. Markdown is off, and default priority 3 is represented by omitting `priority` from the outgoing JSON. Custom non-default priorities are still sent explicitly.

## When to use it

Use this project when your priority is one durable phone/desktop push after a local Codex task has no more work, including concurrent tasks and temporary network outages. An opt-in task link can open that exact task from the notification; if you want native sounds, a notification center, or one notifier for many different coding agents, another project may be a better fit. See [Alternatives and adjacent projects](docs/alternatives.md).

## Supported environments

| Environment | Completion signals | Durable worker | Installer |
| --- | --- | --- | --- |
| Windows 10/11 | modern `Stop` + legacy `notify` + rollout watcher | Task Scheduler | `install.ps1` |
| WSL2 | local signals, Windows bridge, registered rollout root, native fallback | Windows worker / Python fallback | `install.ps1` |
| Native Linux | modern `Stop` + legacy `notify` + rollout watcher | systemd user service or on-demand | `install-linux.sh` |
| Remote SSH, Windows | remote signals and rollout state | remote Task Scheduler | `install-remote-windows.ps1` |
| Remote SSH, Linux | remote signals and rollout state | remote systemd user service or on-demand | `install-remote-linux.sh` |

The same idle semantics apply to local tasks started from the Codex app, VS Code extension, or CLI when that Codex process writes the local hook and rollout state. Each real Windows, WSL, Linux, or SSH environment has its own `CODEX_HOME` and must be installed there.

Pure cloud tasks that never mirror lifecycle state into the local `CODEX_HOME` are not guaranteed. This project does not attach to private UI status streams.

The legacy signal uses Codex [advanced notification configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications). The preferred lifecycle signal uses [Codex Hooks](https://learn.chatgpt.com/docs/hooks).

## Windows and WSL installation details

The Windows/WSL quick start above prompts for the topic with hidden input on a fresh installation. The installer then:

1. creates `~/.codex/ntfy-config.json` with private ACLs;
2. makes a timestamped rollback backup in `~/.codex/ntfy-backups`;
3. removes only local records explicitly created by the notifier's synthetic test command;
4. installs the durable Windows worker as `CodexNtfyWatcher`; Task Scheduler launches its hidden VBS supervisor directly, avoiding two cold PowerShell launcher starts before the notifier can become ready;
5. preserves or installs the root-level legacy `notify` command;
6. registers the managed modern `hooks.Stop` command without replacing unrelated hook handlers;
7. installs the WSL classifier, bridge, and native fallback, then registers that distribution's Codex/SQLite roots with the Windows recovery watcher.

For Windows without WSL:

```powershell
.\install.ps1 -NoWsl
```

For unattended setup, provide the topic through the process environment and remove it afterwards:

```powershell
$env:CODEX_NTFY_TOPIC = '<private-topic>'
try { .\install.ps1 -WslDistro Ubuntu } finally { Remove-Item Env:CODEX_NTFY_TOPIC }
```

### Review the modern hook once

Codex requires newly installed hooks to be reviewed before execution. In every installed Codex environment, use `/hooks` and approve the managed `Stop` hook after inspecting its command. The installer deliberately does **not** modify the Codex trust store.

Until the modern hook is trusted, the legacy notification and continuous rollout watcher remain available as fallbacks. Hook review is still recommended because it provides the earliest explicit stop candidate; the notifier independently classifies its session as root or descendant.

## Native Linux installation details

Set `CODEX_NTFY_SKIP_SYSTEMD=1` to use only the on-demand worker. A continuous worker is recommended because rollout watching is the recovery path for a hook signal that never arrives.

## Remote SSH hosts

Run remote installers from a machine where the notifier is already configured. The private ntfy configuration is copied to the remote host and protected with host-native permissions. Host-local `watch_roots` are cleared during remote installation; register topology on the destination itself instead of copying source-machine paths.

Windows remote host, from PowerShell:

```powershell
.\install-remote-windows.ps1 -HostName my-windows-host
```

Linux remote host, from Linux or WSL:

```sh
./install-remote-linux.sh my-linux-host "$HOME/.codex/ntfy-config.json"
```

Each real host owns its own pending area, rollout cursor, outbox, and worker. Use a separate publish-only ntfy token per host when your ntfy server supports access control. Review the modern hook with `/hooks` on the remote Codex environment too.

## Configuration

The private configuration lives at `~/.codex/ntfy-config.json`. Start from [ntfy-config.example.json](ntfy-config.example.json) when configuring it manually.

### Idle detection

| Setting | Default | Meaning |
| --- | ---: | --- |
| `idle_detection_mode` | `"strict"` | `strict` never turns missing evidence into a notification; `balanced` may fall back after the probe grace period; `off` restores immediate per-turn queueing. |
| `idle_grace_seconds` | `1.5` | Required quiet time after the matching completion before the task is considered idle. |
| `idle_probe_grace_seconds` | `30` | Evidence-probe window. At expiry, `balanced` may accept incomplete evidence; `strict` suppresses an unverifiable candidate locally and never sends it. |
| `unknown_retry_max_seconds` | `60` | Maximum interval between exponential retries while root or rollout evidence remains unknown. |
| `goal_aware` | `true` | Hold a candidate while the root task goal status is `active`. |
| `goal_poll_seconds` | `1` | Poll cadence while goal, turn, or descendant state can still change. |
| `subagent_orphan_seconds` | `1800` | Stop treating a stale child rollout as active after this interval. |
| `suppress_technical_turns` | `true` | Suppress legacy/watcher completions that do not look like a user-facing root turn. Modern `Stop` candidates classified as root are retained. |
| `watch_rollouts` | `true` | Let a continuous worker discover locally persisted completions missed by hooks. |
| `watch_scan_seconds` | `2` | Fast cadence for hot, recently modified rollout files. |
| `watch_discovery_seconds` | `60` | Cadence for refreshing historical cursors and bounded old-date/archive discovery. Unchanged cursor files are not rewritten. |
| `watch_cursor_batch_size` | `64` | Windows-only number of historical rollout files probed per cold cycle; cursor metadata still prevents old-history replay. |
| `watch_remote_timeout_seconds` | `90` | Windows-only limit for one isolated UNC/WSL fallback scan; remote stalls never block local recovery or delivery. |
| `watch_initial_replay_seconds` | `15` | On first sight, replay only a very recent rollout tail instead of old history. |
| `watch_roots` | `[]` | Additional Codex roots watched by the Windows worker. `install.ps1` manages entries for selected WSL distributions, including their SQLite root and source label. |
| `worker_sqlite_path` | installer-managed | Host-local SQLite root used by the Windows scheduled watcher when it differs from `CODEX_HOME`; remote installers reset it for the destination. |

Leave `strict` enabled when “no intermediate notifications” is more important than receiving a notification despite missing local evidence. It retries unknown evidence with bounded exponential intervals, then records `unverifiable` locally after the probe window; it never promotes that uncertainty to ntfy. `balanced` is an explicit availability/noise tradeoff. `off` is primarily a compatibility and diagnostic mode.

### Delivery and privacy

| Setting | Default | Meaning |
| --- | ---: | --- |
| `include_message` | `false` | Do not persist or send the final assistant message unless explicitly enabled. |
| `max_message_chars` | `180` | Maximum character count for the optional final-message excerpt; the complete body also has a 3,500-byte UTF-8 hard cap. |
| `include_thread_title` | `false` | Use only the project directory in the notification title unless explicitly enabled. |
| `include_task_link` | `false` | Add an HTTPS `click` target for the exact task. This sends the full thread ID to ntfy. |
| `include_task_link_action` | `false` | Also show one **Open task** `view` action. It has no effect unless `include_task_link` is enabled. |
| `include_full_path` | `false` | Do not add the sanitized full working-directory path to the body. |
| `tags` | `["white_check_mark"]` | Use one default ntfy tag instead of duplicating an emoji in text. |
| `priority` | `3` | Use ntfy's default priority; the field is omitted from outgoing JSON when it is 3. |
| `markdown` | `false` | Send the compact body as plain text. |
| `suppress_subagents` | `true` | Never send a descendant/subagent completion as its own notification. |
| `subagent_classification_grace_seconds` | `8` | Classification retry window used outside strict root evidence. |
| `max_attempts` | `0` | Retry transient delivery failures indefinitely. |
| `sent_retention_days` | `14` | Retain deduplication receipts. |
| `dead_retention_days` | `30` | Retain sanitized dead-letter records. |
| `allow_insecure_auth` | `false` | Refuse credentials over non-HTTPS non-loopback servers. |

Environment variables override server and authentication values:

- `CODEX_NTFY_SERVER`
- `CODEX_NTFY_TOPIC`
- `CODEX_NTFY_TOKEN`
- `CODEX_NTFY_USER`
- `CODEX_NTFY_PASSWORD`

The notifier refuses HTTP redirects, preventing credentials from being forwarded to a different endpoint.

## Diagnostics

Windows:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Doctor
Get-ScheduledTask CodexNtfyWatcher | Select-Object TaskName, State
& "$HOME\.codex\notify-ntfy.ps1" -Test
```

Linux or WSL:

```sh
python3 ~/.codex/notify-ntfy.py --doctor
systemctl --user status codex-ntfy.service
python3 ~/.codex/notify-ntfy.py --test
```

`pending_idle` is the count still waiting for logical-idle evidence; `queued` is the network-ready outbox count; `watched_rollouts` confirms that the recovery watcher has cursor state.

Runtime state:

```text
~/.codex/ntfy-state/
  pending/      root completion candidates awaiting the idle gate
  outbox/       idle-confirmed events awaiting ntfy delivery
  watch/        incremental rollout cursors for missed-hook recovery
  sent/         delivery receipts used for deduplication
  suppressed/   subagent, technical, and superseded receipts
  dead/         invalid or permanently failed records
  notify.log    bounded operational log
```

Do not delete `pending/` or `outbox/` during an outage. Diagnose why a record is waiting, fix the worker, connectivity, or credentials, and let processing resume. See [Troubleshooting](docs/troubleshooting.md).

## Privacy summary

By default, the ntfy title contains only the project name; the single configured tag renders one completion emoji. The one-line body contains the source host/origin plus a short thread identifier. Thread titles are excluded because they may summarize prompt context. Setting `include_thread_title: true` replaces the project title with that local task title; setting `include_message: true` also stores and sends a redacted/truncated final assistant message. `include_full_path: true` is a separate opt-in that can expose the sanitized working-directory path. `include_task_link: true` sends the full thread ID inside a ChatGPT HTTPS URL; it does not bypass ChatGPT authentication. The notifier uses the HTTPS mobile/web fallback instead of the [`codex://` desktop compatibility scheme](https://learn.chatgpt.com/docs/reference/commands#deep-links).

`include_message` is checked again when an outbox record is sent. Turning it off prevents final-message content in already queued records from leaving the host, but it does not erase the local record, backups, dead letters, a request already in flight, or a notification already accepted by ntfy.

Idle detection reads local Codex lifecycle metadata and read-only SQLite status fields. It queries goal **status**, not the goal objective. The rollout watcher persists path, offset, timestamps, and thread identity—not user prompt bodies. The notifier still needs local read access to Codex rollout files to identify lifecycle markers.

Read [Security and privacy](docs/security-and-privacy.md) before enabling message content or copying credentials to remote hosts. Never attach raw config, rollout, database, state, backup, or log files to a public issue.

## Known limitations

- Modern hooks require explicit user review through `/hooks`. The installer never edits the trust store.
- `strict` mode suppresses a candidate locally as `unverifiable` when matching rollout, root classification, or completion evidence is still missing after `idle_probe_grace_seconds`. This avoids a false final notification but can withhold a true one after an upstream format or storage change.
- `balanced` can notify after `idle_probe_grace_seconds` when evidence stays incomplete, so it has a higher false-positive risk.
- Rollout and local Codex database schemas are upstream implementation details and may require adapter updates.
- A stale child is ignored after `subagent_orphan_seconds` so an abandoned rollout cannot block forever.
- Pure cloud tasks are not guaranteed unless their lifecycle state is mirrored into the local environment being watched.
- Autonomous rollout recovery requires a continuous worker. The Windows installer registers only the WSL distributions passed through `-WslDistro`; other distributions are not crawled implicitly.
- Extremely large legacy notify payloads may fail before the notifier process starts; the rollout watcher can recover only if the local rollout contains the completion.
- Delivery is at-least-once after idle confirmation, not exactly-once.

## Development

The project has no runtime package dependencies. The test suite uses an in-process fake HTTP server and never contacts a real ntfy topic:

```sh
python3 -m unittest discover -s tests -v
```

Windows CI exercises both Python and Windows PowerShell. Linux CI verifies Python, shell syntax, installers, and version parity. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Do not open a public issue for credential exposure or a possible secret leak.

## License

[MIT](LICENSE) © 2026 Riccardo Ravello and contributors.

## References

- [Codex Hooks](https://learn.chatgpt.com/docs/hooks)
- [Codex advanced configuration: notifications](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications)
- [ntfy JSON publishing](https://docs.ntfy.sh/publish/#publish-as-json)
- [ntfy sequence IDs](https://docs.ntfy.sh/publish/#updating-notifications)
