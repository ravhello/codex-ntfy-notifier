# Troubleshooting

Start in the environment where the coding agent actually runs. The Codex app on Windows, a local VS Code window, a WSL window, a Remote SSH window, Claude Code, and AudnCode can use different homes, hook files, lifecycle histories, and notifier state.

Do not post private config, pending/outbox records, rollout files, Codex databases, dead letters, backups, or raw logs. See [Security and privacy](security-and-privacy.md).

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

- `version` is `2.6.0` or a newer compatible release;
- `topic_configured` is `true`;
- `idle_detection_mode` is `strict` when intermediate notifications must never be sent;
- `goal_aware` and `watch_rollouts` are `true`;
- `pending_idle` is the number of root candidates waiting for completion evidence;
- `queued` is the network-ready outbox count;
- `watched_rollouts` becomes nonzero after the continuous worker observes local sessions;
- `dead_letters` is not increasing.

The explicit test bypasses idle detection and sends a real notification:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Test
```

```sh
python3 "$HOME/.codex/notify-ntfy.py" --test
```

Use `-Test`/`--test` only when publishing to the configured topic is acceptable. A successful test proves delivery, not hook or idle detection. The Windows/WSL installer removes only local queue/receipt records explicitly marked as synthetic tests during upgrade; it cannot retract a notification already accepted by ntfy.

## Understand pending versus queued

Version 2.4 and later have two intentionally separate stages:

| Doctor field | Directory | Meaning |
| --- | --- | --- |
| `pending_idle` | `ntfy-state/pending/` | A root completion candidate exists, but the logical-idle gate has not yet accepted it. |
| `queued` | `ntfy-state/outbox/` | Idle was confirmed and the event is waiting for ntfy delivery/retry. |

A short-lived `pending_idle > 0` is normal while the quiet window settles, a goal changes status, or a descendant finishes. A persistent value points to idle evidence; a persistent `queued > 0` points to delivery.

Do not move a pending record into `outbox/` manually. That bypasses the intended diagnosis and can create the premature alert this release is designed to prevent.

## Modern Stop hook does not run

The installer writes `~/.codex/hooks.json`, but Codex requires explicit review before a new hook can execute. The project does not modify the Codex trust store.

Verify that a `Stop` group is present without printing its command:

```powershell
$doc = Get-Content "$HOME\.codex\hooks.json" -Raw | ConvertFrom-Json
@($doc.hooks.Stop).Count
```

```sh
python3 -c 'import json, pathlib; p=pathlib.Path.home()/".codex"/"hooks.json"; print(len(json.loads(p.read_text()).get("hooks", {}).get("Stop", [])))'
```

Then open Codex in that same environment, run `/hooks`, inspect the managed command containing `notify-ntfy`, and approve it. Repeat for each WSL distribution or Remote SSH host on which the notifier was installed.

### Claude Code on Windows

Claude support is opt-in. Rerun the Windows installer with `-EnableClaudeCode`, then use `/hooks` inside Claude Code and verify managed groups for `Stop`, `StopFailure`, `UserPromptSubmit`, and `Notification`:

```powershell
.\install.ps1 -NoWsl -EnableClaudeCode
$settings = Get-Content "$HOME\.claude\settings.json" -Raw | ConvertFrom-Json
$settings.hooks.PSObject.Properties |
  Where-Object { $_.Name -in @('Stop','StopFailure','UserPromptSubmit','Notification') } |
  Select-Object Name,Value
```

The installer preserves unrelated Claude hooks. `Stop`, `StopFailure`, and `UserPromptSubmit` must show `async: false`; this preserves same-prompt lifecycle order and establishes the next prompt epoch before a fast `Stop`. Only the two managed `Notification` handlers are asynchronous. If `disableAllHooks` is `true`, Claude will not execute them. Claude Code 2.1.198 or newer is required for the complete hook set, including stable prompt identity and the `agent_completed` accelerator. The installer validates the newest executable for each detected `PATH`, Claude Desktop, VS Code, VS Code Insiders, and Cursor surface separately; one detected surface below the minimum blocks installation instead of being hidden by a newer one. The ordinary Claude Chat tab is not Claude Code and does not emit these lifecycle hooks.

For a `Stop`, both `background_tasks` and `session_crons` must be present and empty. A missing registry, a non-empty registry, `SubagentStop`, or an `agent_id` is intentionally ignored. For `/goal`, the newest transcript `attachment.goal_status` must prove a transition from active to achieved/failed; active/not-met remains pending, while manual clear is discarded without a notification. `idle_prompt` and `agent_completed` are optional fallbacks only when their non-empty `prompt_id` matches the candidate. `StopFailure` is handled separately because API errors replace `Stop`.

Reload app/CLI processes and VS Code windows that were already running during installation. If `Stop` remains untrusted, the legacy notification and rollout watcher can still detect completions, but the modern candidate is absent.

### AudnCode on Windows

AudnCode support is a separate opt-in. Install it alone or alongside Claude Code:

```powershell
.\install.ps1 -NoWsl -EnableAudnCode
# or
.\install.ps1 -NoWsl -EnableClaudeCode -EnableAudnCode
```

The installer follows a nonblank `CLAUDE_CONFIG_DIR`; otherwise it uses `~/.openclaude`. An explicit `-AudnCodeHome` overrides both. Install once per distinct configuration home when multiple AudnCode profiles or launchers are in use, and close only the AudnCode processes attached to the home being changed. The same Codex home and worker can serve all of them because runtime state remains keyed by AudnCode home and host identity.

Inspect the selected home's handler shape and active idle threshold without printing hook commands:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex')) # replace when customized
$ManagedScript = [IO.Path]::GetFullPath((Join-Path $CodexHome 'notify-ntfy.ps1'))
$AudnHome = if ([string]::IsNullOrWhiteSpace($env:CLAUDE_CONFIG_DIR)) {
  [IO.Path]::GetFullPath((Join-Path $HOME '.openclaude'))
} else {
  [IO.Path]::GetFullPath($env:CLAUDE_CONFIG_DIR)
}
$Utf8Strict = New-Object Text.UTF8Encoding($false, $true)
$SettingsPath = Join-Path $AudnHome 'settings.json'
$Settings = $Utf8Strict.GetString([IO.File]::ReadAllBytes($SettingsPath)) | ConvertFrom-Json
$ExpectedMatchers = [ordered]@{
  SessionStart = '^(startup|resume|clear)$'
  UserPromptSubmit = $null
  Stop = $null
  StopFailure = $null
  Notification = '^(idle_prompt|permission_prompt)$'
  PostToolUse = '^(Agent|AskUserQuestion|Bash|PowerShell|Monitor|TaskStop|KillShell|CronCreate|CronDelete|SendMessage)$'
  SubagentStart = $null
}
$ManagedQuotedScript = "'" + $ManagedScript.Replace("'", "''") + "'"
$ManagedQuotedHome = "'" + $AudnHome.Replace("'", "''") + "'"
$ManagedCommandPrefix = "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; & " +
  $ManagedQuotedScript + " -AudnCodeHook -ReadStdin -Origin 'AudnCode' -AudnCodeHome " + $ManagedQuotedHome
$ManagedEventPattern = '(?i)^' + [regex]::Escape($ManagedCommandPrefix) +
  '\s+-AudnCodeExpectedEvent\s+''(?:SessionStart|UserPromptSubmit|Stop|StopFailure|Notification|PostToolUse|SubagentStart)''$'
foreach ($EventName in @($ExpectedMatchers.Keys)) {
  $EventProperty = $Settings.hooks.PSObject.Properties[$EventName]
  if ($null -eq $EventProperty -or $EventProperty.Value -isnot [array]) {
    [pscustomobject]@{
      Event = $EventName; Present = $false; Matcher = '<missing>'; MatcherMatches = $false
      Async = $null; Timeout = $null; ManagedHandler = $false; TrustedExpectedEvent = $false
    }
    continue
  }
  foreach ($Group in @($EventProperty.Value)) {
    $MatcherProperty = if ($null -ne $Group) { $Group.PSObject.Properties['matcher'] } else { $null }
    $Matcher = if ($null -eq $MatcherProperty) { '<none>' } else { [string]$MatcherProperty.Value }
    $ExpectedMatcher = $ExpectedMatchers[$EventName]
    $MatcherMatches = if ($null -eq $ExpectedMatcher) {
      $null -eq $MatcherProperty
    } else {
      [string]::Equals($Matcher, [string]$ExpectedMatcher, [StringComparison]::Ordinal)
    }
    $HooksProperty = if ($null -ne $Group) { $Group.PSObject.Properties['hooks'] } else { $null }
    $Handlers = if ($null -ne $HooksProperty -and $HooksProperty.Value -is [array]) {
      @($HooksProperty.Value)
    } else { @() }
    if ($Handlers.Count -eq 0) {
      [pscustomobject]@{
        Event = $EventName; Present = $true; Matcher = $Matcher; MatcherMatches = $MatcherMatches
        Async = $null; Timeout = $null; ManagedHandler = $false; TrustedExpectedEvent = $false
      }
      continue
    }
    foreach ($Handler in $Handlers) {
      $Command = if ($null -ne $Handler) { [string]$Handler.command } else { '' }
      $ManagedHandler = $null -ne $Handler -and [string]$Handler.type -eq 'command' -and
        $Command -match $ManagedEventPattern
      $ExpectedCommand = $ManagedCommandPrefix + " -AudnCodeExpectedEvent '" + $EventName + "'"
      $AsyncProperty = if ($null -ne $Handler) { $Handler.PSObject.Properties['async'] } else { $null }
      $TimeoutProperty = if ($null -ne $Handler) { $Handler.PSObject.Properties['timeout'] } else { $null }
      [pscustomobject]@{
        Event = $EventName
        Present = $true
        Matcher = $Matcher
        MatcherMatches = $MatcherMatches
        Async = if ($null -eq $AsyncProperty) { $null } else { [bool]$AsyncProperty.Value }
        Timeout = if ($null -eq $TimeoutProperty) { $null } else { [int]$TimeoutProperty.Value }
        ManagedHandler = [bool]$ManagedHandler
        TrustedExpectedEvent = [bool]($ManagedHandler -and
          [string]::Equals($Command, $ExpectedCommand, [StringComparison]::OrdinalIgnoreCase))
      }
    }
  }
}

$IsTruthy = { param($Value) -not [string]::IsNullOrWhiteSpace($Value) -and $Value.Trim().ToLowerInvariant() -in @('1','true','yes','on') }
$OAuthSuffix = if (-not [string]::IsNullOrEmpty($env:CLAUDE_CODE_CUSTOM_OAUTH_URL)) {
  '-custom-oauth'
} elseif ($env:USER_TYPE -ceq 'ant' -and (& $IsTruthy $env:USE_LOCAL_OAUTH)) {
  '-local-oauth'
} elseif ($env:USER_TYPE -ceq 'ant' -and (& $IsTruthy $env:USE_STAGING_OAUTH)) {
  '-staging-oauth'
} else { '' }
$ConfigJson = Join-Path $AudnHome '.config.json'
$OpenClaudeJson = Join-Path $AudnHome ".openclaude${OAuthSuffix}.json"
$LegacyJson = Join-Path $AudnHome ".claude${OAuthSuffix}.json"
$GlobalPath = if (Test-Path -LiteralPath $ConfigJson -PathType Leaf) {
  $ConfigJson
} elseif ((Test-Path -LiteralPath $OpenClaudeJson -PathType Leaf) -or
          -not (Test-Path -LiteralPath $LegacyJson -PathType Leaf)) {
  $OpenClaudeJson
} else {
  $LegacyJson
}
if (-not (Test-Path -LiteralPath $GlobalPath -PathType Leaf)) { throw "AudnCode global config not found: $GlobalPath" }
($Utf8Strict.GetString([IO.File]::ReadAllBytes($GlobalPath)) | ConvertFrom-Json).messageIdleNotifThresholdMs
```

Expect hook shape 9 with one project-managed group for each of these seven synchronous events:

- `SessionStart` with only `^(startup|resume|clear)$`;
- `UserPromptSubmit` with no matcher;
- `Stop` with no matcher;
- `StopFailure` with no matcher;
- `Notification` with exactly `^(idle_prompt|permission_prompt)$`;
- `PostToolUse` with exactly `^(Agent|AskUserQuestion|Bash|PowerShell|Monitor|TaskStop|KillShell|CronCreate|CronDelete|SendMessage)$`;
- `SubagentStart` with no matcher.

For the single project-managed row under each event, expect `Present`, `MatcherMatches`, `ManagedHandler`, and `TrustedExpectedEvent` to be `true`, `Async` to be `false`, and `Timeout` to be `60`. The diagnostic reports booleans instead of printing the private command path. The default threshold is `1000` ms; a deliberate value from 1,000 through 60,000 can be installed with `-AudnCodeIdleThresholdMs`. If `disableAllHooks` is `true`, AudnCode will not execute the handlers.

AudnCode `Stop` is not a final signal. It creates or refreshes a candidate, which remains pending until a later matching `idle_prompt` proves that the foreground query loop returned. `StopFailure` is accepted only when one current-prompt assistant error record after the saved transcript cursor matches the hook error evidence. That proven failure bypasses only `idle_prompt`: settle time, ingress/lifecycle guards, runtime identity, command queue, background IDs, team/task state, sidechains, cron evidence, repeated snapshots, and the locked promotion boundary still apply. Do not add `agent_completed`; it is not used as AudnCode finality. A late idle or error from an older prompt fails closed through transcript cursor, prompt epoch, timestamp, and hook-process ordering.

`permission_prompt` is deliberately separate from completion. It notifies only when the current root transcript ends in exactly one unanswered `AskUserQuestion` tool call. A generic tool permission, copied/spoofed question text, nested or sidechain call, multiple unresolved questions, matching `tool_result`, or provider-error tail is ignored. Reopening or resuming the same question does not notify again; after the exact answer, `PostToolUse: AskUserQuestion` opens a new epoch so the eventual final result can notify once.

If a managed launcher exports `CODEX_NTFY_AUDNCODE_RECOVERY_MARKER`, revision 1 means recovery is still active and must not notify. Revision 2 `recovered` cancels that error; only revision 2 `exhausted` with the same transcript error UUID can continue. A missing or malformed declared marker, wrong path/schema/UUID/state/reason, PID/start/ancestry mismatch where the manager must be live, reparse component, unprotected/inherited/broadened ACL, revision rollback, rewritten terminal, or failure-UUID mismatch stays fail closed. A terminal marker may legitimately outlive its manager, and there is no age-based TTL. Absence of the environment variable keeps ordinary AudnCode behavior.

Managed hook stdin is strict UTF-8 and capped at 8 MiB. `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `SubagentStart` arm durable ingress before slower correlation as applicable, then transfer it to the exact host-runtime guard. Reaching the 60-second hook timeout, malformed/oversized input, an event mismatch, or an interrupted transfer can intentionally leave the host fail closed. This cannot protect an event for which AudnCode never launched the hook process.

Host exit is not a universal release. It can close only evidence proven to be process-bound to the exact PID/`startedAt`; sticky `SendMessage`, overlapping same-ID resumes, lost lifecycle history, detached work, retained team state, or ambiguous scheduler ownership remains fail closed. Durable cron reconciliation also requires causally ordered stable file snapshots and the exact native `scheduled_tasks.lock` owner/lifetime. Do not move a retained candidate into `outbox/` manually.

Foreground idle is still not whole-runtime idle. The gate binds the logical session to AudnCode's live PID and `startedAt`, preserving state across `/clear` and `/resume`. It waits at least 1.25 seconds after `idle_prompt` or proven `StopFailure`, then checks queue lineage, counted background incarnations, `SendMessage` uncertainty, session/team/custom tasks, retained team directories, CCR claims, cron lease/file evidence, and Ctrl+B sidechains. A non-lead team remains busy until `TeamDelete` removes its directory; `isActive: false` or membership removal is insufficient. The complete check runs three times before the locked outbox commit.

Evidence limits are fail-closed, not truncation-as-idle. Version 2.6.0 permits at most 512 MiB of queue transcripts across the runtime lineage, 4,096 relevant queue records, 1,048,576 characters per line, 65,536 characters per queue content field, 1 MiB per team/task JSON file, and 1,024 team members. If a valid workload exceeds one of these bounds, preserve the files and report a sanitized reproduction; do not move the candidate manually.

AudnCode hot-reloads ordinary settings but has no shared lock for `settings.json`. Close it before a first install or shape change, then verify shape 9 and reopen after `.codex-ntfy-hooks.json` is created or rotated. An identical reinstall preserves the generation. During upgrade the installer stops only verified orphan notifier hook processes from obsolete shapes; it does not terminate current-shape or still-parented processes. Do not copy the marker between homes. Hooks still require AudnCode workspace trust.

Known public-build limits can explain a permanently retained candidate. Successful/malformed `SendMessage` has no resolved recipient or durable consumption acknowledgement; overlapping resumed local agents can reuse one ID; `clearCommandQueue`, kill-all, and some Ctrl+B paths can omit the removed ID. `/ultrareview` requires the exact CCR/remote task identity and terminal record. Session-only cron firing omits its ID; only a later exact same-runtime `CronDelete` closes that incarnation. For durable cron work, a delete response or empty `scheduled_tasks.json` does not prove completion: causal stable file snapshots plus the exact scheduler lease owner/lifetime are required. These gaps can suppress a true final alert, but cannot produce an intermediate one.

## Legacy notification does not run

Confirm the relevant `config.toml` has one root-level `notify` entry:

```powershell
Select-String -Path "$HOME\.codex\config.toml" -Pattern '^\s*notify\s*='
```

```sh
grep -nE '^[[:space:]]*notify[[:space:]]*=' "$HOME/.codex/config.toml"
```

The key must appear before TOML table headers such as `[features]`. The legacy source is documented by OpenAI as `agent-turn-complete` in [advanced notification configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications).

Legacy `notify` is a fallback, not the finality decision. Its event should enter `pending/` and pass the same idle gate as `Stop`.

## No candidate appears at all

If `pending_idle`, `queued`, receipt counts, `watched_rollouts`, and the log never change:

1. confirm `CODEX_HOME` and, when used, `CODEX_SQLITE_HOME` in the Codex process environment;
2. reload the affected Codex app/VS Code/CLI process;
3. review the `Stop` hook with `/hooks`;
4. verify the root-level legacy `notify` entry;
5. confirm the continuous worker is running so rollout recovery can happen independently of a hook process;
6. confirm Codex writes a local rollout under the expected `sessions/` tree.

Pure cloud tasks that do not mirror lifecycle state into the local environment cannot be recovered by this notifier. It does not attach to a private app or VS Code status stream.

On Windows, an exceptionally large legacy payload can fail before the notifier process launches. The continuous rollout watcher can recover it only when a local `task_complete` or `turn_aborted` was persisted.

## Notifications are unusually late

Version 2.4.3 keeps the persistent Windows local scanner off the historical recursive path: it follows active and recently resumed rollout paths from Codex's read-only SQLite index and checks hot current-day files. This removes the repeated full-tree walk that reached 23 GB in the installation where the regression was found. UNC/WSL recovery runs in a different timeout-bounded process, and large local rollout lifecycle checks use a native streaming summary instead of line-by-line PowerShell JSON replay. A full recursive archive walk occurs only when an operator explicitly runs the manual all-scope scanner. Remote SSH installs have their own host-local worker and queue, so they cannot block the local Windows delivery path.

For AudnCode, normal latency includes its configured 1,000 ms idle notification threshold, the notifier's fixed minimum 1.25-second settle, up to three local evidence passes, and worker scheduling. A hook's `timeout: 60` is an upstream safety ceiling, not a normal delay target. Missing or ambiguous evidence has no timed fail-open, so a permanently pending AudnCode candidate is a diagnostic condition rather than a reason to lower the idle threshold.

Check the scheduled task action and scanner health without printing private state:

```powershell
(Get-ScheduledTask -TaskName CodexNtfyWatcher).Actions | Select-Object Execute, Arguments
Get-Content "$HOME\.codex\ntfy-state\watch-health.json" -Raw | ConvertFrom-Json | Select-Object status, last_completed_at, duration_ms
Get-Content "$HOME\.codex\ntfy-state\remote-watch-health.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json | Select-Object status, last_completed_at, duration_ms
```

The task should execute `wscript.exe` with `watch-codex-ntfy-hidden.vbs`; that VBS supervises `notify-ntfy.ps1` directly and avoids two cold PowerShell launcher starts. Reinstall the current release if the action still points to the old PowerShell wrapper. A slow or timed-out remote health record should not delay a healthy local scan or an already queued delivery. Maintenance waits at least 60 seconds and starts only after delivery and the applicable scanners report readiness, so cleanup should not be the startup bottleneck.

## A candidate remains pending

Inspect the sanitized log first. Common idle reasons are:

| Reason | Meaning | Action |
| --- | --- | --- |
| `settling` | The rollout has not been quiet for `idle_grace_seconds`. | Wait briefly. |
| `turn-active` | A later `task_started` has no matching completion yet. | Let Codex finish or abort that turn. |
| `goal-active` | The root goal is still `active`. | Let the goal reach a non-running status. |
| `claude-goal-active` | Claude `/goal` still has a newest active/not-met marker. | Let the same goal reach achieved/failed, or clear it intentionally. |
| `claude-goal-awaiting-finality` | Claude goal evidence is missing/malformed and no matching prompt idle fallback has arrived. | Verify the transcript path, Claude version, and managed prompt/Notification hooks. |
| `audncode-awaiting-idle` | AudnCode produced a normal `Stop`, but no later matching `idle_prompt` has passed session/epoch/process-order checks. | Let the query loop return; verify synchronous managed hooks, trusted expected-event arguments, workspace trust, transcript path, and the 1,000 ms threshold. A valid `StopFailure` follows the separate transcript-error proof path. |
| `audncode-background-settling` | The fixed 1.25-second post-idle window has not elapsed. | Wait; this floor applies even when general idle grace is zero. |
| `audncode-background-tools-active` | At least one runtime-scoped Agent/Bash/PowerShell/Monitor or Ctrl+B sidechain ID lacks terminal proof. | Let it finish, or use a single-ID TaskStop/KillShell path that emits a correlatable ID. |
| `audncode-command-queue-active` | Persisted `queue-operation` records still have outstanding work across the current runtime's session lineage. | Let queued commands dequeue/remove/pop; `popAll` is recognized even from a later `/clear`/`/resume` lineage session when it is logged. |
| `audncode-teammates-active` | A matching non-lead team directory still exists. | Let AudnCode complete `TeamDelete`. Editing `isActive`, removing members, or emptying the list is not terminal proof. |
| `audncode-goal-tasks-active` | A session, team, or valid custom task list still contains `pending`/`in_progress`. | Complete the explicit task records. |
| `audncode-awaiting-terminal-proof` | A background/sidechain ID has a terminal claim but no correlated native external main-session `<task-notification>` after the saved cursor. | Let AudnCode persist either accepted `user`-origin or `queued_command` attachment form; pasted or sidechain-local content is not proof. |
| `audncode-local-agent-ui-queue-unverifiable` | A successful/malformed `SendMessage` left RAM-only delivery or recipient identity unprovable. | Do not forge a terminal record. Resolve the upstream work, then start a fresh host/session; preserve sanitized evidence for a bug report. |
| `audncode-remote-*` | A `/ultrareview` CCR claim lacks its exact identity sidecar or matching remote terminal proof. | Let the same remote task finish; deletion, another prompt, or another task's terminal does not release it. |
| `audncode-cron-*` | Scheduler file/lease history is live, non-causal, reused, missing, malformed, or unstable. | A same-runtime `CronDelete` can close only its exact session-only incarnation. For durable work, let the exact native owner exit and its stable file/lock transition settle; a delete response or empty task file alone is insufficient. |
| `audncode-*-unverifiable` | Host marker, runtime registry, queue, team/task, transcript, or terminal evidence is malformed, unstable, or ambiguous. | Preserve the files, confirm the current PID/session and v2.6.0 hooks, then inspect sanitized logs. The gate intentionally fails closed. |
| `subagents-active` | At least one descendant rollout still looks active. | Let the descendant finish; check stale-child policy if it crashed. |
| `probe-incomplete` | Matching local rollout evidence is missing or unreadable. | Verify the real Codex/session paths and upstream state format. |

### Strict mode suppresses unverifiable evidence

This is intentional for unresolved root classification or missing matching rollout completion. `strict` retries unknown evidence with exponential intervals capped by `unknown_retry_max_seconds`, then writes an `unverifiable` suppressed receipt after `idle_probe_grace_seconds`. It never uses the timeout as permission to notify. An epoch-anchored Claude candidate that cannot pass the locked session/prompt-epoch check immediately before promotion is terminalized locally with receipt reason `claude-session-unverifiable` rather than retried forever; it does not create an ntfy request.

Check:

- the event thread ID exists in the same environment’s local Codex state;
- the matching rollout file is readable by the worker account;
- the worker receives the correct `CODEX_HOME`/`CODEX_SQLITE_HOME`;
- the installed notifier version matches the current Codex rollout format;
- history/session cleanup did not remove the rollout before the candidate could be verified.

If that environment cannot retain usable rollout evidence, `idle_detection_mode: "balanced"` permits a fallback after `idle_probe_grace_seconds`. This explicitly increases the risk of an intermediate notification. `off` disables idle gating and should not be used when final-only alerts are required.

### Goal stays active

With `goal_aware: true`, `active` blocks notification. Other observed statuses, including `complete`, `paused`, `blocked`, `usage_limited`, and `budget_limited`, are non-running and do not block.

These terminal states still control whether a task is running, but since version 2.4.2 the notification title deliberately omits lifecycle status to save space. The title identifies only the task or project.

The notifier reads only the goal status. If a stale upstream goal is permanently `active`, correct/finish the task state. Setting `goal_aware: false` is possible but weakens the final-only guarantee.

### A child appears stuck

Descendants are discovered recursively from local Codex spawn edges and checked through their rollouts. A recent `task_started` child blocks its root. After `subagent_orphan_seconds` (1800 seconds by default), an unchanged child is considered orphaned so one abandoned rollout cannot block forever.

Lowering that timeout can notify while a genuinely long-running child is still active. Increasing it delays recovery from crashed children.

## Intermediate notifications still arrive

Confirm all of the following:

- doctor reports version 2.6.0+ and `idle_detection_mode: "strict"`;
- the alert comes from this installation/topic rather than an older custom hook or another notifier;
- no duplicate/legacy managed notifier handlers remain under `UserPromptExpansion`, `SubagentStop`, or unexpected hook groups; the single managed `UserPromptSubmit` is intentional;
- only one intended `notify-ntfy` `Stop` group exists per environment;
- AudnCode has exactly the seven shape-9 synchronous event groups, 60-second timeouts, and matchers listed above, with no old direct-publish or `agent_completed` handler, and every command has the correct trusted expected event;
- every worker was restarted after upgrade;
- `suppress_subagents` and `suppress_technical_turns` are `true`;
- Windows, WSL, and SSH environments are not publishing to the same topic through separate old installations.

Per-thread coalescing writes older candidates to `suppressed/` with reason `superseded`; this includes a predecessor followed by a later open task. Known descendants use reason `subagent`, non-user-facing legacy/watcher turns use `technical-turn`, and evidence still unknown after the strict probe window uses `unverifiable`. Those receipts are expected and are not sent.

If `balanced` is enabled, a fallback after `idle_probe_grace_seconds` can be premature. Switch back to `strict`.

## The task finished but no notification arrived

Separate detection from delivery:

1. If `pending_idle > 0`, use the pending guidance above.
2. If `queued > 0`, inspect worker/network/authentication.
3. If `sent_receipts` increased, ntfy accepted the event; inspect topic, client subscription, client privacy, and server retention.
4. If only `suppressed` increased, inspect the compact reason: the candidate was classified as subagent, technical, superseded, or unverifiable.
5. If no state changed, verify hooks, continuous watcher, and local rollout availability.

A terminal goal status makes an otherwise matching candidate eligible; it does not synthesize a missing thread/turn identity on its own. The rollout watcher is the recovery source for a persisted completion whose hook signal was lost.

## Events queue but do not send

A nonzero `queued` count means idle confirmation and persistence succeeded. Do not delete `outbox/` during a normal outage.

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

An on-demand worker drains known queues but cannot continuously discover a hook that never ran. For missed-hook recovery, keep the scheduled task or systemd user service active.

Only one worker per state directory acquires `worker.lock`. A second worker exiting immediately is expected.

On Windows, the scheduled task action should be `wscript.exe //B //Nologo ...watch-codex-ntfy-hidden.vbs`. The hidden VBS is the supervisor and directly launches the continuous notifier; an action that still starts `watch-codex-ntfy.ps1` through PowerShell is stale and should be replaced by rerunning the current installer.

### Authentication or authorization fails

HTTP 401 and 403 are retryable because credentials or server policy can be repaired. With `max_attempts: 0`, the record stays in the outbox indefinitely. Correct the token/user/password and restart or wait for the worker; do not create replacement Codex events.

Check JSON syntax without printing values:

```powershell
Get-Content "$HOME\.codex\ntfy-config.json" -Raw | ConvertFrom-Json | Out-Null
```

```sh
python3 -m json.tool "$HOME/.codex/ntfy-config.json" >/dev/null
```

Environment overrides must exist in the worker’s environment. A systemd service does not automatically inherit variables exported later in an interactive shell.

### Insecure-auth refusal

`refusing to send ntfy credentials over an insecure connection` means token/basic credentials target non-HTTPS, non-loopback HTTP. Configure TLS or a trusted local proxy. Enable `allow_insecure_auth` only for a separately protected transport whose risk is understood.

### Redirect rejected

Redirects are never followed. Configure `server` as the final publishing endpoint. A 3xx response is a permanent failure and moves the event to `dead/`.

### Network, TLS, rate limit, or server failure

DNS, connection, timeout, TLS, and 5xx failures retry with exponential backoff. HTTP 429 and numeric `Retry-After` are retried. Test from the same host and worker account; local browser success does not prove remote reachability.

## Dead letters

`dead/` contains malformed pending/outbox files, redirects, most non-retryable 4xx failures, and events that reached a positive `max_attempts`. The default retention is 30 days.

Fix the cause first. Do not blindly move malformed state into `outbox/`. A complete reviewed outbox record may be replayed privately, but it can contain sensitive content and may already have reached ntfy after an ambiguous failure.

Deleting a dead letter does not remove any server-side notification.

## Duplicate notifications

Local deduplication requires both Codex thread and turn IDs. A weak identity cannot be deterministically deduplicated.

If ntfy accepts a request but the local receipt is not written, the retry reuses the same `sequence_id`. Exactly-once display is still not guaranteed.

Queues and receipts are per state directory. Two hosts or two separate `CODEX_NTFY_STATE_DIR` values do not coordinate. Coalescing prevents several candidates for one root thread from becoming several final notifications on the same state store; it does not merge independent hosts.

## WSL-specific checks

Normal routing:

```text
WSL hooks/rollout -> WSL classifier -> powershell.exe bridge -> Windows pending/outbox worker
```

When interop fails, the WSL Python notifier owns native pending/outbox state.

Inside the affected distribution:

```sh
test -x "$HOME/.codex/notify-ntfy-wsl.sh"
test -x "$HOME/.codex/notify-ntfy.py"
test -f "$HOME/.codex/hooks.json"
command -v python3
command -v powershell.exe || true
printf '%s\n' "$WSL_DISTRO_NAME"
```

Run Windows and WSL doctor commands separately. Install into the exact distribution name returned by `wsl.exe -l -q`; configuring one distribution does not configure another. Review `/hooks` inside the affected WSL Codex environment.

The Windows scheduled watcher covers its Windows `CODEX_HOME` plus WSL roots registered by `install.ps1 -WslDistro`. Its persistent local path obtains recent rollout paths from the Codex SQLite index and hot current-day directories without repeatedly walking the complete local session archive. UNC/WSL roots run in an isolated one-shot scanner, inspect their own remote cursors independently, and are terminated after `watch_remote_timeout_seconds`, so an unavailable distro cannot stall local recovery or delivery. Check the private config's `watch_roots` entries when both WSL hooks were missed. Each entry must point to the correct distribution Codex root and, when different, `sqlite_path`; reinstall that distribution to refresh them. Unregistered distributions are intentionally not crawled.

## Remote SSH checks

Open a terminal in the same VS Code Remote SSH context and run the remote platform’s doctor. Confirm:

- the SSH alias points to the intended machine;
- Codex and the installer use the same remote account;
- Python 3.10+ exists on Linux;
- remote hooks, config, rollout state, and worker are under the intended remote home;
- `/hooks` was reviewed remotely;
- the remote host can reach the ntfy server directly.

A Linux user service may stop after logout unless the host keeps the user manager alive:

```sh
loginctl show-user "$USER" -p Linger
```

Without a persistent service, hook-driven on-demand delivery still works, but autonomous rollout scanning between hooks is not guaranteed.

## Notification content looks wrong

- Since 2.4.2, the JSON title is only `<conversation-or-project>`. With the default single `white_check_mark` tag, the complete visible title is one status emoji plus that title. Provider names, `done`, model names, lifecycle text, and duplicate emoji are intentionally absent.
- The default body is one plain-text line, `<origin> · #<thread8>`, without `Project:`, `Source:`, or `Thread:` labels. A project is prepended only when a distinct task title occupies the title, or a sanitized path is added when full-path output is enabled.
- Keep `include_message: false` unless the final assistant response should be captured and sent. With it enabled, the excerpt is prepended to the context and `max_message_chars` defaults to 180.
- Keep `include_full_path: false` to avoid sending the sanitized working-directory path.
- Keep `include_thread_title: false` unless a prompt-derived local task title is acceptable.
- Every outgoing payload has one tag: `warning` for any terminal non-success, otherwise the first valid configured member or `white_check_mark`. Legacy multi-tag strings/arrays normalize to the first valid member. The templates do not duplicate its emoji in title or body.
- The full ntfy `message` is always at most 3,500 UTF-8 bytes. `max_message_chars` controls the optional excerpt, not that final byte ceiling.
- Version 2.6.0 decodes hook/config/state/title data as strict UTF-8/Unicode, normalizes display text to NFC, removes unsafe control/bidi formatting, and truncates only between complete display clusters. Titles are at most 60 clusters and 240 UTF-8 bytes; bodies are at most 3,500 bytes. Mojibake or U+FFFD indicates stale/invalid input or an older duplicate handler; remove the stale registration rather than adding substitutions.

Redaction is best-effort. If sensitive data was published, rotate credentials/topic access and follow the ntfy server/client deletion procedure; configuration changes cannot recall it.

### Prevent queued message content from being sent

Set `include_message` to `false` in the private config. The worker checks this setting again when it builds every network request, so final-message text captured while the option was previously enabled is omitted from pending and outbox deliveries, including retries.

For an urgent change, stop the continuous worker before editing and avoid launching another hook-driven worker until the config is saved. This closes the avoidable race with a new request, but cannot cancel a request already in flight. Existing queue/dead-letter files and backups may still contain the captured text; the opt-out prevents transmission, not local erasure. It also cannot recall a notification already accepted by ntfy.

## Safe issue checklist

For a public issue, provide only:

- notifier and platform versions;
- Codex app/VS Code/CLI plus Windows/WSL/Linux/SSH topology;
- sanitized doctor fields;
- whether candidate, idle gate, outbox, worker, and server stages were reached;
- sanitized log lines with topics, URLs, usernames, hostnames, paths, IDs, and content replaced.

For a possible vulnerability or credential leak, use the private process in [SECURITY.md](../SECURITY.md).
