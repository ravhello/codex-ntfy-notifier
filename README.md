# Durable Codex ntfy notifier

[![CI](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/ravhello/codex-ntfy-notifier/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Windows PowerShell 5.1](https://img.shields.io/badge/Windows%20PowerShell-5.1-5391FE.svg)](https://learn.microsoft.com/powershell/)

Reliable ntfy push notifications when an OpenAI Codex turn finishes, including concurrent VS Code windows, WSL, and Remote SSH hosts.

[Italiano](README.it.md) · [Architecture](docs/architecture.md) · [Privacy and security](docs/security-and-privacy.md) · [Troubleshooting](docs/troubleshooting.md)

> [!IMPORTANT]
> This is an unofficial community project. It is not affiliated with or endorsed by OpenAI or ntfy.

## Why this project exists

A one-line `curl` hook works until the network times out, several Codex sessions finish together, or the actual Codex app-server runs inside WSL or on an SSH host. This project treats notification delivery as a small reliability problem:

- the hook writes the event to an atomic on-disk outbox before doing any network work;
- one worker per host retries transient failures with exponential backoff and jitter;
- `thread-id + turn-id` provides deterministic deduplication;
- the same ntfy `sequence_id` is reused after an ambiguous timeout;
- Windows, WSL, and each Remote SSH host use the correct Codex session metadata;
- subagent completions are suppressed by default, including while rollout files are still open for writing;
- malformed and poison queue records cannot stop the rest of the queue;
- user prompts are never persisted, and the final assistant message is excluded by default.

The delivery guarantee is **durable at-least-once**, not transactional exactly-once. See [Architecture](docs/architecture.md) for the exact failure model.

## When to use it

Use this project when your priority is durable phone/desktop push delivery for Codex across machines and offline periods. If you want native desktop sounds, click-to-focus, or one notifier for many different coding agents, projects such as [ai-agent-notifier](https://github.com/DevinoSolutions/ai-agent-notifier), [code-notify](https://github.com/mylee04/code-notify), and [agent-notify](https://github.com/paultendo/agent-notify) may be a better fit. A fuller comparison is in [docs/alternatives.md](docs/alternatives.md).

## Supported environments

| Environment | Hook | Durable worker | Installer |
| --- | --- | --- | --- |
| Windows 10/11 | Windows PowerShell 5.1 | Task Scheduler | `install.ps1` |
| WSL2 | local classifier + Windows bridge, native fallback | Windows worker / Python fallback | `install.ps1` |
| Native Linux | Python 3.10+ | systemd user service or on-demand | `install-linux.sh` |
| Remote SSH, Windows | Windows PowerShell 5.1 | remote Task Scheduler | `install-remote-windows.ps1` |
| Remote SSH, Linux | Python 3.10+ | remote systemd user service or on-demand | `install-remote-linux.sh` |

Codex must support the root-level [`notify` configuration](https://developers.openai.com/codex/config-advanced/#notifications). WSL is optional. Remote installers additionally require OpenSSH client tools and an already working host alias.

## Quick start: Windows and WSL

Clone the repository, open PowerShell in it, and run:

```powershell
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
.\install.ps1 -WslDistro Ubuntu
```

On a fresh installation the topic prompt is hidden. The installer then:

1. creates `~/.codex/ntfy-config.json` with private ACLs;
2. makes a timestamped rollback backup in `~/.codex/ntfy-backups`;
3. installs the durable Windows worker as `CodexNtfyWatcher`;
4. adds the root-level Codex `notify` command without replacing an unrelated hook;
5. installs the WSL classifier, bridge, and native fallback when requested.

For Windows without WSL:

```powershell
.\install.ps1 -NoWsl
```

For unattended setup, provide the topic through the process environment and remove it afterwards:

```powershell
$env:CODEX_NTFY_TOPIC = '<private-topic>'
try { .\install.ps1 -WslDistro Ubuntu } finally { Remove-Item Env:CODEX_NTFY_TOPIC }
```

Reload VS Code windows that were already open so their Codex app-server rereads `config.toml`.

## Quick start: native Linux

```sh
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
CODEX_NTFY_TOPIC=$(python3 -c 'import getpass; print(getpass.getpass("Private ntfy topic: "))')
export CODEX_NTFY_TOPIC
./install-linux.sh
unset CODEX_NTFY_TOPIC
```

Set `CODEX_NTFY_SKIP_SYSTEMD=1` to use only the on-demand worker. The on-demand worker remains alive until its outbox is empty.

## Remote SSH hosts

Run remote installers from a machine where the notifier is already configured. The private ntfy configuration is copied to the remote host and protected with host-native permissions.

Windows remote host, from PowerShell:

```powershell
.\install-remote-windows.ps1 -HostName my-windows-host
```

Linux remote host, from Linux or WSL:

```sh
./install-remote-linux.sh my-linux-host "$HOME/.codex/ntfy-config.json"
```

Each real host owns its own outbox and worker. Use a separate publish-only ntfy token per host when your ntfy server supports access control.

## Configuration

The private configuration lives at `~/.codex/ntfy-config.json`. Start from [ntfy-config.example.json](ntfy-config.example.json) when configuring it manually.

Important defaults:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `include_message` | `false` | Do not persist or send the final assistant message unless explicitly enabled. |
| `include_thread_title` | `false` | Use only the project directory in the notification title unless explicitly enabled. |
| `include_full_path` | `false` | Send only the final project-directory name. |
| `suppress_subagents` | `true` | Suppress delegated/subagent turn completions. |
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
~/.codex/notify-ntfy.ps1 -Doctor
Get-ScheduledTask CodexNtfyWatcher | Select-Object TaskName, State
~/.codex/notify-ntfy.ps1 -Test
```

Linux or WSL:

```sh
python3 ~/.codex/notify-ntfy.py --doctor
systemctl --user status codex-ntfy.service
python3 ~/.codex/notify-ntfy.py --test
```

Runtime state:

```text
~/.codex/ntfy-state/
  outbox/       pending events
  sent/         delivery receipts used for deduplication
  suppressed/   subagent receipts
  dead/         invalid or permanently failed events
  notify.log    bounded operational log
```

Do not delete `outbox/` during an outage. Fix connectivity or credentials and let the worker resume. See [Troubleshooting](docs/troubleshooting.md) for common failure modes and [Uninstall and rollback](docs/uninstall.md) before removing managed files.

## Privacy summary

By default, ntfy receives the project name, source host/origin, a short thread identifier, and a generic completion message. Thread titles are excluded because they may summarize prompt context. Setting `include_thread_title: true` opts into that title; setting `include_message: true` also stores and sends a redacted/truncated final assistant message. Regex redaction is best-effort and cannot identify every secret. User prompts (`input-messages`) are never stored.

Read [docs/security-and-privacy.md](docs/security-and-privacy.md) before enabling message content or copying credentials to remote hosts. Never attach raw config, session, state, backup, or log files to a public issue.

## Known upstream limitations

- The external Codex `notify` hook currently covers turn completion, not every approval/input event. Track [openai/codex#11808](https://github.com/openai/codex/issues/11808).
- Very large notify payloads can prevent Windows from launching a hook before this project receives it. Track [openai/codex#18309](https://github.com/openai/codex/issues/18309).
- Subagent filtering relies on local Codex rollout metadata because the legacy completion payload does not expose session source. Upstream changes may require an adapter update; the worker fails open after a short grace period to avoid losing root notifications.

## Development

The project has no runtime package dependencies. The test suite uses an in-process fake HTTP server and never contacts a real ntfy topic:

```sh
python3 -m unittest discover -s tests -v
```

Windows CI exercises both the Python and Windows PowerShell implementations. Linux CI verifies Python, shell syntax, installers, and version parity. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Do not open a public issue for credential exposure or a possible secret leak.

## License

[MIT](LICENSE) © 2026 Riccardo Ravello and contributors.

## References

- [Codex advanced configuration: notifications](https://developers.openai.com/codex/config-advanced/#notifications)
- [ntfy JSON publishing](https://docs.ntfy.sh/publish/#publish-as-json)
- [ntfy sequence IDs](https://docs.ntfy.sh/publish/#updating-notifications)
