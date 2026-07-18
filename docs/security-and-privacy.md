# Security and privacy

Durable Codex ntfy notifier 2.5.1 is an unofficial local hook and worker that reads local Codex lifecycle metadata—and, when explicitly enabled on Windows, Claude Code completion metadata—and sends a small notification to a server selected by the operator. Treat its topic, authentication values, hook configuration, local agent state, notifier state, and backups as private data.

## Privacy defaults

Fresh installations use:

```json
{
  "include_message": false,
  "max_message_chars": 180,
  "include_thread_title": false,
  "include_task_link": false,
  "include_task_link_action": false,
  "include_full_path": false,
  "tags": ["white_check_mark"],
  "priority": 3,
  "markdown": false,
  "suppress_subagents": true,
  "suppress_technical_turns": true,
  "idle_detection_mode": "strict",
  "idle_probe_grace_seconds": 30,
  "unknown_retry_max_seconds": 60,
  "goal_aware": true,
  "watch_rollouts": true,
  "allow_insecure_auth": false,
  "dead_retention_days": 30
}
```

The installers migrate existing private configuration conservatively and do not print secrets. Review content-related settings after any upgrade from a private or older public build.

`strict` is also the privacy/conservatism default for behavior: unresolved root classification or matching rollout evidence is retried locally, then becomes an `unverifiable` suppressed receipt after `idle_probe_grace_seconds` instead of an uncertain network notification. Time alone never promotes it.

## Local data read for idle detection

To distinguish a final root completion from an intermediate result, the notifier can read:

- the matching local Codex rollout JSONL lifecycle;
- thread source/classification and recursive spawn edges from the local Codex state database;
- the `status` column for the root thread in the local goal database;
- local session-index title metadata only when `include_thread_title: true`;
- filesystem modification time and rollout path.

SQLite access is opened read-only and query-only. Goal awareness selects the goal **status**, not its objective. The notifier does not update Codex databases.

With `-EnableClaudeCode`, the Windows hook receives Claude's `session_id`, `prompt_id`, work registries, final message, working directory, and transcript path. Redirected hook JSON is decoded from raw standard input as strict UTF-8 rather than through the active console code page; malformed byte sequences are rejected. The notifier uses the registries only to prove no background work remains. A memory-bounded reverse scan finds the newest local `attachment.goal_status` lifecycle record and aligns bounded tail reads to a valid UTF-8 code-point boundary; only the boolean state and an opaque UUID/hash marker are retained. The pre-existing goal condition/reason is not extracted, stored, logged, or sent. A separate bounded head/tail lookup runs only when `include_thread_title: true` and looks for title metadata. The notifier does not parse the transcript for the final response because Claude supplies `last_assistant_message` directly.

The rollout watcher reads newly appended complete JSONL lines in memory to find `task_complete` and `turn_aborted`. It persists a cursor containing the rollout path, byte offset, timestamp, and thread ID. It does not copy prompt lines into `watch/`.

The local Codex rollout can itself contain prompts, assistant content, tool data, and paths. The notifier needs read access to that pre-existing file, but its own state intentionally stores neither Codex `input-messages` nor prompt bodies. Never attach rollout or database files to a public issue.

## What leaves the host

By default, one ntfy publication can contain:

- the final directory name of the working directory in the title;
- the source host/origin label in a compact, label-free body;
- `#` plus the first eight characters of the Codex thread ID or Claude session ID;
- one ntfy tag—`white_check_mark` for a successful completion, or `warning` for an aborted/blocked result—and a deterministic sequence ID.

The default JSON title is only `<project>`, while the single applicable tag renders one status emoji before it in ntfy. The one-line body is `<origin> · #<thread-or-session8>`. The templates add no notifier name, completion word, model name, status label, or text emoji. Markdown is disabled, and default ntfy priority 3 is represented by omitting the `priority` member from the outgoing JSON. These choices reduce visual and wire noise; they do not make the remaining metadata anonymous.

`include_thread_title: true` opts into the locally indexed Codex task title or bounded Claude `ai-title`/`custom-title` transcript metadata when available. When that title differs from the project, the project moves into the body so the location is retained. A task title can summarize the user's request.

`include_full_path: true` uses the sanitized working-directory path as the body's location context instead of a project-name context item. A full path can reveal usernames, clients, repository names, mounts, or organization structure.

`include_message: true` adds a sanitized and truncated copy of the final assistant message ahead of the context. `max_message_chars` limits that excerpt to 180 characters by default. With the default `markdown: false`, presentational Markdown is converted to compact plain text before whitespace is normalized and the whole body is placed on one line; link labels and table-cell text remain. Explicit `markdown: true` preserves Markdown and line breaks. The complete ntfy `message` is also hard-capped at 3,500 UTF-8 bytes, even if `max_message_chars` is increased. Plain-text conversion, truncation, and the byte cap limit presentation and size, not sensitivity.

For Codex records only, `include_task_link: true` adds `https://chatgpt.com/codex/tasks/<thread-id>` as the ntfy `click` target after validating a canonical UUID. This sends the full thread ID to the ntfy server and its subscribed clients instead of only the default eight-character prefix. The URL still requires the appropriate ChatGPT account, workspace, and Remote host access; it is not an authentication token and does not bypass authorization. `include_task_link_action: true` duplicates the same destination in one visible `view` action, so it is a separate opt-in and remains off by default. Claude records never add this URL or send the full Claude session ID through a task link.

A thread title or assistant response can summarize sensitive prompt context. “Raw prompts are not copied” is not equivalent to “no sensitive context can leave the host.”

The ntfy server sees normal connection metadata such as source IP, request time, and user agent. Its retention/access logs are outside this project’s control. ntfy clients may display content on a lock screen; configure client privacy accordingly.

The `server` URL must be absolute HTTP(S) and must not contain URI userinfo. Put authentication only in the dedicated token or username/password fields. Doctor output reduces the server to scheme, host, and non-default port, omitting path, query, fragment, and credentials.

## What remains on disk

The private configuration is `~/.codex/ntfy-config.json`. It can contain server URL, topic, token, username, password, task-link privacy choices, and installer-managed `watch_roots` entries with local/UNC Codex and SQLite paths for selected WSL distributions.

`~/.codex/ntfy-state` can contain:

- full thread and turn IDs;
- full local working directory, Codex home, SQLite home, and rollout path;
- host/origin, classification, goal status, descendant count, and idle-gate metadata;
- for Claude, session epoch, prompt-correlated idle state, and opaque goal baseline/current markers;
- watcher paths, byte offsets, timestamps, and thread IDs;
- retry timing, attempt count, and sanitized error text;
- the final assistant message only for records created while `include_message` was enabled;
- compact sent, subagent, technical-turn, and superseded receipts;
- dead letters, which may retain a complete failed record.

The operational log records short event keys, gate reasons, origin labels, retry state, and sanitized errors. Redaction is best-effort; logs are not automatically safe to publish.

Installers keep up to ten timestamped directories under `~/.codex/ntfy-backups`. A backup can contain credentials, `config.toml`, hook registration, and—when Claude support is enabled—the complete pre-install Claude `settings.json`. Rotation is by count, not age. Remote hosts own their own copies and backups.

`include_message` is enforced both when content is captured and again when the worker builds the network payload. If it is changed from `true` to `false`, final-message text already present in pending or outbox records is excluded at send time and does not leave the host through a later retry. The setting does not scrub that content from existing state, dead letters, logs, or backups.

For an urgent opt-out, stop the worker before editing the private config so it cannot begin another request concurrently. No local setting can recall a request already in flight or content already accepted, retained, or displayed by ntfy. Other content-setting changes likewise do not rewrite existing local state or remote/client history.

## Hook review and trust

The installer writes a managed `Stop` handler to `~/.codex/hooks.json` and preserves unrelated hook groups and metadata. Codex requires the operator to review a new hook through `/hooks`.

When explicitly enabled, the Windows installer also atomically merges ordered synchronous `Stop`/`StopFailure`/`UserPromptSubmit` and asynchronous `Notification` handlers for prompt-correlated `idle_prompt`/`agent_completed` into `~/.claude/settings.json`. Synchronous terminal hooks preserve same-prompt ordering; their initial transcript scan is capped at 1 MiB and full reconciliation runs in the worker. The installer uses executable-plus-argument form, preserves unrelated settings and hooks, stores no ntfy credential there, and includes the original file in the private rollback snapshot. Claude's `/hooks` view is read-only; direct settings changes are normally reloaded automatically.

The installer deliberately does **not** edit Codex’s trust store or simulate approval. This keeps code execution consent with the user. Review the exact command in every Windows, WSL, Linux, or remote Codex environment before trusting it.

The hook command contains local executable paths but no ntfy topic or credential. Those secrets remain in the private config or worker environment. Treat `hooks.json` as environment metadata even though it should not contain authentication values.

## Redaction limits

When content storage is enabled, the notifier normalizes whitespace, truncates values, and redacts several common patterns, including authorization headers, password/token/key assignments, selected provider token prefixes, and ntfy topic URLs.

Regex redaction cannot reliably detect every secret, custom hostname, source-code credential, private key, personal datum, or value whose context has been removed. It can also produce false positives. The safest settings for sensitive work are `include_message: false`, `include_thread_title: false`, `include_task_link: false`, and `include_full_path: false`.

## Topics and credentials

An unguessable topic on a public ntfy service acts like a capability: anyone who learns it may be able to subscribe or publish, depending on server policy. Prefer explicit access control on a trusted server when notification metadata is sensitive.

Recommended practice:

- create a separate publish-only token for each real host;
- do not reuse an administrator or subscribe-capable credential for publishing;
- use different topics or credentials for environments with different trust levels;
- rotate the affected topic/token after a config, backup, environment dump, shell history, or issue attachment leaks;
- revoke a retired host’s token instead of relying only on file deletion;
- never commit a real `ntfy-config.json` or paste it into an issue.

Environment variables override server/authentication fields. They reduce long-lived config content only if the surrounding process, service manager, shell history, crash reporting, and process inspection are also trusted. Clear temporary variables after installation.

## Transport protections

The notifier:

- refuses token or basic-auth credentials over non-HTTPS connections to non-loopback hosts unless `allow_insecure_auth: true`;
- permits HTTP authentication to `localhost`, `127.0.0.1`, and `::1` for a local trusted proxy;
- refuses all HTTP redirects so an authorization header is not forwarded;
- applies a configurable request timeout.

Anonymous HTTP publication is not blocked because no authorization header is present, but topic and message remain visible and modifiable in transit. HTTPS is strongly recommended.

`allow_insecure_auth: true` sends reusable credentials without transport confidentiality. Reserve it for a separately protected network tunnel whose risk is understood.

TLS validation uses the host operating system/runtime trust store. The project does not implement certificate pinning.

## Filesystem protections

Installers attempt to limit access:

| Platform | Protection |
| --- | --- |
| Windows | Private ACLs for the current user, `SYSTEM`, and local administrators on config, state, staging, hooks, and backups where managed. |
| Linux/WSL | Mode `0600` for private config/TOML/JSON and `0700` for executable or state directories where installed. |
| Linux systemd | `UMask=0077`, `NoNewPrivileges=true`, and `PrivateTmp=true`. |

These controls do not protect against the same user account, administrator/root, malware in that security context, a compromised SSH endpoint, or offline access to unencrypted storage. Use full-disk encryption and appropriate host security.

Inspect permissions without printing contents:

```powershell
icacls "$HOME\.codex\ntfy-config.json"
icacls "$HOME\.codex\hooks.json"
icacls "$HOME\.codex\ntfy-state"
```

```sh
stat -c '%a %U:%G %n' "$HOME/.codex/ntfy-config.json" "$HOME/.codex/hooks.json" "$HOME/.codex/ntfy-state"
```

## Remote installation trust

Remote installers use existing OpenSSH configuration and host verification. They copy the private destination, authentication, and policy through a restricted staging directory, then install it for the remote account. Host-local `watch_roots` are cleared because source-machine WSL/custom paths are not portable. Therefore:

- the local machine, SSH client config, selected host key, remote account, and remote administrators are in the trust boundary;
- every target receives the source config’s credentials unless a host-specific config is supplied;
- backups on every target can retain old credentials after rotation;
- an SSH alias must resolve to the intended machine before credentials are copied;
- hook approval must be performed in the remote Codex environment.

Prefer a host-specific publish-only token. Verify a new host interactively before unattended installation.

## Retention and erasure

Defaults are 14 days for sent/suppressed receipts and 30 days for dead letters. Cleanup runs when a worker starts.

Busy pending candidates and network-ready outbox records have no time-based expiry because silently dropping an unfinished/offline notification would violate the delivery goal. Unknown evidence is different: in `strict` mode it is retried locally only through `idle_probe_grace_seconds`, then reduced to an `unverifiable` suppressed receipt and never sent. An epoch-anchored Claude candidate that cannot be reconciled with its locked session/prompt epoch at the final commit boundary is likewise terminalized locally with receipt reason `claude-session-unverifiable`; it never enters the outbox. Maintenance does not delete a Claude session-state record while a pending candidate references it. Watch cursors persist so scans can resume without replaying old rollout history; unchanged cursor files are not rewritten.

For deliberate local erasure:

1. stop the worker;
2. decide whether pending and outbox events should be delivered or discarded;
3. delete the relevant state and backup directories on every host;
4. delete or replace private config and managed hook/config entries as appropriate;
5. revoke or rotate server-side credentials and topic access;
6. clear ntfy client history and follow the server’s deletion/retention procedure.

Deleting local files does not recall a notification already published to ntfy. See [Uninstall and rollback](uninstall.md).

## Pure cloud boundary

The notifier observes local hook invocations, local Codex rollout files, and local databases. A task executed only in a hosted/cloud environment with no lifecycle state mirrored locally is outside that observation boundary. No local configuration can guarantee a notification for such a task.

## Safe support and disclosure

`--doctor`/`-Doctor` reports configuration state rather than secret values and is the preferred first diagnostic. Review hostnames and paths before sharing even sanitized output.

Never attach raw files from `ntfy-state`, `ntfy-backups`, Codex sessions, Codex databases, shell environments, `hooks.json`, or private config to a public issue. Replace hostnames, usernames, paths, IDs, topics, and URLs in logs. Report vulnerabilities through [SECURITY.md](../SECURITY.md).
