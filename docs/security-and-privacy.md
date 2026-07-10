# Security and privacy

Durable Codex ntfy notifier 2.3.0 is an unofficial local hook that sends metadata to a server chosen by the operator. Treat its topic, authentication values, state, and backups as private data.

## Privacy defaults

Fresh installations use:

```json
{
  "include_message": false,
  "include_thread_title": false,
  "include_full_path": false,
  "suppress_subagents": true,
  "allow_insecure_auth": false,
  "dead_retention_days": 30
}
```

An upgrade preserves the behavior of an older installation. In particular, an existing configuration that has no `include_message` or `include_thread_title` key is migrated to `true` for compatibility. Review both values after upgrading if message content or prompt-derived thread titles should no longer be sent.

## What leaves the host

By default, one ntfy publication can contain:

- the final directory name of the Codex working directory;
- the source host/origin label;
- the first eight characters of the thread ID;
- the project directory name as the notification title;
- a generic `Turn completed.` message;
- configured tags, priority, Markdown flag, and a deterministic sequence ID.

`include_full_path: true` sends the sanitized working-directory path instead of only its final name.

`include_thread_title: true` replaces the project title with the locally indexed Codex thread title when available. `include_message: true` also sends a sanitized and truncated copy of the final assistant message. The limit is `max_message_chars` (900 characters by default). The notifier does not add Codex `input-messages` to its queue or ntfy body. A thread title or assistant reply can still summarize sensitive user context, so the absence of raw prompts is not equivalent to zero content disclosure.

The server sees normal connection metadata such as source IP, request time, and user agent. Its retention and access logs are outside this project's control. ntfy clients may show content on a lock screen; configure client-side notification privacy as needed.

## What remains on disk

The private configuration is `~/.codex/ntfy-config.json`. It can contain the server URL, topic, token, username, and password.

The local `~/.codex/ntfy-state` directory can contain:

- full thread and turn IDs;
- the full local working directory and session Codex home;
- host/origin and classification metadata;
- retry timing, attempt count, and sanitized error text;
- the final assistant message only for records created while `include_message` was enabled;
- compact sent and suppressed receipts;
- dead letters, which may retain a complete failed record.

The operational log records short event keys, origin labels, retry state, and sanitized errors. Redaction is best-effort; do not assume logs are safe to publish.

Installers keep up to ten timestamped directories under `~/.codex/ntfy-backups`. A backup can contain credentials and a previous `config.toml`. Rotation is by count, not age. Remote hosts have their own copies and backups.

Changing `include_message` to `false` affects new events only. Changing `include_thread_title` affects future delivery payloads. Neither change rewrites dead letters, logs, backups, notifications already accepted by ntfy, or a client's notification history.

## Redaction limits

When content storage is enabled, the notifier normalizes whitespace, truncates values, and redacts several common forms, including authorization headers, password/token/key assignments, selected provider token prefixes, and `ntfy.sh` topic URLs.

Regex redaction cannot reliably detect every secret, custom hostname, source-code credential, private key, personal datum, or value whose context has been removed. It can also produce false positives. The safe choice for sensitive work is to keep `include_message: false`, `include_thread_title: false`, and `include_full_path: false`.

## Topics and credentials

An unguessable topic on a public ntfy service acts like a capability: anyone who learns it may be able to subscribe or publish, depending on server policy. Prefer explicit access control on a trusted ntfy server when notification metadata is sensitive.

Recommended practice:

- create a separate publish-only token for each real host when the server supports scoped tokens;
- do not reuse an administrator or subscribe-capable credential for publishing;
- use different topics or credentials for environments with different trust levels;
- rotate the affected topic/token if a config, backup, environment dump, command history, or issue attachment leaks;
- revoke a retired host's token instead of relying only on deletion from that host;
- never commit a real `ntfy-config.json` or paste it into an issue.

Environment variables override server and authentication fields. They reduce long-lived config content only if the surrounding process, service manager, shell history, crash reporting, and process inspection are also trusted. Clear temporary variables after installation.

## Transport protections

The notifier:

- refuses to send token or basic-auth credentials over non-HTTPS connections to non-loopback hosts unless `allow_insecure_auth: true` is explicitly set;
- permits HTTP authentication to `localhost`, `127.0.0.1`, and `::1` for a local trusted proxy;
- refuses all HTTP redirects, so an authorization header is not forwarded to a redirect target;
- applies a configurable request timeout.

Anonymous publication over HTTP is not blocked because no authorization header is present, but the topic and message remain visible and modifiable in transit. HTTPS is still strongly recommended.

`allow_insecure_auth: true` sends reusable credentials without transport confidentiality. It should be reserved for a separately protected network tunnel whose risk is understood.

TLS validation uses the host operating system and runtime trust store. The project does not implement certificate pinning.

## Filesystem protections

Installers attempt to limit access as follows:

| Platform | Protection |
| --- | --- |
| Windows | Private ACLs for the current user, `SYSTEM`, and local administrators on config, state, staging, and backup paths. |
| Linux/WSL | Mode `0600` for private config/TOML and `0700` for executable or state directories where installed. |
| Linux systemd | `UMask=0077`, `NoNewPrivileges=true`, and `PrivateTmp=true`. |

These controls do not protect against the same user account, an administrator/root account, malware in that security context, a compromised SSH endpoint, or offline access to unencrypted storage. Use full-disk encryption and appropriate host security where necessary.

To inspect permissions without printing file contents:

```powershell
icacls "$HOME\.codex\ntfy-config.json"
icacls "$HOME\.codex\ntfy-state"
```

```sh
stat -c '%a %U:%G %n' "$HOME/.codex/ntfy-config.json" "$HOME/.codex/ntfy-state"
```

## Remote installation trust

Remote installers use the existing OpenSSH configuration and host verification. They copy the complete private configuration through a permission-restricted staging directory, then install it for the remote account. This means:

- the local machine, SSH client configuration, selected host key, remote account, and remote administrators are all in the trust boundary;
- every target receives the source config's credentials unless a host-specific config is supplied;
- backups on every target can retain old credentials after rotation;
- an SSH alias must resolve to the intended machine before credentials are copied.

Prefer a host-specific config containing a publish-only token. Verify a new host interactively before using an unattended installer.

## Retention and erasure

Defaults are 14 days for sent/suppressed receipts and 30 days for dead letters. Cleanup occurs when a worker starts. Pending outbox records have no time-based expiry because dropping an offline notification would violate the delivery goal.

For deliberate local erasure:

1. stop the worker;
2. decide whether pending outbox events should be delivered or discarded;
3. delete the relevant state and backup directories on every host;
4. delete or replace the private config;
5. revoke or rotate server-side credentials and topic access;
6. clear ntfy client history and follow the server's deletion/retention procedure if required.

Deleting local files does not recall a notification already published to ntfy. See [Uninstall and rollback](uninstall.md) for platform-specific commands.

## Safe support and disclosure

The `--doctor`/`-Doctor` output deliberately reports configuration state rather than secret values, and is the preferred first diagnostic. Even so, review hostnames and paths before sharing it.

Never attach raw files from `ntfy-state`, `ntfy-backups`, Codex sessions, shell environments, or private config to a public issue. Sanitize logs manually and replace hostnames, usernames, paths, IDs, topics, and URLs. Report possible vulnerabilities through the private process in [SECURITY.md](../SECURITY.md).
