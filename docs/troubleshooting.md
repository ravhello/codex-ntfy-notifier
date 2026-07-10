# Troubleshooting

Start with the environment in which Codex is actually running. A local VS Code window, a WSL window, and a Remote SSH window do not necessarily read the same `~/.codex`, run the same hook, or use the same queue.

Do not post private config, queue records, dead letters, Codex session files, or raw logs. See [Security and privacy](security-and-privacy.md) before sharing diagnostics.

## Quick health check

Windows PowerShell:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Doctor
Get-ScheduledTask -TaskName CodexNtfyWatcher | Select-Object TaskName, State
Get-Content "$HOME\.codex\ntfy-state\notify.log" -Tail 40
```

Linux or WSL:

```sh
python3 "$HOME/.codex/notify-ntfy.py" --doctor
systemctl --user status codex-ntfy.service --no-pager
tail -n 40 "$HOME/.codex/ntfy-state/notify.log"
```

The doctor output does not print the topic or credentials. Check:

- `version` is the expected release;
- `topic_configured` is `true`;
- `auth_mode` matches the intended anonymous, token, or basic-auth setup;
- `queued` is not growing indefinitely;
- `dead_letters` is not increasing after each test.

The explicit test sends a real notification:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Test
```

```sh
python3 "$HOME/.codex/notify-ntfy.py" --test
```

Use `-Test`/`--test` only when publishing to the configured topic is acceptable.

## No event is queued

If a Codex turn finishes but the outbox, sent receipt count, and log do not change, the hook probably did not run.

### Reload the Codex process

`config.toml` is read by the Codex app-server. Reload every VS Code window that was open during installation. For Remote SSH and WSL, reload the remote window, not only a local window.

Verify that the relevant environment has one root-level managed `notify` entry:

```powershell
Select-String -Path "$HOME\.codex\config.toml" -Pattern '^\s*notify\s*='
```

```sh
grep -nE '^[[:space:]]*notify[[:space:]]*=' "$HOME/.codex/config.toml"
```

The line must appear before any TOML table header such as `[features]`; a `notify` key inside a table is not the root-level Codex setting.

### Confirm the real Codex home

Check `CODEX_HOME` in the process environment. When set, it can move config and state away from `~/.codex`. Remote SSH uses the remote account's home. WSL uses the selected distribution's Linux home for its hook, even when delivery is bridged to Windows.

### Check the upstream hook boundary

The external hook currently covers turn completion, not every approval or input request. See [openai/codex#11808](https://github.com/openai/codex/issues/11808).

On Windows, an exceptionally large notify payload may exceed process-launch limits before this notifier receives it. In that case there is no queue or log entry to recover. See [openai/codex#18309](https://github.com/openai/codex/issues/18309).

## Events queue but do not send

A nonzero `queued` count means persistence worked and the delivery side needs attention. Do not delete `outbox/` during a normal outage.

### Worker is stopped

Windows:

```powershell
Start-ScheduledTask -TaskName CodexNtfyWatcher
Get-ScheduledTask -TaskName CodexNtfyWatcher | Select-Object State
```

Linux:

```sh
systemctl --user daemon-reload
systemctl --user restart codex-ntfy.service
systemctl --user is-active codex-ntfy.service
```

When the scheduled task or systemd installation was intentionally skipped, each hook starts an on-demand worker. It remains alive while the queue is nonempty. Make sure `CODEX_NTFY_NO_SPAWN=1` was not left in the Codex environment; that variable is intended for testing.

Only one worker per state directory acquires `worker.lock`. A second worker exiting immediately is expected, not a failure.

### Authentication or authorization fails

HTTP 401 and 403 are retryable by design because credentials or server policy can be repaired. With the default `max_attempts: 0`, they remain in the outbox indefinitely. Correct the token/user/password and restart or wait for the worker; do not re-run Codex to create replacement events.

Check configuration syntax without printing its contents:

```powershell
Get-Content "$HOME\.codex\ntfy-config.json" -Raw | ConvertFrom-Json | Out-Null
```

```sh
python3 -m json.tool "$HOME/.codex/ntfy-config.json" >/dev/null
```

If environment overrides are used, inspect them in the worker's environment, not only in the current shell. A systemd service does not automatically inherit variables exported after it started.

### Insecure-auth refusal

The log message `refusing to send ntfy credentials over an insecure connection` means token/basic credentials are configured with non-HTTPS, non-loopback HTTP. Configure TLS or a trusted local proxy. Set `allow_insecure_auth: true` only for a separately protected transport whose risk is understood.

### Redirect rejected

Redirects are deliberately not followed. Configure `server` as the final publishing endpoint. This avoids forwarding an authorization header to another location. A 3xx response is a permanent failure and moves the event to `dead/`.

### Network, TLS, rate limit, or server failure

DNS failures, connection failures, timeouts, TLS errors, and 5xx responses retry with exponential backoff. HTTP 429 and a numeric `Retry-After` are also retried. Test the same server from the same host and user context; a successful browser request on the local machine does not prove that a remote worker can reach it.

Check system time as well. Large clock corrections can make persisted `next_attempt_unix_ms` appear unexpectedly early or late.

## Dead letters

`dead/` contains malformed queue files, redirect responses, most non-retryable 4xx failures, and events that reached a positive `max_attempts` limit. The default retention is 30 days.

First fix the underlying cause. Do not blindly move a malformed record back to the outbox; it will be rejected again. A complete valid record may be replayed manually after review, but it can contain sensitive content and may already have reached ntfy after an ambiguous failure. Stop the worker, retain a private backup, move only the reviewed file back to `outbox/`, then restart the worker. Expect at-least-once rather than exactly-once behavior.

If the event is no longer wanted, delete only that dead-letter file. Deleting a dead letter does not remove any server-side notification.

## Duplicate notifications

Local deduplication requires both Codex `thread-id` and `turn-id`. Doctor output does not expose individual records; inspect a private outbox/dead record locally and check `weak_identity` only if necessary. Missing IDs produce a random identity and cannot be deterministically deduplicated.

A crash or timeout after ntfy accepts the request but before a local receipt is written causes a retry with the same `sequence_id`. Client/server sequence handling normally reduces duplicate presentation, but exactly-once display is not guaranteed.

Queues are per host. If the same logical action is independently reported by two real hosts or two separately configured `CODEX_NTFY_STATE_DIR` values, their receipt stores do not coordinate.

## Unexpected subagent notifications

Keep `suppress_subagents: true` and confirm that the notifier can read the matching Codex session under the event's real `CODEX_HOME`. Classification waits up to `subagent_classification_grace_seconds` (8 seconds by default) for rollout metadata.

After the grace period, an unknown session is intentionally delivered to avoid dropping a root notification. An upstream metadata change can therefore cause extra notifications. A known subagent completion increments the `suppressed` receipt count instead.

For a private diagnostic, pass one captured hook payload through classification without queueing or networking:

```sh
python3 "$HOME/.codex/notify-ntfy.py" --classify --read-stdin < private-payload.json
```

The payload is sensitive and must not be attached to a public issue.

## WSL-specific checks

The WSL hook normally follows this path:

```text
WSL classifier -> powershell.exe bridge -> Windows outbox/worker
```

It falls back to the WSL Python outbox when Windows interop is unavailable or the bridge fails.

Every later bridge invocation checks that native outbox and starts its on-demand worker when pending records exist. This lets an event queued during an interop outage resume even after the original WSL process exited.

Inside the affected distribution, check:

```sh
test -x "$HOME/.codex/notify-ntfy-wsl.sh"
test -x "$HOME/.codex/notify-ntfy.py"
command -v python3
command -v powershell.exe || true
printf '%s\n' "$WSL_DISTRO_NAME"
```

Then run the Windows and WSL doctor commands separately. If the Windows doctor shows new receipts, the bridge is working. If only the WSL state changes, delivery is using the native fallback.

Run `install.ps1` with the exact distribution name returned by `wsl.exe -l -q`. Installing into one distribution does not configure another.

## Remote SSH checks

Open a terminal in the same VS Code Remote SSH context and run the remote platform's doctor. Confirm that:

- the SSH alias points to the intended machine;
- Codex and the installer use the same remote account;
- Python 3.10+ exists on a Linux target;
- the remote `config.toml`, config, state, and worker are under the remote user's home;
- the remote host can reach the ntfy server directly.

A Linux user service may stop when the user logs out unless that host keeps the user manager alive. If persistent background delivery is required, ask the host administrator whether user lingering is appropriate:

```sh
loginctl show-user "$USER" -p Linger
```

Even without a persistent service, the hook-spawned on-demand worker can drain the queue when Codex creates an event.

## Notification content looks wrong

- Keep `include_message: false` for the generic completion text.
- Set `include_message: true` only if the final assistant response should be stored and sent.
- Adjust `max_message_chars` to change the sent truncation limit.
- Keep `include_full_path: false` to send only the project directory name.
- The title uses the project name by default. Set `include_thread_title: true` to prefer the locally indexed thread title.
- Reload Codex after changing the hook, but ordinary notifier JSON changes are read by the worker on each loop.

Redaction is best-effort. If a sensitive value was published, rotate credentials/topic access and follow the ntfy server/client deletion procedure; changing the config cannot recall it.

## Safe issue checklist

Before opening a public issue, provide only:

- notifier version and platform versions;
- Windows/WSL/native Linux/Remote SSH topology;
- sanitized doctor fields;
- whether the hook, queue, worker, and server request stages were reached;
- sanitized log lines with topics, URLs, usernames, hostnames, paths, IDs, and content replaced.

For a potential vulnerability or credential leak, use the private process in [SECURITY.md](../SECURITY.md).
