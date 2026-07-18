# Architecture

This document describes the completion-detection and delivery model implemented by version 2.5.2. Durable Codex ntfy notifier is an unofficial community project; it is not an OpenAI, Anthropic, or ntfy component.

## Design goal

The notifier has one user-facing rule:

> Send one notification when a root coding-agent task has no more work, not whenever an intermediate turn emits a completion-shaped event.

Codex can complete a turn and immediately start another, keep a goal active, or leave a delegated descendant running. Therefore a hook callback is treated as evidence to evaluate, not proof that the whole task is idle.

The design favors:

- independent logical-idle state per root thread, so concurrent Codex app, VS Code, and CLI tasks do not debounce one another;
- multiple local completion signals, with no single hook as a delivery single point of failure;
- a strict fail-closed default for uncertain root/rollout evidence;
- a short hook path with no required network request;
- one independent queue and worker per real Windows, WSL, Linux, or SSH host;
- durable at-least-once ntfy delivery after idle is confirmed;
- privacy-preserving defaults, especially `include_message: false`;
- a minimal payload that is useful on a lock screen without repeating the notifier name or completion state.

## Components

| Component | Responsibility |
| --- | --- |
| Codex modern `Stop` hook | Supplies an explicit session-stop candidate on standard input. Local rollout/database evidence still determines whether that session is a root or descendant. It is never sent directly. |
| Codex legacy root-level `notify` | Supplies `agent-turn-complete` as a compatibility/fallback candidate. |
| Claude Code `Stop` / `StopFailure` hooks (Windows, opt-in) | Supply authoritative main-agent completion/error candidates. `Stop` is accepted only with empty background-task and session-cron registries. |
| Incremental rollout watcher | Discovers recent local `task_complete` or `turn_aborted` records that hooks missed. On Windows, the persistent local scanner is seeded by Codex's read-only SQLite thread index; UNC/WSL roots use a separate bounded scanner. |
| `notify-ntfy.ps1` / `notify-ntfy.py` | Normalizes signals, classifies roots/subagents, maintains pending probes, applies the idle gate, and delivers the outbox. |
| Windows scheduled task `CodexNtfyWatcher` | Launches the hidden VBS supervisor directly. The VBS starts and restarts the notifier worker without the former two-hop cold PowerShell launcher chain. |
| Linux user unit `codex-ntfy.service` | Runs the continuous Python worker and rollout watcher when user systemd is available. |
| `notify-ntfy-wsl.sh` | Preserves WSL session paths and classification, prefers the Windows queue, and falls back to native Python state. |
| `ntfy-config.json` | Stores the private destination, credentials, idle policy, delivery policy, and privacy options. |
| `ntfy-state/` | Stores idle candidates, rollout cursors, network-ready events, receipts, dead letters, locks, and the bounded log. |

The modern Codex hook format and review model are documented in [Codex Hooks](https://learn.chatgpt.com/docs/hooks). The legacy external notification is documented in [Codex advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications). Claude fields and lifecycle semantics come from the [Claude Code hooks reference](https://code.claude.com/docs/en/hooks).

## Signal ingestion

Four sources feed the same record schema:

1. **Modern `Stop`.** The installer registers a managed `hooks.Stop` handler. The notifier reads its JSON from standard input, returns an empty JSON object as the hook result, classifies the session from local Codex state, and stores only an accepted root candidate. It intentionally ignores an event explicitly named `SubagentStop`; current Codex versions can also report descendant sessions through `Stop`, so the classifier remains mandatory.
2. **Legacy `notify`.** The existing root-level notification remains installed as a compatibility path. Its `agent-turn-complete` payload is normalized to the same candidate schema.
3. **Rollout watcher.** A continuous worker tails rollout JSONL files under its own `CODEX_HOME` and any explicitly registered roots using a persisted byte offset. On Windows, the persistent local scanner queries Codex's read-only SQLite thread index for active and recently resumed rollout paths, then supplements those results with hot current-day files. It does not enumerate historical cursor files or recursively walk `sessions/` and `archived_sessions/` on every continuous scan; the expensive full archive walk is reserved for an explicit manual `ScanScope=All` run. UNC/WSL roots run in a separate one-shot scanner with independent cursor handling and a `watch_remote_timeout_seconds` limit, so a suspended distro or slow share cannot block local recovery or delivery. The watcher consumes only complete newline-terminated records, leaves an incomplete trailing line for the next scan, and does not rewrite a cursor when file size and modification time are unchanged. A local `task_complete` or `turn_aborted` can therefore reconstruct a candidate when a hook process was never launched. The Windows installer registers the selected WSL Codex and SQLite roots without globally guessing a newest session.
4. **Claude Code hooks on Windows.** With `-EnableClaudeCode`, exec-form handlers read `Stop`, `StopFailure`, `UserPromptSubmit`, and selected `Notification` JSON from standard input. Main-agent `Stop` requires `session_id`, `prompt_id`, and present, empty `background_tasks` plus `session_crons`; missing or active registries fail closed without a receipt so the same prompt can later become final. `Stop`, `StopFailure`, and `UserPromptSubmit` are deliberately synchronous: this preserves host lifecycle order, prevents repeated same-prompt stops from overwriting a newer result out of order, increments the session epoch before a new prompt, and removes older pending candidates before Claude can reach a fast `Stop`. Their prompt-path transcript scans are capped at 1 MiB; only the `idle_prompt`/`agent_completed` notification accelerators are asynchronous. `StopFailure` is terminal by definition. Claude candidates bypass Codex SQLite/rollout classification, reuse the shared pending/outbox worker, and use the supplied `last_assistant_message` for the notification body.

On Windows, redirected hook JSON is decoded directly from raw standard input with a strict UTF-8 decoder, independently of the active console code page. Invalid byte sequences fail ingestion instead of being replaced and then interpreted as different JSON text.

The watcher initializes an old rollout at its current end; it only replays a newly discovered rollout when the file was modified within `watch_initial_replay_seconds`. This avoids turning historical sessions into fresh notifications after installation.

Receipt retention and legacy lock cleanup run in a separate maintenance process no earlier than 60 seconds after startup and only after delivery plus the applicable local and remote scanners have reported ready, completed, timed out, or failed. Maintenance holds its own lock and cannot delay worker readiness or a completion notification.

Codex recovery sources share the same deterministic key when both thread and turn IDs are present. Claude uses a provider-isolated key from `session_id + prompt_id`, so simultaneous Claude sessions and Codex tasks cannot collide.

## State machine

```text
Codex Stop hook ────────┐
legacy notify ──────────┼──> normalize/classify ──> pending/
rollout watcher ────────┤                              |
Claude lifecycle hooks ─┘                              |
                                                       v
                                             coalesce by root thread
                                                       |
                                                       v
                                           idle gate + final snapshot
                         ┌─────────────────────────────┼───────────────────────┐
                         | busy                 | superseded/unverifiable  | idle
                         v                      v                           v
                    remain pending/        suppressed receipt           outbox/
                                                                               |
                                                                               v
                                                                       POST to ntfy
                                                                               |
                                                                               v
                                                                        sent receipt
```

Candidates are written through a private temporary file and an atomic filesystem operation. Simultaneous signals cannot expose a partial JSON record. A bounded set of hash-prefix cross-process mutation locks serializes changes to the same event key, so a worker using an older snapshot cannot overwrite or suppress newer authoritative `Stop` evidence without leaving one lock file per event in the state root. Cleanup removes legacy root-level mutation-lock debris.

## Logical-idle gate

For the default `strict` mode, a root candidate becomes network-ready only after these checks:

1. **Root classification.** A known subagent is suppressed. An unknown root/subagent classification is retried during the evidence-probe window, then suppressed locally as `unverifiable` in strict mode.
2. **Matching completion.** The root rollout contains `task_complete` or `turn_aborted` for the candidate turn.
3. **No later open turn.** A later `task_started` makes the earlier completion an obsolete predecessor, so it is suppressed; the later task's own terminal event creates the useful candidate.
4. **Goal not active.** With `goal_aware: true`, a goal whose status is `active` keeps the candidate pending. Terminal/non-running states such as `complete`, `paused`, `blocked`, `usage_limited`, and `budget_limited` do not block delivery.
5. **No active descendants.** The notifier traverses Codex `thread_spawn_edges` recursively and inspects child rollout lifecycles. A recent child ending in `task_started` or an unknown active tail keeps the root pending.
6. **Quiet window.** The matching rollout must remain unchanged for `idle_grace_seconds`.

The gate takes a second fresh snapshot while the candidate is still in `pending/`, immediately before the atomic promotion. Promotion closes that logical idle epoch: an `outbox/` record is a durable delivery and is never coalesced with a later user request or moved back to pending. Network retries preserve that boundary.

The notifier reads Codex SQLite databases read-only and enables query-only mode. Goal awareness selects only the `status` column; it does not query the goal objective. If the goal database is unavailable, a goal status persisted in the rollout can still be used. Missing goal information alone is not interpreted as an active goal.

Claude uses a separate transcript gate. At `UserPromptSubmit`, the newest session-level `attachment.goal_status` marker in the bounded tail becomes the prompt baseline. Bounded byte windows are advanced to a valid UTF-8 code-point boundary before decoding, so beginning inside a multibyte character cannot corrupt the first retained line. The synchronous `Stop` performs the same bounded initial reverse read; if that is insufficient, the detached worker performs the memory-bounded full reconciliation. `met:false` is active and remains pending; a newer `met:true` is achieved; `failed:true` is terminal-blocked; and `met:true,sentinel:true` is a manual clear that removes the candidate without a sent/suppressed receipt. Historical terminal markers equal to the prompt baseline are ignored, so they cannot label or cancel later ordinary turns. If a candidate was anchored to an active marker, only a different terminal marker proves finality. Missing, oversized, or malformed lifecycle evidence fails closed; prompt-correlated `idle_prompt`/`agent_completed` can be used only as an optional fallback for recoverable unknown state and are never required before a transcript-proven terminal result.

Immediately before promotion, a Claude candidate is checked again against its locked session and prompt epoch. For an epoch-anchored candidate (`claude_session_epoch > 0`), a missing, corrupt, or incompatible session record at that commit boundary terminalizes the candidate locally with receipt reason `claude-session-unverifiable`; it cannot enter the outbox and is not retried indefinitely. Epoch-zero compatibility candidates retain their existing current-session behavior.

Active descendants are bounded by `subagent_orphan_seconds`. The timeout prevents a crashed or abandoned child rollout from blocking its root forever; it is a liveness tradeoff rather than proof that the child succeeded.

While a descendant is inside that active window, malformed lifecycle JSON, invalid UTF-8, or a missing string turn ID is permanent invalid evidence for the current candidate: both strict and balanced modes fail closed instead of releasing the root. A trailing record without its newline is treated as transient busy state and retried, while ordinary I/O races keep the existing unknown/orphan behavior.

For a large Windows rollout, the idle probe first parses relevant JSON records structurally with a native snapshot-bounded reader. It returns a fixed 23-field summary containing only terminal/open-turn, goal, user/final-message, incomplete-tail, and before/after snapshot facts needed by the existing gate; historical lifecycle lines never cross back into PowerShell. A snapshot ending in a partial JSONL record returns the bounded incomplete-tail summary immediately and is retried only after more data arrives. The compatibility parser processes 64 KiB chunks and retains only an incomplete line. This keeps the fail-closed decision semantics unchanged without a tens-of-megabytes replay, duplicate full-tail buffers, or a second PowerShell deserialization pass.

## Detection modes

| Mode | Behavior | Intended use |
| --- | --- | --- |
| `strict` | Requires verifiable root classification and matching rollout completion. Unknown evidence is retried exponentially and, after `idle_probe_grace_seconds`, suppressed locally as `unverifiable`; it is never promoted by time alone. | Default; prioritize no premature notification. |
| `balanced` | Applies the same positive busy checks but may accept otherwise valid evidence that remains unknown or unavailable after `idle_probe_grace_seconds`. Malformed lifecycle/UTF-8 data and a partial trailing JSONL record are never promoted. | Prefer eventual notification when local history may be unavailable. |
| `off` | Skips idle detection and queues each accepted completion signal immediately. | Compatibility, diagnostics, or intentionally per-turn behavior. |

`balanced` can produce a false final notification if otherwise valid evidence is still unknown or unavailable when its fallback expires. Invalid or half-written lifecycle data remains fail-closed in every mode except `off`. `off` restores the noisy per-turn behavior that version 2.4 and later are designed to avoid.

## Coalescing and technical-turn suppression

The worker compares pending records with the same root thread ID. Older pending candidates become compact `suppressed/` receipts with reason `superseded`; a candidate whose rollout already contains a later open task is also suppressed immediately as an obsolete predecessor. Only that later task's terminal candidate can be promoted. A record already promoted to `outbox/` is immutable and is not coalesced with a later request. Coalescing is per root thread, never a global delay, so unrelated simultaneous tasks remain independent.

With `suppress_technical_turns: true`, a legacy/watcher candidate without both a user-message marker and a final assistant-message marker is suppressed unless it represents a terminal goal state. A modern `Stop` candidate classified as root bypasses this heuristic because it is the more explicit user-facing lifecycle signal.

Events explicitly named `SubagentStop`, descendant `Stop` candidates, and descendant `agent-turn-complete` records are suppressed by default. A descendant never creates its own ntfy notification; its running state can still delay the root notification.

## Record identity and deduplication

When both IDs are present, the event key is the SHA-256 digest of:

```text
codex-ntfy/v1 | thread-id | turn-id
```

The same key names pending, outbox, sent, or suppressed state. A repeated signal sees the existing record or receipt and does not create a second notification.

If either ID is absent, the notifier creates a random weak identity. Deterministic local deduplication is then impossible, and `weak_identity` records that limitation.

Every delivery attempt for one record reuses `codex-<first-32-hex-characters-of-key>` as its ntfy `sequence_id`. This reduces duplicate presentation after an ambiguous timeout on ntfy implementations that honor sequence updates, but it is not a distributed transaction.

Claude strong identities hash `codex-ntfy/v1 | claude | session-id | prompt-id` and use the parallel `claude-<first-32-hex-characters-of-key>` sequence namespace. A repeated stop for the same prompt atomically refreshes the still-pending record and its revision instead of creating another notification. Promotion compares that revision under the same mutation lock, so a worker holding an older snapshot cannot send the previous message. A cleared goal is removed revision-safely without a terminal receipt, allowing later prompts to use their own keys normally.

## Delivery guarantee

After logical idle is confirmed, the intended guarantee is **durable at-least-once delivery**:

1. the complete event is visible in `outbox/` before a worker attempts the request;
2. a successful 2xx response is followed by an atomic sent receipt;
3. only after the receipt exists is the outbox record removed;
4. transient failures update the record with attempt count and next-attempt time;
5. a process crash leaves either the outbox record, the receipt, or both in a recoverable state.

If ntfy accepts a request and the worker crashes or times out before the receipt is written, the worker retries with the same sequence ID. Exactly-once display cannot be guaranteed.

Strict idle detection changes the liveness boundary: a true completion whose evidence cannot be verified is retried only during `idle_probe_grace_seconds`, then becomes a local `unverifiable` receipt. It is deliberately withheld rather than turned into a false “finished” notification; strict never fails open.

## Send-time payload assembly

The compact wire presentation introduced in version 2.4.2 is independent of the logical-idle decision. The worker renders the ntfy JSON from the durable record and the current private configuration immediately before each delivery attempt.

With the default tag, the visible title is:

```text
✅ <task-or-project>
```

The outgoing JSON `title` is only the display value: the project directory by default, or an available local task title with `include_thread_title: true`. Title lookup queries `threads.title` by exact thread ID from the current state database in read-only/query-only mode, then checks `session_index.jsonl` as a compatibility fallback. The single `white_check_mark` tag supplies the completion emoji rendered by ntfy. No notifier name, completion word, model name, or textual lifecycle status is prepended.

The default plain-text body is a label-free sequence joined by ` · `:

```text
[final-message ·] [project-or-opted-in-path ·] origin · #thread8
```

The project appears in the body only when a distinct task title occupies the title. `include_full_path: true` instead adds the sanitized working directory as explicit extra context. The optional final-message excerpt appears only while the current `include_message` setting is true and is limited by `max_message_chars` (180 by default). With the default `markdown: false`, fences and presentational heading, list, table, link, emphasis, and inline-code syntax are reduced to compact plain text before whitespace is normalized into one line; link labels and table-cell text are retained. An explicit `markdown: true` opt-in preserves message lines and Markdown and is the exception to that one-line default.

The full ntfy `message` is truncated on a valid Unicode boundary to at most 3,500 UTF-8 bytes after context is assembled. This byte ceiling is independent of the configurable character limit for the excerpt.

Fresh configuration uses `tags: ["white_check_mark"]`; the templates add no duplicate emoji to title or body. Priority 3 is ntfy's default and is omitted from the outgoing JSON. The worker serializes `priority` only for a non-default value and serializes `markdown: true` only when Markdown was explicitly enabled and an optional message is present.

Task navigation is assembled at send time only for Codex records. With `include_task_link: true`, a canonical UUID becomes `https://chatgpt.com/codex/tasks/<thread-id>` in the JSON `click` member. An absent or non-canonical ID omits navigation without failing delivery. `include_task_link_action: true` additionally emits one `view` action with the same URL. Claude records omit both fields because no documented existing-session Claude Code deep link maps safely from the local session ID.

The send-time `include_message` check is a privacy gate for durable state. A record captured while the option was true can still contain final-message text locally, but changing it to false prevents that text from entering later network attempts, including retries of an outbox record. Lifecycle probe caches follow the current setting too: message-disabled native and incremental scans retain only presence facts, and changing the setting forces a replay instead of reusing text or presence markers from the opposite mode. This does not erase an already-written record or its backups and cannot recall a request already in flight or accepted by ntfy.

## Concurrency and host topology

Hooks and rollout scans may run concurrently. Each state directory has one non-blocking worker lock, so a scheduled/service worker and an on-demand worker do not process the same host queue simultaneously. Sharded `mutation-locks/` additionally serialize hook/worker changes to the same deterministic event key without serializing unrelated chats.

On Windows, the supervisor starts three independent runtime paths: durable delivery, the persistent local scanner, and a timeout-bounded one-shot remote scanner when UNC/WSL roots are configured. Their separate locks and health files prevent remote latency from serializing local discovery or network delivery. The remote timeout budget starts only after the child process is created, so slow process startup is not misreported as remote scan time.

Remote SSH does not share that UNC scanner: each remote installation owns a host-local worker, queue, rollout state, and network path. A stalled SSH host therefore cannot serialize the local Windows queue either.

The lock is a delivery serialization mechanism, not a global chat debounce. Pending probes and coalescing use the root thread ID, so several VS Code windows, app tasks, or CLI sessions can reach idle independently.

Queues are deliberately not shared between machines:

- local Windows owns its state;
- each native Linux or Remote SSH host owns its state;
- each WSL distribution has its own Codex session state even when it bridges delivery into the Windows queue;
- separate `CODEX_NTFY_STATE_DIR` values do not share receipts.

WSL routing:

1. the WSL hook preserves its Linux `CODEX_HOME` and optional `CODEX_SQLITE_HOME`;
2. when Windows interop is available, the bridge sends the candidate plus those session locations to PowerShell;
3. Windows owns pending/outbox delivery while evaluating the WSL rollout and database paths;
4. classification itself is side-effect free and cannot start or flush the native worker;
5. only if the bridge fails does native Python state own the candidate and start an on-demand worker.

The Windows continuous watcher does not enumerate arbitrary WSL distributions. `install.ps1 -WslDistro <name>` records that distribution's Codex root, optional distinct SQLite root, and source label in the private configuration. If neither WSL hook launches, the Windows worker can then recover the persisted completion directly; unregistered distributions remain isolated.

## Retry and failure policy

Before network delivery, unknown root or rollout evidence is retried with exponential intervals capped by `unknown_retry_max_seconds` (60 seconds by default) and bounded by `idle_probe_grace_seconds`. The delivery worker separately uses exponential backoff with jitter, capped by `retry_max_seconds` (900 seconds by default). A numeric `Retry-After` is honored within the delivery cap.

| Failure | Default handling |
| --- | --- |
| DNS, connection, TLS, timeout, 5xx | Retry. |
| 401, 403, 408, 409, 425, 429 | Retry, because credentials, policy, or rate limits may be repaired. |
| Redirect (3xx) | Reject and dead-letter; redirects are never followed. |
| Other 4xx | Treat as permanent and dead-letter. |
| Invalid pending/outbox JSON or schema | Store a sanitized minimal dead letter and continue. |
| Positive `max_attempts` reached | Dead-letter the record. |

`max_attempts: 0` means retry transient failures indefinitely.

## Stored state

```text
ntfy-state/
  pending/      root candidates, idle probe state, and next probe time
  outbox/       idle-confirmed events and retry metadata
  watch/        rollout paths, byte offsets, timestamps, and thread IDs
  sent/         compact successful-delivery receipts
  suppressed/   compact subagent, technical, and superseded receipts
  dead/         invalid or permanently failed records
  mutation-locks/ bounded sharded cross-process locks for same-key state changes
  worker.lock   per-state-directory worker lock
  maintenance.lock retention-cleanup lock
  watch-health.json local scanner health
  remote-watch-health.json isolated UNC/WSL scanner health
  notify.log    bounded operational log (one rotated generation)
```

Pending/outbox records can contain local paths, full thread and turn IDs, origin/classification metadata, and—only when opted in—the final assistant message. Watch cursors do not contain prompt bodies, but their rollout paths and thread IDs are private metadata. See [Security and privacy](security-and-privacy.md).

## Hook trust boundary

Installers write the managed `Stop` registration but do not grant it trust. Codex requires the operator to review the hook using `/hooks` before it runs. This avoids silently modifying a product security decision. Unrelated hook groups, handlers, and metadata are preserved.

Legacy `notify` and the rollout watcher are independent fallback sources, so an untrusted modern hook does not by itself disable all detection. A continuous worker is required for autonomous rollout recovery.

## Upstream boundaries

The project depends on local state emitted by Codex and, when explicitly enabled, Claude Code:

- modern hook availability and semantics are controlled by Codex;
- legacy notifications currently expose `agent-turn-complete` rather than a first-class “entire chat is permanently idle” event;
- rollout JSONL and local SQLite schemas can change upstream;
- an exceptionally large legacy Windows payload can fail before this notifier is launched;
- pure cloud tasks that do not mirror lifecycle state locally cannot be observed;
- rollout recovery requires a continuous worker and is scoped to its own plus explicitly registered Codex homes;
- Claude hook availability and fields are controlled by Claude Code, and each detected Windows surface must meet the installer's minimum supported version;
- Claude `/goal` finality depends on the upstream local `attachment.goal_status` transcript format, which can change;
- hosted Claude work that does not execute a local hook cannot be observed;
- ntfy retention, access control, sequence behavior, and client display are controlled by the chosen server and client.

The notifier is not an audit log, task scheduler, or proof that a model result was correct. It reports the best locally verifiable end of work for a supported root coding-agent task.
