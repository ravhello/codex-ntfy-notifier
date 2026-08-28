# Codex ntfy Notifier — idle-aware completion notifications

[![CI](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/ravhello/codex-ntfy-notifier?display_name=tag&sort=semver)](https://github.com/ravhello/codex-ntfy-notifier/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Windows PowerShell 5.1](https://img.shields.io/badge/Windows%20PowerShell-5.1-5391FE.svg)](https://learn.microsoft.com/powershell/)

Final-only ntfy notifications for supported local coding chats, with durable host-local delivery across Windows, WSL, Linux, and Remote SSH installs. WSL normally bridges into the Windows queue and keeps separate native fallback state. OpenAI Codex works in the Codex app, VS Code, and CLI with optional ChatGPT task navigation; opt-in Windows adapters add Claude Code (including Claude Desktop's Code tab) and AudnCode. Ordinary ChatGPT chats without local Codex lifecycle state remain outside the observation boundary.

![Codex ntfy Notifier waits for locally verifiable idle before sending one compact completion notification](docs/assets/hero.svg)

[Italiano](README.it.md) · [Architecture](docs/architecture.md) · [Privacy and security](docs/security-and-privacy.md) · [Support](SUPPORT.md) · [Alternatives](docs/alternatives.md)

> [!IMPORTANT]
> This is an unofficial community project. It is not affiliated with or endorsed by OpenAI, Anthropic, AudnCode, or ntfy.

## What makes it different

- **Idle-aware:** the root task must be locally verifiable as idle; intermediate turns, active goals, and running subagents keep the notification pending.
- **Durable delivery:** an atomic outbox, stable deduplication, and retry with backoff provide at-least-once delivery after idle confirmation.
- **Multi-environment:** Codex app, VS Code, CLI, Windows, WSL, native Linux, host-local Remote SSH installs, and opt-in Claude Code or AudnCode on Windows use the same durable delivery design while retaining per-host and per-session isolation.
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

To also connect Claude Code on Windows (Claude Desktop Code tab, CLI, and VS Code), add the explicit opt-in:

```powershell
.\install.ps1 -WslDistro Ubuntu -EnableClaudeCode
```

The installer atomically merges ordered synchronous main-agent `Stop`/`StopFailure` and `UserPromptSubmit`, plus optional asynchronous `Notification` accelerators (`idle_prompt` and `agent_completed`), into `~/.claude/settings.json`. It preserves unrelated Claude hooks and backs the original file up with the Codex installation snapshot. Claude Code 2.1.198 or newer is required for the complete managed lifecycle set. The installer checks the newest executable found for each detected surface separately—`PATH`, Claude Desktop, VS Code, VS Code Insiders, and Cursor—and stops if any detected surface is older than that minimum.

To connect AudnCode on Windows, use its separate opt-in:

```powershell
.\install.ps1 -WslDistro Ubuntu -EnableAudnCode
```

Both adapters can be enabled in one installation:

```powershell
.\install.ps1 -WslDistro Ubuntu -EnableClaudeCode -EnableAudnCode
```

When `CLAUDE_CONFIG_DIR` is nonblank, the AudnCode installer follows that directory by default; an explicit `-AudnCodeHome` always wins. If separate AudnCode profiles or launchers use different configuration homes, run the installer once for each home with its matching environment or explicit path. They can share the same `-CodexHome` and durable worker while keeping hooks, markers, sessions, and runtime correlation isolated per AudnCode home.

The AudnCode installer reads `<AudnCodeHome>/settings.json` (normally `~/.openclaude/settings.json`), preserves unrelated entries, and commits an atomic replacement with hook shape 8: seven synchronous events—`SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`, `Notification`, `PostToolUse`, and `SubagentStart`. `SessionStart` matches only `^(startup|resume|clear)$`, `Notification` only `idle_prompt`, and `PostToolUse` exactly `Agent|Bash|PowerShell|Monitor|TaskStop|KillShell|CronCreate|CronDelete|SendMessage`; every handler has a 60-second hook timeout. Each managed command carries its installer-controlled expected event, which must match the untrusted payload event exactly. Busy/lifecycle ingress is armed before standard input is read; redirected input is strict UTF-8 and capped at 8 MiB. Oversized, malformed, mismatched, timed-out, or interrupted ingress fails closed instead of releasing pending work. AudnCode does not expose a shared lock for this settings file, so close running AudnCode processes before a first install or a hook-shape upgrade. The installer backs up the original files, sets `messageIdleNotifThresholdMs` to 1,000 ms, writes `<AudnCodeHome>/.codex-ntfy-hooks.json`, and removes only verified orphaned notifier hook processes from older shapes. Reopen AudnCode when the marker is created or rotated; an identical reinstall preserves its generation. Hooks run only in workspaces AudnCode already trusts.

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

For Claude Code, `/hooks` is a read-only configuration browser: verify four managed event types and five handlers (`Notification` has separate `idle_prompt` and `agent_completed` handlers). Claude normally reloads `settings.json` automatically.

For AudnCode, verify all seven events listed above in its hook view or the selected `<AudnCodeHome>/settings.json`. Every managed handler is synchronous with `timeout: 60`; confirm the exact `SessionStart`, `Notification`, and `PostToolUse` matchers and the trusted expected-event argument. Also confirm that the workspace is trusted. Hot reload applies unchanged-shape updates, but a created or rotated observation marker requires the one-time restart printed by the installer.

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

Version 2.4 introduced the logical **idle epoch** retained by 2.5 and 2.6:

- the modern Codex `Stop` hook contributes a candidate; it never publishes directly;
- the legacy `agent-turn-complete` notification remains a compatibility signal;
- a continuous rollout watcher can recover a completion missed by either hook when it watches that same `CODEX_HOME`;
- on Windows, the persistent local watcher obtains active and recently resumed rollout paths from Codex's read-only SQLite index and checks only hot current-day paths; its continuous path does not recursively rescan a multi-gigabyte `sessions/` and `archived_sessions/` tree;
- UNC/WSL fallback recovery runs as a separate timeout-bounded scanner, so a suspended distro or slow share cannot delay the local scanner or an already-ready ntfy delivery;
- candidates first enter a private `pending/` area;
- the idle gate confirms the same turn completed, no later turn is open, any goal is no longer active, descendants are no longer running, and the rollout stayed quiet for a short settling window; on Windows, a structural native parser returns a fixed-size lifecycle summary instead of replaying a large rollout line by line in PowerShell;
- pending candidates for one root thread are coalesced, and a completion followed by a later open task is suppressed as an obsolete predecessor; an already promoted outbox epoch remains immutable;
- `strict` mode never fails open: unknown or unavailable evidence is retried during `idle_probe_grace_seconds`, then an unverifiable candidate is suppressed locally instead of becoming a false “done” alert.

Version 2.5 adds a provider-specific Claude path on Windows. Claude `Stop` is accepted only for the main agent when both authoritative work registries are present and empty; `session_id + prompt_id` provides stable deduplication, and `StopFailure` covers turns ended by an API error. `Stop`, `StopFailure`, and `UserPromptSubmit` are ordered synchronously so repeated same-prompt goal stops cannot finish out of order; their initial reverse scan is capped at 1 MiB and any full reconciliation runs in the worker. `UserPromptSubmit` snapshots the previous session-level goal marker and cancels stale candidates before a new prompt can finish. The gate then mirrors Claude's own resume rule from the newest local `attachment.goal_status`: active/not-met markers hold the candidate, a newer achieved/failed marker releases it, and a newer manual-clear sentinel discards it without a notification. `idle_prompt`/`agent_completed` with the same non-empty `prompt_id` are optional asynchronous fallbacks, never required for correctness, so delayed or uncorrelated VS Code idle events cannot release the wrong candidate. The notification body still uses Claude's supplied final message.

Version 2.6 adds a separate AudnCode path on Windows. A normal `Stop` is only a candidate and needs a matching later `Notification: idle_prompt`; `StopFailure` is terminal only after one matching current-prompt transcript error is proven. That proof bypasses only `idle_prompt`: every other gate remains mandatory. `UserPromptSubmit` pre-arms ordinary prompts, while `SessionStart` supplies the guarded root epoch for direct initial-plan and complex-content paths that AudnCode 0.9.x can start without `UserPromptSubmit`. `SubagentStart` distinguishes a fresh synchronous child from a resumed local-agent incarnation. Process-start ordering prevents delayed events from an older prompt from releasing newer work, and `agent_completed` is never accepted as finality.

AudnCode finality is bound to the live host runtime by AudnCode home, PID, and `startedAt`, so `/clear` and `/resume` cannot erase outstanding work. If two live windows expose the same session UUID, output from the old process is never attributed to the new owner: that exact old lifetime remains a hard gate until its ordered `Stop` then `idle_prompt` **and** its host-specific background, cron, and lifecycle guards are all proven clear; only then is that lifetime retired. Parent-process exit alone does not prune a superseded lifetime: detached background work and every host-specific guard are revalidated before removal. The synchronous `PostToolUse` hook records background launches and exact terminal evidence; `Monitor` is always a launch. A successful or malformed `SendMessage` is conservatively sticky because the public build exposes neither the resolved recipient ID nor durable proof that its RAM-only queue was consumed; only an authoritative `success: false` proves no work was queued. Resumed same-ID local agents are counted as separate incarnations, so one delayed terminal cannot close a newer overlap. The gate also requires an empty persisted command queue, completed task lists, no unresolved Ctrl+B sidechain, and no retained non-lead team directory: `isActive: false` or removing a member is not terminal proof; only AudnCode's `TeamDelete` removal releases that team. After at least 1.25 seconds of settling, the entire gate is evaluated three times before the locked outbox commit.

Evidence reads are bounded as an operational safety limit. In 2.6.0, AudnCode queue reconciliation accepts at most 512 MiB across the runtime lineage, 4,096 relevant records, 1,048,576-character lines, and 65,536-character queue content fields; team/task JSON is capped at 1 MiB per file, and team membership at 1,024 entries. Exceeding a limit is unknown evidence and fails closed—it is not interpreted as idle.

Background and cron lifecycle events are durably pre-armed before slower correlation. Overlapping hooks receive independent bounded guard tokens, so one successful mutation cannot clear another in flight. If a synchronous hook is killed, reaches its 60-second timeout, or cannot commit its exact mutation, a later terminal-looking event cannot erase the lost-history uncertainty. Live or candidate-owned runtime/guard files are protected from ordinary age cleanup; the installer separately terminates only command-line-verified orphan hook processes left by obsolete shapes.

Cron finality uses a separate host registry plus AudnCode's native scheduler lease. `CronCreate` records the observed incarnation. A trusted `CronDelete` closes only the exact session-only incarnation created by the same runtime and host; an uncorrelated delete remains fail closed. For durable work, `CronDelete` is diagnostic because it does not prove that the native scheduler stopped owning or running that incarnation, and an empty `.claude/scheduled_tasks.json` is not standalone completion proof. Durable promotion requires causally ordered, stable file snapshots together with the exact `scheduled_tasks.lock` owner/lifetime boundary; ID reuse, a late or foreign lease, a live owner, missing/malformed evidence, or an empty file written after the relevant owner began remains fail closed. Session-only uncertainty without that exact delete ends only with its host lifetime; this also lets an exited pre-install host stop blocking after restart, while its durable files and every host guard are still checked.

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

The visible title is exactly one completion/status emoji supplied by ntfy plus the local conversation title, or the project directory when title sharing is disabled or unavailable. The JSON `title` contains only that text value. Codex titles come from bounded read-only database/index lookups; Claude Code and AudnCode titles come from bounded `ai-title`/`custom-title` metadata. Display text is NFC-normalized, strips unsafe control/bidi formatting, and is capped at 60 complete clusters and 240 UTF-8 bytes; invalid scalar text falls back instead of emitting replacement characters. The single tag supplies the emoji; the notifier does not add a provider name, `done`, a model name, a status label, or another decorative emoji.

With the default `markdown: false`, the body is one line and its context has no labels such as `Project:`, `Source:`, or `Thread:`. With the privacy default `include_message: false`, it contains only the necessary project (when not already in the title), origin, and `#` plus the first eight thread-ID characters. With `include_message: true`, a redacted final-message excerpt is prepended; presentational Markdown is reduced to compact plain text while link labels and table-cell text remain, and `max_message_chars` defaults to 180. The complete ntfy `message` is hard-capped at 3,500 UTF-8 bytes regardless of that character setting. An explicit `markdown: true` opt-in preserves Markdown and message lines in the optional excerpt.

Notification taps do nothing extra by default. For Codex only, setting `include_task_link: true` adds the authenticated HTTPS task URL `https://chatgpt.com/codex/tasks/<thread-id>` as [ntfy's `click` target](https://docs.ntfy.sh/publish/#click-action). Claude Code and AudnCode notifications deliberately omit that ChatGPT URL because it cannot identify those local sessions.

Every outgoing payload has exactly one ntfy tag. Success uses the first valid configured tag or `white_check_mark`; any terminal non-success uses `warning`. Older comma-separated or array configurations with several tags are accepted but normalized to the first valid member and logged generically. Apart from the one emoji rendered by ntfy, the templates add no decorative emoji. Markdown is off, and default priority 3 is represented by omitting `priority`.

## When to use it

Use this project when your priority is one durable phone/desktop push after a local Codex, Windows Claude Code, or Windows AudnCode task has no more work, including concurrent sessions and temporary network outages. See [Alternatives and adjacent projects](docs/alternatives.md) for different transports and agents.

## Supported environments

| Environment | Completion signals | Durable worker | Installer |
| --- | --- | --- | --- |
| Windows 10/11 | modern `Stop` + legacy `notify` + rollout watcher | Task Scheduler | `install.ps1` |
| Claude Code on Windows | main-agent `Stop`/`StopFailure`, ordered prompt start, transcript goal gate; active work and `/goal` loops fail closed | same Windows worker | `install.ps1 -EnableClaudeCode` |
| AudnCode on Windows | seven ordered synchronous lifecycle hooks plus guarded background, queue, team/task, CCR, cron-lease, and Ctrl+B sidechain gates | same Windows worker | `install.ps1 -EnableAudnCode` |
| WSL2 | local signals, Windows bridge, registered rollout root, native fallback | Windows worker / Python fallback | `install.ps1` |
| Native Linux | modern `Stop` + legacy `notify` + rollout watcher | systemd user service or on-demand | `install-linux.sh` |
| Remote SSH, Windows | remote signals and rollout state | remote Task Scheduler | `install-remote-windows.ps1` |
| Remote SSH, Linux | remote signals and rollout state | remote systemd user service or on-demand | `install-remote-linux.sh` |

The same idle semantics apply to local tasks started from the Codex app, VS Code extension, or CLI when that Codex process writes the local hook and rollout state. Each real Windows, WSL, Linux, or SSH environment has its own `CODEX_HOME` and must be installed there.

Pure cloud tasks that never mirror lifecycle state into the local `CODEX_HOME` are not guaranteed. This project does not attach to private UI status streams.

The legacy signal uses Codex [advanced notification configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications). The preferred lifecycle signal uses [Codex Hooks](https://learn.chatgpt.com/docs/hooks). Claude hook fields and ordering follow the official [Claude Code hooks reference](https://code.claude.com/docs/en/hooks). The local `attachment.goal_status` record is a Claude transcript implementation detail used defensively for `/goal` continuity; it is not advertised as a hook payload field.

## Windows and WSL installation details

The Windows/WSL quick start above prompts for the topic with hidden input on a fresh installation. The installer then:

1. creates `~/.codex/ntfy-config.json` with private ACLs;
2. makes a timestamped rollback backup in `~/.codex/ntfy-backups`;
3. removes only local records explicitly created by the notifier's synthetic test command;
4. installs the durable Windows worker as `CodexNtfyWatcher`; Task Scheduler launches its hidden VBS supervisor directly, avoiding two cold PowerShell launcher starts before the notifier can become ready;
5. preserves or installs the root-level legacy `notify` command;
6. registers the managed modern `hooks.Stop` command without replacing unrelated hook handlers;
7. when `-EnableClaudeCode` is present, atomically merges ordered synchronous `Stop`/`StopFailure`/`UserPromptSubmit` and optional asynchronous `Notification` handlers into the user Claude settings and includes that file in rollback;
8. when `-EnableAudnCode` is present, preserves unrelated entries and atomically installs hook shape 8 with seven synchronous 60-second handlers (`SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`, `Notification`, `PostToolUse`, and `SubagentStart`), exact matchers, and a trusted expected-event argument; it sets the 1,000 ms idle threshold, manages the private observation marker, and removes only verified orphaned old-shape hook processes; a machine-global transaction lock serializes every installer touching the shared scheduled task, then a per-home lock protects AudnCode marker changes and compare-and-swap rollback;
9. installs the WSL classifier, bridge, and native fallback, then registers that distribution's Codex/SQLite roots with the Windows recovery watcher.

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
| `idle_probe_grace_seconds` | `30` | Evidence-probe window. At expiry, `balanced` may accept unknown or unavailable evidence; malformed UTF-8/lifecycle data and a partial trailing JSONL record always remain fail-closed. `strict` suppresses an unverifiable candidate locally and never sends it. |
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
| `tags` | `["white_check_mark"]` | Select one success tag. Multiple legacy values normalize to the first valid member; empty/invalid input falls back to `white_check_mark`, while any terminal non-success uses `warning`. |
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

Idle detection reads local Codex lifecycle metadata and read-only SQLite status fields. It queries goal **status**, not the goal objective. The rollout watcher persists path, offset, timestamps, and thread identity—not user prompt bodies. The notifier still needs local read access to Codex rollout files to identify lifecycle markers. With Claude enabled, a memory-bounded reverse scan finds only the newest relevant `goal_status` lifecycle attachment and stores its state plus opaque marker; it does not extract, store, log, or send the goal condition/reason. With AudnCode enabled, the notifier validates the session UUID, transcript location below the configured AudnCode `projects` directory, prompt epoch, host PID/start identity, hook ordering, background IDs, queue operations, task/team state, terminal sidechain evidence, and—only for `StopFailure`—one matching error record after the prompt cursor. Hook JSON is decoded as strict UTF-8 and capped at 8 MiB, so malformed or oversized input is rejected instead of silently producing corrupted characters or unbounded memory use.

Read [Security and privacy](docs/security-and-privacy.md) before enabling message content or copying credentials to remote hosts. Never attach raw config, rollout, database, state, backup, or log files to a public issue.

## Known limitations

- Modern hooks require explicit user review through `/hooks`. The installer never edits the trust store.
- Claude support currently targets local Claude Code on Windows. The ordinary Claude Chat tab does not expose Claude Code hooks, user interrupts do not emit `Stop`, and hosted work without a local hook is outside the observation boundary.
- Claude `/goal` finality relies on a memory-bounded reverse scan of Claude's local transcript `attachment.goal_status` records without loading the full transcript. That is an upstream local format and may require an adapter update if Claude changes it; missing or malformed active-goal evidence fails closed instead of sending an intermediate alert.
- AudnCode support targets the public local AudnCode 0.9.x build on Windows and depends on upstream hook payloads, process/session markers, transcript/queue/error records, team/task directories, CCR sidecars, scheduler lease/files, and sidechain naming. Hooks run only in trusted workspaces. Missing, mismatched, recursive, oversized, ambiguous, or out-of-order evidence fails closed.
- The public build does not expose durable consumption for successful `SendMessage`, a definitive end for overlapping same-ID resumed local agents, or every ID removed by `clearCommandQueue`, kill-all, or some Ctrl+B paths. These conditions can withhold a true final notification even after host exit; the notifier does not convert them into an intermediate alert. Fresh asynchronous agents without those ambiguity sources can release on counted exact terminal evidence.
- `/ultrareview` starts a CCR/remote task before the prompt hook and may persist its identity sidecar later. The pre-armed claim stays busy until exact matching remote terminal proof; sidecar deletion, a later prompt, or a different task's terminal is not enough.
- `CronDelete` is terminal only for an exact correlated session-only incarnation from the same runtime and host. For durable jobs it remains an observation, and an empty `scheduled_tasks.json` is not completion proof; durable finality depends on causally ordered file snapshots and the exact native scheduler lease owner. Missing, malformed, reused, late, or foreign evidence remains fail closed.
- `strict` mode suppresses a candidate locally as `unverifiable` when matching rollout, root classification, or completion evidence is still missing after `idle_probe_grace_seconds`. This avoids a false final notification but can withhold a true one after an upstream format or storage change.
- `balanced` can notify after `idle_probe_grace_seconds` when otherwise valid evidence stays unknown or unavailable, so it has a higher false-positive risk. It never promotes malformed UTF-8/lifecycle data or a partial trailing JSONL record.
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
