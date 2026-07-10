# Architecture

This document describes the completion-detection and delivery model implemented by version 2.4.1. Durable Codex ntfy notifier is an unofficial community project; it is not an OpenAI or ntfy component.

## Design goal

The notifier has one user-facing rule:

> Send one notification when a root Codex task has no more work, not whenever an intermediate turn emits a completion-shaped event.

Codex can complete a turn and immediately start another, keep a goal active, or leave a delegated descendant running. Therefore a hook callback is treated as evidence to evaluate, not proof that the whole task is idle.

The design favors:

- independent logical-idle state per root thread, so concurrent Codex app, VS Code, and CLI tasks do not debounce one another;
- multiple local completion signals, with no single hook as a delivery single point of failure;
- a strict fail-closed default for uncertain root/rollout evidence;
- a short hook path with no required network request;
- one independent queue and worker per real Windows, WSL, Linux, or SSH host;
- durable at-least-once ntfy delivery after idle is confirmed;
- privacy-preserving defaults, especially `include_message: false`;
- a compact status-aware payload that is useful on a lock screen without repeating technical labels.

## Components

| Component | Responsibility |
| --- | --- |
| Codex modern `Stop` hook | Supplies an explicit session-stop candidate on standard input. Local rollout/database evidence still determines whether that session is a root or descendant. It is never sent directly. |
| Codex legacy root-level `notify` | Supplies `agent-turn-complete` as a compatibility/fallback candidate. |
| Incremental rollout watcher | Discovers recent local `task_complete` or `turn_aborted` records that hooks missed. |
| `notify-ntfy.ps1` / `notify-ntfy.py` | Normalizes signals, classifies roots/subagents, maintains pending probes, applies the idle gate, and delivers the outbox. |
| Windows scheduled task `CodexNtfyWatcher` | Runs the continuous Windows worker and rollout watcher. |
| Linux user unit `codex-ntfy.service` | Runs the continuous Python worker and rollout watcher when user systemd is available. |
| `notify-ntfy-wsl.sh` | Preserves WSL session paths and classification, prefers the Windows queue, and falls back to native Python state. |
| `ntfy-config.json` | Stores the private destination, credentials, idle policy, delivery policy, and privacy options. |
| `ntfy-state/` | Stores idle candidates, rollout cursors, network-ready events, receipts, dead letters, locks, and the bounded log. |

The modern hook format and review model are documented in [Codex Hooks](https://learn.chatgpt.com/docs/hooks). The legacy external notification is documented in [Codex advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications).

## Signal ingestion

Three sources feed the same record schema:

1. **Modern `Stop`.** The installer registers a managed `hooks.Stop` handler. The notifier reads its JSON from standard input, returns an empty JSON object as the hook result, classifies the session from local Codex state, and stores only an accepted root candidate. It intentionally ignores an event explicitly named `SubagentStop`; current Codex versions can also report descendant sessions through `Stop`, so the classifier remains mandatory.
2. **Legacy `notify`.** The existing root-level notification remains installed as a compatibility path. Its `agent-turn-complete` payload is normalized to the same candidate schema.
3. **Rollout watcher.** A continuous worker tails recent rollout JSONL files under its own `CODEX_HOME` and any explicitly registered roots using a persisted byte offset. It consumes only complete newline-terminated records and leaves an incomplete trailing line for the next scan. Existing cursors remain active across date boundaries; a slower bounded discovery finds recently modified old-date and archived rollouts. A local `task_complete` or `turn_aborted` can therefore reconstruct a candidate when a hook process was never launched. The Windows installer registers the selected WSL Codex and SQLite roots without globally guessing a newest session.

The watcher initializes an old rollout at its current end; it only replays a newly discovered rollout when the file was modified within `watch_initial_replay_seconds`. This avoids turning historical sessions into fresh notifications after installation.

All sources share the same deterministic key when Codex provides both thread and turn IDs. Consequently, the recovery paths normally converge on one local record.

## State machine

```text
Stop hook ──────────────┐
legacy notify ──────────┼──> normalize/classify ──> pending/
rollout watcher ────────┘                              |
                                                       v
                                             coalesce by root thread
                                                       |
                                                       v
                                           idle gate + final snapshot
                         ┌─────────────────────────────┼───────────────────────┐
                         | busy/unknown                | superseded            | idle
                         v                             v                       v
                    remain pending/              suppressed receipt       outbox/
                                                                               |
                                                                               v
                                                                       POST to ntfy
                                                                               |
                                                                               v
                                                                        sent receipt
```

Candidates are written through a private temporary file and an atomic filesystem operation. Simultaneous signals cannot expose a partial JSON record. Sharded cross-process mutation locks serialize changes to the same event key, so a worker using an older snapshot cannot overwrite or suppress newer authoritative `Stop` evidence.

## Logical-idle gate

For the default `strict` mode, a root candidate becomes network-ready only after these checks:

1. **Root classification.** A known subagent is suppressed. An unknown root/subagent classification stays pending in strict mode.
2. **Matching completion.** The root rollout contains `task_complete` or `turn_aborted` for the candidate turn.
3. **No later open turn.** A later `task_started` makes the root busy again.
4. **Goal not active.** With `goal_aware: true`, a goal whose status is `active` keeps the candidate pending. Terminal/non-running states such as `complete`, `paused`, `blocked`, `usage_limited`, and `budget_limited` do not block delivery.
5. **No active descendants.** The notifier traverses Codex `thread_spawn_edges` recursively and inspects child rollout lifecycles. A recent child ending in `task_started` or an unknown active tail keeps the root pending.
6. **Quiet window.** The matching rollout must remain unchanged for `idle_grace_seconds`.

The gate takes a second fresh snapshot while the candidate is still in `pending/`, immediately before the atomic promotion. Promotion closes that logical idle epoch: an `outbox/` record is a durable delivery and is never coalesced with a later user request or moved back to pending. Network retries preserve that boundary.

The notifier reads Codex SQLite databases read-only and enables query-only mode. Goal awareness selects only the `status` column; it does not query the goal objective. If the goal database is unavailable, a goal status persisted in the rollout can still be used. Missing goal information alone is not interpreted as an active goal.

Active descendants are bounded by `subagent_orphan_seconds`. The timeout prevents a crashed or abandoned child rollout from blocking its root forever; it is a liveness tradeoff rather than proof that the child succeeded.

## Detection modes

| Mode | Behavior | Intended use |
| --- | --- | --- |
| `strict` | Requires verifiable root classification and matching rollout completion. Incomplete evidence remains pending with no time-based fail-open. | Default; prioritize no premature notification. |
| `balanced` | Applies the same positive busy checks but may accept incomplete rollout evidence after `idle_probe_grace_seconds`. | Prefer eventual notification when local history may be unavailable. |
| `off` | Skips idle detection and queues each accepted completion signal immediately. | Compatibility, diagnostics, or intentionally per-turn behavior. |

`balanced` can produce a false final notification if evidence is still incomplete when its fallback expires. `off` restores the noisy per-turn behavior that version 2.4 and later are designed to avoid.

## Coalescing and technical-turn suppression

The worker compares pending records with the same root thread ID. Older pending candidates become compact `suppressed/` receipts with reason `superseded`; only the newest pending candidate can be promoted for that logical-idle epoch. A record already promoted to `outbox/` is immutable and is not coalesced with a later request. Coalescing is per root thread, never a global delay, so unrelated simultaneous tasks remain independent.

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

## Delivery guarantee

After logical idle is confirmed, the intended guarantee is **durable at-least-once delivery**:

1. the complete event is visible in `outbox/` before a worker attempts the request;
2. a successful 2xx response is followed by an atomic sent receipt;
3. only after the receipt exists is the outbox record removed;
4. transient failures update the record with attempt count and next-attempt time;
5. a process crash leaves either the outbox record, the receipt, or both in a recoverable state.

If ntfy accepts a request and the worker crashes or times out before the receipt is written, the worker retries with the same sequence ID. Exactly-once display cannot be guaranteed.

Strict idle detection changes the liveness boundary: a true completion can remain indefinitely in `pending/` when Codex no longer exposes enough local evidence. This is deliberate. The project prefers a withheld notification over a false “finished” notification in strict mode.

## Send-time payload assembly

Version 2.4.1 changes the wire presentation, not the logical-idle decision. The worker renders the ntfy JSON from the durable record and the current private configuration immediately before each delivery attempt.

The title is:

```text
Codex <status> · <task-or-project>
```

Recognized goal states take precedence and map `blocked`, `paused`, `usage_limited`, and `budget_limited` to `blocked`, `paused`, `usage limit`, and `budget limit`. Otherwise a `turn_aborted` completion maps to `stopped`; normal and other eligible completions map to `done`. The display value is the project directory by default. With `include_thread_title: true`, an available local task title replaces it.

The default plain-text body is a label-free sequence joined by ` · `:

```text
[final-message ·] [project-or-opted-in-path ·] origin · #thread8
```

The project appears in the body only when a distinct task title occupies the title. `include_full_path: true` instead adds the sanitized working directory as explicit extra context. The optional final-message excerpt appears only while the current `include_message` setting is true and is limited by `max_message_chars` (180 by default). With the default `markdown: false`, whitespace is normalized and the body is one line. An explicit Markdown opt-in can preserve message lines and is the exception to that one-line default.

The full ntfy `message` is truncated on a valid Unicode boundary to at most 3,500 UTF-8 bytes after context is assembled. This byte ceiling is independent of the configurable character limit for the excerpt.

Fresh configuration uses `tags: ["white_check_mark"]`; the templates add no duplicate emoji to title or body. Priority 3 is ntfy's default and is omitted from the outgoing JSON. The worker serializes `priority` only for a non-default value and serializes `markdown: true` only when Markdown was explicitly enabled and an optional message is present.

The send-time `include_message` check is a privacy gate for durable state. A record captured while the option was true can still contain final-message text locally, but changing it to false prevents that text from entering later network attempts, including retries of an outbox record. This does not erase the record or backups and cannot recall a request already in flight or accepted by ntfy.

## Concurrency and host topology

Hooks and rollout scans may run concurrently. Each state directory has one non-blocking worker lock, so a scheduled/service worker and an on-demand worker do not process the same host queue simultaneously. Sharded `mutation-locks/` additionally serialize hook/worker changes to the same deterministic event key without serializing unrelated chats.

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
4. if the bridge fails, native Python state owns the candidate and starts an on-demand worker.

The Windows continuous watcher does not enumerate arbitrary WSL distributions. `install.ps1 -WslDistro <name>` records that distribution's Codex root, optional distinct SQLite root, and source label in the private configuration. If neither WSL hook launches, the Windows worker can then recover the persisted completion directly; unregistered distributions remain isolated.

## Retry and failure policy

The delivery worker uses exponential backoff with jitter, capped by `retry_max_seconds` (900 seconds by default). A numeric `Retry-After` is honored within the same cap.

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
  mutation-locks/ sharded cross-process locks for same-key state changes
  worker.lock   per-state-directory worker lock
  notify.log    bounded operational log (one rotated generation)
```

Pending/outbox records can contain local paths, full thread and turn IDs, origin/classification metadata, and—only when opted in—the final assistant message. Watch cursors do not contain prompt bodies, but their rollout paths and thread IDs are private metadata. See [Security and privacy](security-and-privacy.md).

## Hook trust boundary

Installers write the managed `Stop` registration but do not grant it trust. Codex requires the operator to review the hook using `/hooks` before it runs. This avoids silently modifying a product security decision. Unrelated hook groups, handlers, and metadata are preserved.

Legacy `notify` and the rollout watcher are independent fallback sources, so an untrusted modern hook does not by itself disable all detection. A continuous worker is required for autonomous rollout recovery.

## Upstream boundaries

The project depends on local state emitted by Codex:

- modern hook availability and semantics are controlled by Codex;
- legacy notifications currently expose `agent-turn-complete` rather than a first-class “entire chat is permanently idle” event;
- rollout JSONL and local SQLite schemas can change upstream;
- an exceptionally large legacy Windows payload can fail before this notifier is launched;
- pure cloud tasks that do not mirror lifecycle state locally cannot be observed;
- rollout recovery requires a continuous worker and is scoped to its own plus explicitly registered Codex homes;
- ntfy retention, access control, sequence behavior, and client display are controlled by the chosen server and client.

The notifier is not an audit log, task scheduler, or proof that a model result was correct. It reports the best locally verifiable end of work for a root Codex task.
