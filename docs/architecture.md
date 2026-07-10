# Architecture

This document describes the delivery model implemented by version 2.3.0. Durable Codex ntfy notifier is an unofficial community project; it is not an OpenAI or ntfy component.

## Design goals

The notifier is designed for one narrow job: preserve a Codex turn-completion event locally, then deliver an ntfy notification even when the network is temporarily unavailable. It must remain safe when several VS Code windows finish at once and when Codex is actually running in Windows, WSL, or a Remote SSH host.

The design favors:

- a short hook path that performs no required network operation;
- one independent queue and worker per real host;
- deterministic deduplication when Codex supplies both a thread ID and a turn ID;
- bounded operational metadata and explicit retention;
- failure isolation, so one malformed event cannot block later events;
- privacy-preserving defaults, especially `include_message: false`.

## Components

| Component | Responsibility |
| --- | --- |
| Codex root-level `notify` hook | Invokes the platform notifier with the turn-completion JSON payload. |
| `notify-ntfy.ps1` / `notify-ntfy.py` | Parses and classifies the event, writes an atomic outbox record, and starts an on-demand worker if allowed. |
| Windows scheduled task `CodexNtfyWatcher` | Keeps one continuous Windows worker available. |
| Linux user unit `codex-ntfy.service` | Keeps one continuous Python worker available when user systemd is usable. |
| `notify-ntfy-wsl.sh` | Classifies a WSL event, prefers the Windows queue through the PowerShell bridge, and falls back to the WSL Python notifier. |
| `ntfy-config.json` | Stores the private destination, credentials, delivery policy, and privacy options. |
| `ntfy-state/` | Holds pending events, receipts, dead letters, the worker lock, and the bounded log. |

## Event flow

```text
Codex turn completes
        |
        v
notify hook parses and classifies the payload
        |
        +---- known subagent and suppression enabled ---> suppressed
        |
        v
atomic JSON record in ntfy-state/outbox
        |
        v
single host worker acquires worker.lock
        |
        +---- still a subagent -------------------------> suppressed receipt
        |
        +---- invalid/permanent failure ---------------> dead letter
        |
        +---- transient failure -----------------------> retry metadata in outbox
        |
        v
POST JSON to the configured ntfy server
        |
        v
sent receipt written, then outbox record removed
```

The hook writes the queue record through a private temporary file and an atomic filesystem operation. This prevents workers from observing partially written JSON and prevents simultaneous hooks from overwriting an existing record. The implementation flushes the temporary file before publishing it. Absolute persistence across sudden power loss still depends on the host filesystem and storage hardware.

## Record identity and deduplication

When both IDs are present, the event key is the SHA-256 digest of this stable identity:

```text
codex-ntfy/v1 | thread-id | turn-id
```

The same key names the outbox record and its eventual receipt. A concurrent or repeated hook invocation sees the existing outbox, sent, or suppressed file and does not enqueue a second record.

If either ID is absent, the notifier creates a random weak identity. The record is still delivered, but deterministic local deduplication is not possible. The `weak_identity` field records that limitation.

Every delivery attempt for one record reuses `codex-<first-32-hex-characters-of-key>` as its ntfy `sequence_id`. This is especially useful after an ambiguous timeout: the server may have accepted the first request even though the worker did not receive the response. Reusing the sequence ID reduces duplicate presentation on ntfy implementations that honor sequence updates, but it is not a distributed transaction.

## Delivery guarantee

The intended guarantee is **durable at-least-once delivery**:

1. the event is visible in the outbox before a worker attempts the request;
2. a successful 2xx response is followed by an atomic sent receipt;
3. only after the receipt exists is the outbox record removed;
4. transient failures update the record with an attempt count and next-attempt time;
5. a process crash leaves either the outbox record, the receipt, or both in a recoverable state.

There is an unavoidable ambiguity if the ntfy server accepts a request and the worker crashes or times out before writing the receipt. The worker retries with the same sequence ID. Consequently, exactly-once display cannot be guaranteed.

Sent and suppressed receipts expire after `sent_retention_days` (14 days by default). Replaying the same old Codex event after its receipt has expired can enqueue it again.

## Concurrency model

Hooks may run concurrently, but each state directory has one non-blocking worker lock. A scheduled/service worker and any hook-spawned on-demand worker therefore do not process the same host queue simultaneously. A contender that cannot acquire the lock exits successfully.

Queues are deliberately not shared between machines. Windows, each native Linux host, and each Remote SSH host own their state and worker. This avoids cross-host locking and makes offline recovery local. It also means two different hosts that independently receive logically identical events do not share receipts.

WSL is a special routing case:

1. the shell bridge asks the WSL Python implementation to classify the session without queueing and kicks a previously stranded native outbox worker when needed;
2. when Windows interop is available, it passes the payload and WSL session path to the Windows PowerShell notifier;
3. the Windows outbox and worker then own delivery;
4. if the bridge is unavailable, the WSL Python notifier owns the event and starts an on-demand worker.

## Retry and failure policy

The worker uses exponential backoff with jitter, capped by `retry_max_seconds` (900 seconds by default). A numeric `Retry-After` response is honored within the same cap.

| Failure | Default handling |
| --- | --- |
| DNS, connection, TLS, timeout, 5xx | Retry. |
| 401, 403, 408, 409, 425, 429 | Retry, because credentials, authorization, or rate limits may be repaired. |
| Redirect (3xx) | Reject and dead-letter; redirects are never followed. |
| Other 4xx | Treat as permanent and dead-letter. |
| Invalid queue JSON or schema | Store a sanitized minimal dead letter and continue. |
| `max_attempts` reached | Dead-letter the record. |

`max_attempts: 0` means retry transient failures indefinitely. Dead letters expire after `dead_retention_days` (30 days by default). Receipt and dead-letter cleanup runs when a worker starts.

## Subagent classification

Codex completion payloads do not always expose the session source. The notifier first checks explicit payload fields and then reads the local Codex rollout metadata for the matching thread. If metadata is still being written, an `unknown` event waits for `subagent_classification_grace_seconds` (8 seconds by default) and is classified again.

After the grace period, classification fails open: the event is delivered rather than silently lost. This can allow an occasional subagent notification after an upstream format change or missing rollout file. Setting `suppress_subagents: false` bypasses this filtering.

Suppressed records become small receipts in `ntfy-state/suppressed`; they are not sent to ntfy.

## Stored state

```text
ntfy-state/
  outbox/       complete pending records and retry metadata
  sent/         compact successful-delivery receipts
  suppressed/   compact subagent receipts
  dead/         invalid or permanently failed records
  worker.lock   per-state-directory worker lock
  notify.log    bounded operational log (one rotated generation)
```

The outbox and some dead letters can contain local paths, thread and turn IDs, origin metadata, and—only when opted in—the final assistant message. See [Security and privacy](security-and-privacy.md) before inspecting, copying, or reporting these files.

## Upstream boundaries

The project can only act after Codex launches the external hook:

- the hook currently reports turn completion, not every approval or input request; see [openai/codex#11808](https://github.com/openai/codex/issues/11808);
- on Windows, an extremely large command-line payload may prevent hook launch before this code runs; see [openai/codex#18309](https://github.com/openai/codex/issues/18309);
- changes to Codex payload or rollout metadata may require updates to parsing and subagent classification;
- ntfy retention, access control, sequence behavior, and client display are controlled by the selected ntfy server and client.

The hook is not an audit log, a task scheduler, or a guarantee that every Codex UI state will produce a notification.
