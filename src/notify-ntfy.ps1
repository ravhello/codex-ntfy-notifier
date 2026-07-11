[CmdletBinding()]
param(
  [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
  [string[]]$NotificationArgs,
  [string]$Origin,
  [string]$SessionCodexHome,
  [string]$SessionSqliteHome,
  [ValidateSet('', 'root', 'subagent', 'unknown')]
  [string]$SessionClassification = '',
  [switch]$ReadStdin,
  [switch]$HookEvent,
  [switch]$BridgeFallback,
  [switch]$Worker,
  [switch]$Continuous,
  [switch]$NoSpawn,
  [switch]$Doctor,
  [switch]$Test,
  [int]$PollSeconds = 2,
  [double]$RetryBaseSeconds = 5
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ScriptVersion = '2.4.2'
$MaxNtfyMessageBytes = 3500
$MiddleDot = [char]0x00B7
$Ellipsis = [char]0x2026
$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
  $PSScriptRoot
} else {
  $env:CODEX_HOME
}
$ConfigPath = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_CONFIG)) {
  Join-Path $CodexHome 'ntfy-config.json'
} else {
  $env:CODEX_NTFY_CONFIG
}
$StateRoot = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_STATE_DIR)) {
  Join-Path $CodexHome 'ntfy-state'
} else {
  $env:CODEX_NTFY_STATE_DIR
}
$PendingDir = Join-Path $StateRoot 'pending'
$OutboxDir = Join-Path $StateRoot 'outbox'
$WatchDir = Join-Path $StateRoot 'watch'
$SentDir = Join-Path $StateRoot 'sent'
$SuppressedDir = Join-Path $StateRoot 'suppressed'
$DeadDir = Join-Path $StateRoot 'dead'
$WorkerLockPath = Join-Path $StateRoot 'worker.lock'
$LogPath = Join-Path $StateRoot 'notify.log'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Ensure-RuntimeDirectories {
  foreach ($path in @($StateRoot, $PendingDir, $OutboxDir, $WatchDir, $SentDir, $SuppressedDir, $DeadDir)) {
    if (-not (Test-Path -LiteralPath $path)) {
      New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
  }
}

function Write-RuntimeLog {
  param([string]$Message)

  try {
    Ensure-RuntimeDirectories
    if ((Test-Path -LiteralPath $LogPath) -and (Get-Item -LiteralPath $LogPath).Length -gt 1048576) {
      $oldLog = "$LogPath.1"
      if (Test-Path -LiteralPath $oldLog) {
        Remove-Item -LiteralPath $oldLog -Force -ErrorAction SilentlyContinue
      }
      Move-Item -LiteralPath $LogPath -Destination $oldLog -Force
    }
    $timestamp = [DateTimeOffset]::Now.ToString('yyyy-MM-dd HH:mm:ss zzz')
    Add-Content -LiteralPath $LogPath -Value "[$timestamp] $Message" -Encoding UTF8
  } catch {
    # Notifications must never make Codex fail.
  }
}

function Get-ObjectValue {
  param(
    [object]$Object,
    [string]$Name,
    [object]$Default = $null
  )

  if ($null -eq $Object) {
    return $Default
  }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property -or $null -eq $property.Value) {
    return $Default
  }
  return $property.Value
}

function Get-FirstObjectValue {
  param(
    [object]$Object,
    [string[]]$Names
  )

  foreach ($name in $Names) {
    $value = Get-ObjectValue -Object $Object -Name $name
    if ($null -ne $value -and -not [string]::IsNullOrWhiteSpace([string]$value)) {
      return $value
    }
  }
  return $null
}

function Get-Sha256Hex {
  param([string]$Value)

  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = $Utf8NoBom.GetBytes($Value)
    return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
  } finally {
    $sha.Dispose()
  }
}

function ConvertTo-CompactJson {
  param([object]$Value)
  return ($Value | ConvertTo-Json -Compress -Depth 20)
}

function Write-JsonAtomic {
  param(
    [string]$Path,
    [object]$Value,
    [switch]$NoOverwrite
  )

  $directory = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
  }
  $tempPath = Join-Path $directory ('.{0}.{1}.{2}.tmp' -f (Split-Path -Leaf $Path), $PID, [Guid]::NewGuid().ToString('N'))
  [System.IO.File]::WriteAllText($tempPath, (ConvertTo-CompactJson $Value), $Utf8NoBom)
  try {
    if ($NoOverwrite) {
      [System.IO.File]::Move($tempPath, $Path)
    } elseif (Test-Path -LiteralPath $Path) {
      try {
        [System.IO.File]::Replace($tempPath, $Path, $null)
      } catch {
        Move-Item -LiteralPath $tempPath -Destination $Path -Force
      }
    } else {
      [System.IO.File]::Move($tempPath, $Path)
    }
  } finally {
    if (Test-Path -LiteralPath $tempPath) {
      Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
  }
}

function Read-JsonFile {
  param([string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    return $null
  }
  return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Get-NotificationTags {
  param([object]$Config)

  $property = $Config.PSObject.Properties['tags']
  $raw = if ($null -eq $property -or $null -eq $property.Value) {
    @('white_check_mark')
  } else {
    $property.Value
  }
  $items = if ($raw -is [string]) {
    @($raw -split ',')
  } elseif ($raw -is [System.Collections.IEnumerable]) {
    @($raw)
  } else {
    throw 'tags must be an array of strings or a comma-separated string'
  }
  $seen = New-Object 'System.Collections.Generic.HashSet[string]'
  $result = New-Object 'System.Collections.Generic.List[string]'
  foreach ($item in $items) {
    if ($item -isnot [string]) {
      throw 'tags must be an array of strings or a comma-separated string'
    }
    $tag = $item.Trim()
    if ([string]::IsNullOrWhiteSpace($tag)) {
      if ($items.Count -eq 1 -and $raw -is [string]) { continue }
      throw 'tags must contain non-empty strings of at most 32 characters without whitespace'
    }
    if ($tag.Length -gt 32 -or [regex]::IsMatch($tag, '\s')) {
      throw 'tags must contain non-empty strings of at most 32 characters without whitespace'
    }
    if ($seen.Add($tag)) { $result.Add($tag) }
  }
  if ($result.Count -gt 3) { throw 'tags must contain at most 3 values' }
  foreach ($tag in $result) { Write-Output $tag }
}

function Get-Config {
  $fileConfig = Read-JsonFile -Path $ConfigPath
  if ($null -eq $fileConfig) {
    $fileConfig = [pscustomobject]@{}
  }

  $server = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_SERVER)) {
    [string](Get-ObjectValue $fileConfig 'server' 'https://ntfy.sh')
  } else { $env:CODEX_NTFY_SERVER }
  $topic = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_TOPIC)) {
    [string](Get-ObjectValue $fileConfig 'topic' '')
  } else { $env:CODEX_NTFY_TOPIC }
  $token = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_TOKEN)) {
    [string](Get-ObjectValue $fileConfig 'token' '')
  } else { $env:CODEX_NTFY_TOKEN }
  $user = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_USER)) {
    [string](Get-ObjectValue $fileConfig 'username' '')
  } else { $env:CODEX_NTFY_USER }
  $password = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_PASSWORD)) {
    [string](Get-ObjectValue $fileConfig 'password' '')
  } else { $env:CODEX_NTFY_PASSWORD }
  $idleDetectionMode = ([string](Get-ObjectValue $fileConfig 'idle_detection_mode' 'strict')).Trim().ToLowerInvariant()
  if ($idleDetectionMode -notin @('strict', 'balanced', 'off')) {
    throw "idle_detection_mode must be strict, balanced, or off"
  }
  $priority = [int](Get-ObjectValue $fileConfig 'priority' 3)
  if ($priority -lt 1 -or $priority -gt 5) {
    throw 'priority must be between 1 and 5'
  }
  $maxMessageChars = [int](Get-ObjectValue $fileConfig 'max_message_chars' 180)
  if ($maxMessageChars -lt 32 -or $maxMessageChars -gt 3000) {
    throw 'max_message_chars must be between 32 and 3000'
  }
  $tags = @(Get-NotificationTags -Config $fileConfig)
  $watchRoots = @()
  $seenWatchRoots = @{}
  foreach ($entry in @(Get-ObjectValue $fileConfig 'watch_roots' @())) {
    $path = ''
    $rootOrigin = ''
    $sqliteHome = ''
    if ($entry -is [string]) {
      $path = [string]$entry
    } elseif ($null -ne $entry) {
      $path = [string](Get-ObjectValue $entry 'path' '')
      $rootOrigin = [string](Get-ObjectValue $entry 'origin' '')
      $sqliteHome = [string](Get-FirstObjectValue $entry @('sqlite_path', 'sqlite_home', 'session_sqlite_home'))
    }
    $path = $path.Trim()
    if ([string]::IsNullOrWhiteSpace($path)) { continue }
    $rootKey = $path.ToLowerInvariant()
    if ($seenWatchRoots.ContainsKey($rootKey)) { continue }
    $seenWatchRoots[$rootKey] = $true
    if ([string]::IsNullOrWhiteSpace($sqliteHome)) { $sqliteHome = $path }
    $watchRoots += [pscustomobject]@{
      path = $path
      session_codex_home = $path
      session_sqlite_home = $sqliteHome.Trim()
      origin = Sanitize-NotificationText -Text $rootOrigin -MaxLength 100
    }
  }
  $configuredWorkerSqlite = [string](Get-ObjectValue $fileConfig 'worker_sqlite_path' '')
  $workerSqliteConfigured = -not [string]::IsNullOrWhiteSpace($configuredWorkerSqlite) -or
    -not [string]::IsNullOrWhiteSpace($env:CODEX_SQLITE_HOME)
  $workerSqlitePath = if (-not [string]::IsNullOrWhiteSpace($env:CODEX_SQLITE_HOME)) {
    $env:CODEX_SQLITE_HOME
  } elseif (-not [string]::IsNullOrWhiteSpace($configuredWorkerSqlite)) {
    $configuredWorkerSqlite.Trim()
  } else {
    $CodexHome
  }

  return [pscustomobject]@{
    server = $server.TrimEnd('/')
    topic = $topic.Trim('/')
    token = $token
    username = $user
    password = $password
    allowInsecureAuth = [bool](Get-ObjectValue $fileConfig 'allow_insecure_auth' $false)
    priority = $priority
    tags = @($tags)
    maxMessageChars = $maxMessageChars
    includeMessage = [bool](Get-ObjectValue $fileConfig 'include_message' $false)
    includeThreadTitle = [bool](Get-ObjectValue $fileConfig 'include_thread_title' $false)
    markdown = [bool](Get-ObjectValue $fileConfig 'markdown' $false)
    includeFullPath = [bool](Get-ObjectValue $fileConfig 'include_full_path' $false)
    suppressSubagents = [bool](Get-ObjectValue $fileConfig 'suppress_subagents' $true)
    subagentClassificationGraceSeconds = [double](Get-ObjectValue $fileConfig 'subagent_classification_grace_seconds' 8)
    idleDetectionMode = $idleDetectionMode
    idleGraceSeconds = [double](Get-ObjectValue $fileConfig 'idle_grace_seconds' 1.5)
    idleProbeGraceSeconds = [double](Get-ObjectValue $fileConfig 'idle_probe_grace_seconds' 30)
    goalAware = [bool](Get-ObjectValue $fileConfig 'goal_aware' $true)
    goalPollSeconds = [double](Get-ObjectValue $fileConfig 'goal_poll_seconds' 1)
    suppressTechnicalTurns = [bool](Get-ObjectValue $fileConfig 'suppress_technical_turns' $true)
    watchRollouts = [bool](Get-ObjectValue $fileConfig 'watch_rollouts' $true)
    watchScanSeconds = [double](Get-ObjectValue $fileConfig 'watch_scan_seconds' 2)
    watchInitialReplaySeconds = [double](Get-ObjectValue $fileConfig 'watch_initial_replay_seconds' 15)
    watchDiscoverySeconds = [double](Get-ObjectValue $fileConfig 'watch_discovery_seconds' 60)
    watchRoots = @($watchRoots)
    workerSqlitePath = $workerSqlitePath
    workerSqliteConfigured = $workerSqliteConfigured
    subagentOrphanSeconds = [double](Get-ObjectValue $fileConfig 'subagent_orphan_seconds' 1800)
    timeoutSeconds = [int](Get-ObjectValue $fileConfig 'timeout_seconds' 12)
    maxAttempts = [int](Get-ObjectValue $fileConfig 'max_attempts' 0)
    retryMaxSeconds = [double](Get-ObjectValue $fileConfig 'retry_max_seconds' 900)
    sentRetentionDays = [int](Get-ObjectValue $fileConfig 'sent_retention_days' 14)
    deadRetentionDays = [int](Get-ObjectValue $fileConfig 'dead_retention_days' 30)
  }
}

function Limit-NotificationCharacters {
  param(
    [AllowEmptyString()][string]$Value,
    [int]$MaxLength
  )

  if ([string]::IsNullOrEmpty($Value)) { return '' }
  $indexes = [Globalization.StringInfo]::ParseCombiningCharacters($Value)
  if ($indexes.Count -le $MaxLength) { return $Value }
  if ($MaxLength -le 0) { return '' }
  if ($MaxLength -eq 1) { return [string]$Ellipsis }
  $keep = $MaxLength - 1
  $prefix = $Value.Substring(0, $indexes[$keep]).TrimEnd()
  $boundary = [Math]::Max($prefix.LastIndexOf(' '), $prefix.LastIndexOf("`n"))
  if ($boundary -ge [int][Math]::Floor($keep * 0.7)) {
    $prefix = $prefix.Substring(0, $boundary).TrimEnd()
  }
  return $prefix + $Ellipsis
}

function Limit-Utf8Text {
  param(
    [AllowEmptyString()][string]$Value,
    [int]$MaxBytes
  )

  if ([string]::IsNullOrEmpty($Value) -or $MaxBytes -le 0) { return '' }
  if ($Utf8NoBom.GetByteCount($Value) -le $MaxBytes) { return $Value }
  $suffix = [string]$Ellipsis
  $suffixBytes = $Utf8NoBom.GetByteCount($suffix)
  if ($MaxBytes -lt $suffixBytes) { return '' }
  $budget = $MaxBytes - $suffixBytes
  $indexes = [Globalization.StringInfo]::ParseCombiningCharacters($Value)
  $low = 0
  $high = $indexes.Count
  while ($low -lt $high) {
    $middle = [int][Math]::Ceiling(($low + $high) / 2.0)
    $end = if ($middle -ge $indexes.Count) { $Value.Length } else { $indexes[$middle] }
    if ($Utf8NoBom.GetByteCount($Value.Substring(0, $end)) -le $budget) {
      $low = $middle
    } else {
      $high = $middle - 1
    }
  }
  $prefixEnd = if ($low -ge $indexes.Count) { $Value.Length } else { $indexes[$low] }
  return $Value.Substring(0, $prefixEnd).TrimEnd() + $suffix
}

function Sanitize-NotificationText {
  param(
    [string]$Text,
    [int]$MaxLength = 900,
    [switch]$PreserveLines
  )

  if ([string]::IsNullOrWhiteSpace($Text)) {
    return ''
  }
  if ($PreserveLines) {
    $value = $Text -replace "`r`n?", "`n"
    $value = [regex]::Replace($value, '[\t\f\v ]+', ' ')
    $value = [regex]::Replace($value, ' *\n *', "`n")
    $value = [regex]::Replace($value, '\n{3,}', "`n`n")
    $value = $value.Trim()
  } else {
    $value = ($Text -replace '\s+', ' ').Trim()
  }
  $value = [regex]::Replace($value, '(?i)\b(authorization)\s*[:=]\s*(bearer|basic)\s+\S+', '$1=[REDACTED]')
  $value = [regex]::Replace($value, '(?i)\b(password|passwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+', '$1=[REDACTED]')
  $value = [regex]::Replace($value, '(?i)\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,})\b', '[REDACTED]')
  $value = [regex]::Replace($value, '(?i)https://ntfy\.sh/[A-Za-z0-9._~-]+', 'https://ntfy.sh/[REDACTED]')
  return Limit-NotificationCharacters -Value $value -MaxLength $MaxLength
}

function Get-ProjectName {
  param([string]$Cwd)

  if ([string]::IsNullOrWhiteSpace($Cwd)) {
    return 'workspace'
  }
  $normalized = $Cwd.Trim().TrimEnd('/', '\') -replace '\\', '/'
  if ($normalized -match '^[A-Za-z]:$') {
    return $normalized
  }
  if ([string]::IsNullOrWhiteSpace($normalized)) {
    return 'workspace'
  }
  $parts = @($normalized -split '/' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
  if ($parts.Count -eq 0) {
    return 'workspace'
  }
  return [string]$parts[-1]
}

function Get-ThreadTitle {
  param(
    [string]$ThreadId,
    [string]$SessionHome = $CodexHome,
    [string]$SqliteHome = $SessionHome
  )

  if ([string]::IsNullOrWhiteSpace($ThreadId)) {
    return $null
  }
  if (-not [string]::IsNullOrWhiteSpace($SqliteHome)) {
    $database = Get-StateDatabasePath -SqliteHome $SqliteHome
    $row = Invoke-SqliteRow -DatabasePath $database -Sql "SELECT COALESCE(title,'') FROM threads WHERE id=?1 LIMIT 1" -Parameter $ThreadId -ColumnCount 1
    if ($row.ok -and $row.found -and -not [string]::IsNullOrWhiteSpace([string]$row.values[0])) {
      return [string]$row.values[0]
    }
  }
  $indexPath = Join-Path $SessionHome 'session_index.jsonl'
  if (-not (Test-Path -LiteralPath $indexPath)) {
    return $null
  }
  $stream = $null
  $reader = $null
  try {
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = New-Object IO.FileStream($indexPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    $reader = New-Object IO.StreamReader($stream, $Utf8NoBom, $true)
    $title = $null
    while (-not $reader.EndOfStream) {
      $line = $reader.ReadLine()
      if ([string]::IsNullOrWhiteSpace($line) -or -not $line.Contains($ThreadId)) { continue }
      try {
        $item = $line | ConvertFrom-Json
        if ([string](Get-ObjectValue $item 'id' '') -eq $ThreadId) {
          $candidate = [string](Get-ObjectValue $item 'thread_name' '')
          if (-not [string]::IsNullOrWhiteSpace($candidate)) { $title = $candidate }
        }
      } catch {
        continue
      }
    }
  } catch {
    $title = $null
  } finally {
    if ($null -ne $reader) { $reader.Dispose() }
    elseif ($null -ne $stream) { $stream.Dispose() }
  }
  if (-not [string]::IsNullOrWhiteSpace($title)) { return $title }
  return $null
}

function Read-FirstLineShared {
  param([string]$Path)

  $stream = $null
  $reader = $null
  try {
    # Codex keeps active rollout files open for writing. FileShare.Read alone
    # conflicts with that writer on Windows, so explicitly allow write/delete.
    $sharing = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
    $stream = [System.IO.FileStream]::new(
      $Path,
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::Read,
      $sharing
    )
    $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $true, 4096, $false)
    return $reader.ReadLine()
  } finally {
    if ($null -ne $reader) {
      $reader.Dispose()
    } elseif ($null -ne $stream) {
      $stream.Dispose()
    }
  }
}

function Get-EventClassification {
  param(
    [object]$Event,
    [string]$ThreadId,
    [string]$SessionHome = $CodexHome
  )

  try {
    $source = Get-ObjectValue $Event 'source'
    if ($null -ne $source -and $source -isnot [string]) {
      $subagent = Get-ObjectValue $source 'subagent'
      if ($null -ne $subagent) {
        return 'subagent'
      }
    }
    foreach ($name in @('is-subagent', 'is_subagent')) {
      $value = Get-ObjectValue $Event $name
      if ($value -eq $true -or [string]$value -match '^(?i:true|1)$') {
        return 'subagent'
      }
    }
    foreach ($name in @('parent-thread-id', 'parent_thread_id')) {
      $value = [string](Get-ObjectValue $Event $name '')
      if (-not [string]::IsNullOrWhiteSpace($value)) {
        return 'subagent'
      }
    }
  } catch {
    # Fall through to the rollout metadata check.
  }

  if ([string]::IsNullOrWhiteSpace($ThreadId)) {
    return 'unknown'
  }
  foreach ($rootName in @('sessions', 'archived_sessions')) {
    $root = Join-Path $SessionHome $rootName
    if (-not (Test-Path -LiteralPath $root)) {
      continue
    }
    try {
      $session = Get-ChildItem -LiteralPath $root -Filter "*$ThreadId*.jsonl" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($null -eq $session) {
        continue
      }
      $firstLine = Read-FirstLineShared -Path $session.FullName
      if ([string]::IsNullOrWhiteSpace($firstLine)) {
        continue
      }
      $metadata = $firstLine | ConvertFrom-Json
      $payload = Get-ObjectValue $metadata 'payload'
      $source = Get-ObjectValue $payload 'source'
      if ($null -ne $source -and $source -isnot [string] -and $null -ne (Get-ObjectValue $source 'subagent')) {
        return 'subagent'
      }
      return 'root'
    } catch {
      continue
    }
  }
  return 'unknown'
}

function Get-RawNotification {
  if ($HookEvent -or $ReadStdin) {
    return [Console]::In.ReadToEnd()
  }
  if ($NotificationArgs.Count -gt 0) {
    return ($NotificationArgs -join ' ')
  }
  if ([Console]::IsInputRedirected) {
    return [Console]::In.ReadToEnd()
  }
  return ''
}

function ConvertTo-NotificationEvent {
  param([string]$Raw)

  if ([string]::IsNullOrWhiteSpace($Raw)) {
    return $null
  }
  try {
    return ($Raw | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    Write-RuntimeLog 'ignored malformed notify payload'
    return $null
  }
}

function New-EventRecord {
  param(
    [object]$Event,
    [string]$EventOrigin,
    [string]$EventSessionHome,
    [string]$EventSqliteHome,
    [string]$EventClassification,
    [bool]$EventIncludeMessage,
    [string]$CandidateKind = 'legacy',
    [string]$SourceEvent = 'agent-turn-complete'
  )

  $threadId = [string](Get-FirstObjectValue $Event @('thread-id', 'thread_id'))
  $turnId = [string](Get-FirstObjectValue $Event @('turn-id', 'turn_id'))
  $weakIdentity = [string]::IsNullOrWhiteSpace($threadId) -or [string]::IsNullOrWhiteSpace($turnId)
  $identity = if ($weakIdentity) {
    'codex-ntfy/v1|weak|' + [Guid]::NewGuid().ToString('N')
  } else {
    "codex-ntfy/v1|$threadId|$turnId"
  }
  $key = Get-Sha256Hex $identity
  $now = [DateTimeOffset]::UtcNow
  $storedOrigin = Sanitize-NotificationText -Text $EventOrigin -MaxLength 100
  $storedEvent = [ordered]@{
    type = 'agent-turn-complete'
    cwd = Sanitize-NotificationText -Text ([string](Get-FirstObjectValue $Event @('cwd', 'working-directory', 'working_directory'))) -MaxLength 1000
    'last-assistant-message' = if ($EventIncludeMessage) {
      Sanitize-NotificationText -Text ([string](Get-FirstObjectValue $Event @('last-assistant-message', 'last_assistant_message'))) -MaxLength 4000 -PreserveLines
    } else { '' }
  }

  return [pscustomobject]@{
    schema = 1
    key = $key
    sequence_id = 'codex-' + $key.Substring(0, 32)
    weak_identity = $weakIdentity
    thread_id = $threadId
    turn_id = $turnId
    origin = $storedOrigin
    session_codex_home = $EventSessionHome
    session_sqlite_home = $EventSqliteHome
    session_classification = $EventClassification
    candidate_kind = $CandidateKind
    source_event = $SourceEvent
    completion_event_type = [string](Get-FirstObjectValue $Event @('completion-event-type', 'completion_event_type'))
    candidate_rollout_path = Sanitize-NotificationText -Text ([string](Get-FirstObjectValue $Event @('transcript_path', 'transcript-path', 'rollout_path', 'rollout-path'))) -MaxLength 1200
    rollout_sequence = [int64](Get-FirstObjectValue $Event @('rollout-sequence', 'rollout_sequence'))
    created_at = $now.ToString('o')
    created_unix_ms = $now.ToUnixTimeMilliseconds()
    next_attempt_unix_ms = $now.ToUnixTimeMilliseconds()
    attempts = 0
    last_error = $null
    event = $storedEvent
  }
}

function Test-IsStopEvidence {
  param([object]$Record)
  return [string](Get-ObjectValue $Record 'source_event' '') -eq 'Stop' -or
    [string](Get-ObjectValue $Record 'candidate_kind' '') -eq 'hook_stop'
}

function Invoke-WithRecordMutationLock {
  param(
    [string]$Key,
    [scriptblock]$Action,
    [object[]]$Arguments = @()
  )
  Ensure-RuntimeDirectories
  if ($Key -notmatch '^[0-9a-f]{64}$') { throw 'invalid mutation lock key' }
  $lockPath = Join-Path $StateRoot ("mutation-$Key.lock")
  $lockStream = $null
  for ($attempt = 0; $attempt -lt 200 -and $null -eq $lockStream; $attempt++) {
    try {
      $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
      if ($attempt -ge 199) { throw }
      Start-Sleep -Milliseconds 10
    }
  }
  try {
    return (& $Action @Arguments)
  } finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
  }
}

function Upgrade-PendingRecordFromStop {
  param(
    [string]$Path,
    [object]$IncomingRecord
  )
  if (-not (Test-IsStopEvidence -Record $IncomingRecord) -or -not (Test-Path -LiteralPath $Path)) {
    return $false
  }
  $existing = Read-JsonFile -Path $Path
  Assert-QueuedRecord -Record $existing -ExpectedKey $IncomingRecord.key

  $existingRollout = [string](Get-ObjectValue $existing 'candidate_rollout_path' '')
  if ([string]::IsNullOrWhiteSpace([string](Get-ObjectValue $IncomingRecord 'candidate_rollout_path' '')) -and
      -not [string]::IsNullOrWhiteSpace($existingRollout)) {
    Set-RecordValue -Record $IncomingRecord -Name 'candidate_rollout_path' -Value $existingRollout
    Set-RecordValue -Record $IncomingRecord -Name 'rollout_sequence' -Value ([int64](Get-ObjectValue $existing 'rollout_sequence' 0))
  }
  $existingAttempts = [int](Get-ObjectValue $existing 'attempts' 0)
  if ($existingAttempts -gt 0) {
    Set-RecordValue -Record $IncomingRecord -Name 'attempts' -Value $existingAttempts
    Set-RecordValue -Record $IncomingRecord -Name 'next_attempt_unix_ms' -Value ([int64](Get-ObjectValue $existing 'next_attempt_unix_ms' 0))
    Set-RecordValue -Record $IncomingRecord -Name 'last_error' -Value (Get-ObjectValue $existing 'last_error')
  }
  # IncomingRecord was built with the current include_message setting; replacing
  # the whole JSON atomically also removes an older message after privacy opt-out.
  Write-JsonAtomic -Path $Path -Value $IncomingRecord
  Write-RuntimeLog "upgraded pending candidate with Stop evidence key=$($IncomingRecord.key.Substring(0, 12))"
  return $true
}

function Remove-TechnicalSuppressionForStopCore {
  param(
    [string]$Path,
    [object]$Record
  )
  if (-not (Test-IsStopEvidence -Record $Record) -or -not (Test-Path -LiteralPath $Path)) {
    return $false
  }
  try {
    $receipt = Read-JsonFile -Path $Path
    if ([string](Get-ObjectValue $receipt 'reason' '') -ne 'technical-turn') { return $false }
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $Path) { return $false }
    Write-RuntimeLog "revived technical suppression with Stop evidence key=$($Record.key.Substring(0, 12))"
    return $true
  } catch {
    return $false
  }
}

function Add-OutboxEventCore {
  param([object]$Record)

  Ensure-RuntimeDirectories
  $sentPath = Join-Path $SentDir ($Record.key + '.json')
  $suppressedPath = Join-Path $SuppressedDir ($Record.key + '.json')
  $deadPath = Join-Path $DeadDir ($Record.key + '.json')
  $pendingPath = Join-Path $PendingDir ($Record.key + '.json')
  $outboxPath = Join-Path $OutboxDir ($Record.key + '.json')
  if (Test-Path -LiteralPath $sentPath) {
    Write-RuntimeLog "deduplicated sent event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'sent' }
  }
  if (Test-Path -LiteralPath $deadPath) {
    Write-RuntimeLog "deduplicated dead event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'dead' }
  }
  if (Test-Path -LiteralPath $suppressedPath) {
    $revived = Remove-TechnicalSuppressionForStopCore -Path $suppressedPath -Record $Record
    if (-not $revived) {
      Write-RuntimeLog "deduplicated suppressed event key=$($Record.key.Substring(0, 12))"
      return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'suppressed' }
    }
  }
  if (Test-Path -LiteralPath $pendingPath) {
    if (Upgrade-PendingRecordFromStop -Path $pendingPath -IncomingRecord $Record) {
      return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'pending' }
    }
    $existing = Read-JsonFile -Path $pendingPath
    Assert-QueuedRecord -Record $existing -ExpectedKey $Record.key
    Write-RuntimeLog "deduplicated pending event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'pending' }
  }
  if (Test-Path -LiteralPath $outboxPath) {
    $existing = Read-JsonFile -Path $outboxPath
    Assert-QueuedRecord -Record $existing -ExpectedKey $Record.key
    Write-RuntimeLog "deduplicated queued event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'queued' }
  }
  try {
    Write-JsonAtomic -Path $outboxPath -Value $Record -NoOverwrite
    Write-RuntimeLog "queued event key=$($Record.key.Substring(0, 12)) origin=$($Record.origin)"
    return [pscustomobject]@{ queued = $true; key = $Record.key; status = 'queued' }
  } catch [System.IO.IOException] {
    if (Test-Path -LiteralPath $outboxPath) {
      $existing = Read-JsonFile -Path $outboxPath
      Assert-QueuedRecord -Record $existing -ExpectedKey $Record.key
      return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'queued' }
    }
    throw
  }
}

function Add-PendingEventCore {
  param([object]$Record)

  Ensure-RuntimeDirectories
  $sentPath = Join-Path $SentDir ($Record.key + '.json')
  $suppressedPath = Join-Path $SuppressedDir ($Record.key + '.json')
  $deadPath = Join-Path $DeadDir ($Record.key + '.json')
  $pendingPath = Join-Path $PendingDir ($Record.key + '.json')
  $outboxPath = Join-Path $OutboxDir ($Record.key + '.json')
  foreach ($terminal in @(@($sentPath, 'sent'), @($deadPath, 'dead'))) {
    if (Test-Path -LiteralPath $terminal[0]) {
      Write-RuntimeLog "deduplicated $($terminal[1]) event key=$($Record.key.Substring(0, 12))"
      return [pscustomobject]@{ queued = $false; key = $Record.key; status = $terminal[1] }
    }
  }
  if (Test-Path -LiteralPath $suppressedPath) {
    $revived = Remove-TechnicalSuppressionForStopCore -Path $suppressedPath -Record $Record
    if (-not $revived) {
      Write-RuntimeLog "deduplicated suppressed event key=$($Record.key.Substring(0, 12))"
      return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'suppressed' }
    }
  }
  if (Test-Path -LiteralPath $outboxPath) {
    $existing = Read-JsonFile -Path $outboxPath
    Assert-QueuedRecord -Record $existing -ExpectedKey $Record.key
    Write-RuntimeLog "deduplicated queued event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'queued' }
  }
  if (Test-Path -LiteralPath $pendingPath) {
    if (Upgrade-PendingRecordFromStop -Path $pendingPath -IncomingRecord $Record) {
      return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'pending' }
    }
    $existing = Read-JsonFile -Path $pendingPath
    Assert-QueuedRecord -Record $existing -ExpectedKey $Record.key
    Write-RuntimeLog "deduplicated pending event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'pending' }
  }
  try {
    Write-JsonAtomic -Path $pendingPath -Value $Record -NoOverwrite
    Write-RuntimeLog "pending idle candidate key=$($Record.key.Substring(0, 12)) origin=$($Record.origin)"
    return [pscustomobject]@{ queued = $true; key = $Record.key; status = 'pending' }
  } catch [System.IO.IOException] {
    if (Test-Path -LiteralPath $pendingPath) {
      if (Upgrade-PendingRecordFromStop -Path $pendingPath -IncomingRecord $Record) {
        return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'pending' }
      }
      $existing = Read-JsonFile -Path $pendingPath
      Assert-QueuedRecord -Record $existing -ExpectedKey $Record.key
      return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'pending' }
    }
    throw
  }
}

function Add-OutboxEvent {
  param([object]$Record)
  return Invoke-WithRecordMutationLock -Key ([string]$Record.key) -Action {
    param($lockedRecord)
    Add-OutboxEventCore -Record $lockedRecord
  } -Arguments @($Record)
}

function Add-PendingEvent {
  param([object]$Record)
  return Invoke-WithRecordMutationLock -Key ([string]$Record.key) -Action {
    param($lockedRecord)
    Add-PendingEventCore -Record $lockedRecord
  } -Arguments @($Record)
}

function Add-CandidateEvent {
  param(
    [object]$Record,
    [object]$Config
  )

  if ($Config.idleDetectionMode -eq 'off') {
    return Add-OutboxEvent -Record $Record
  }
  return Add-PendingEvent -Record $Record
}

function Get-DefaultOrigin {
  if (-not [string]::IsNullOrWhiteSpace($Origin)) {
    return $Origin
  }
  if (-not [string]::IsNullOrWhiteSpace($env:CODEX_NTFY_ORIGIN)) {
    return $env:CODEX_NTFY_ORIGIN
  }
  if (-not [string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) {
    return $env:COMPUTERNAME
  }
  return 'Windows'
}

function Get-CompletionLabel {
  param([object]$Record)

  $goal = ([string](Get-ObjectValue $Record 'goal_status' '')).Trim().ToLowerInvariant()
  switch ($goal) {
    'blocked' { return 'blocked' }
    'paused' { return 'paused' }
    'usage_limited' { return 'usage limit' }
    'budget_limited' { return 'budget limit' }
  }
  if (([string](Get-ObjectValue $Record 'completion_event_type' '')).Trim().ToLowerInvariant() -eq 'turn_aborted') {
    return 'stopped'
  }
  return 'done'
}

function New-NtfyPayload {
  param(
    [object]$Record,
    [object]$Config
  )

  $event = $Record.event
  $cwd = [string](Get-FirstObjectValue $event @('cwd', 'working-directory', 'working_directory'))
  $project = Sanitize-NotificationText -Text (Get-ProjectName $cwd) -MaxLength 60
  $sessionHome = [string](Get-ObjectValue $Record 'session_codex_home' $CodexHome)
  if ([string]::IsNullOrWhiteSpace($sessionHome)) {
    $sessionHome = $CodexHome
  }
  $sessionSqliteHome = [string](Get-ObjectValue $Record 'session_sqlite_home' $sessionHome)
  if ([string]::IsNullOrWhiteSpace($sessionSqliteHome)) { $sessionSqliteHome = $sessionHome }
  $displayName = $project
  $hasDistinctThreadTitle = $false
  if ($Config.includeThreadTitle) {
    $threadTitle = Sanitize-NotificationText -Text (Get-ThreadTitle -ThreadId $Record.thread_id -SessionHome $sessionHome -SqliteHome $sessionSqliteHome) -MaxLength 60
    if (-not [string]::IsNullOrWhiteSpace($threadTitle)) {
      $displayName = $threadTitle
      $hasDistinctThreadTitle = -not [string]::Equals($threadTitle, $project, [StringComparison]::OrdinalIgnoreCase)
    }
  }

  $metadata = @()
  if ($Config.includeFullPath -and -not [string]::IsNullOrWhiteSpace($cwd)) {
    $metadata += Sanitize-NotificationText -Text $cwd -MaxLength 120
  } elseif ($hasDistinctThreadTitle) {
    $metadata += $project
  }
  $sanitizedOrigin = Sanitize-NotificationText -Text ([string](Get-ObjectValue $Record 'origin' '')) -MaxLength 40
  if (-not [string]::IsNullOrWhiteSpace($sanitizedOrigin)) {
    $metadata += $sanitizedOrigin
  }
  if (-not [string]::IsNullOrWhiteSpace($Record.thread_id)) {
    $rawThread = [string]$Record.thread_id
    $shortThread = Sanitize-NotificationText -Text $rawThread.Substring(0, [Math]::Min(8, $rawThread.Length)) -MaxLength 8
    if (-not [string]::IsNullOrWhiteSpace($shortThread)) { $metadata += '#' + $shortThread }
  }
  $context = $metadata -join (" $MiddleDot ")

  $summary = ''
  if ($Config.includeMessage) {
    $rawMessage = [string](Get-FirstObjectValue $event @('last-assistant-message', 'last_assistant_message'))
    if ($Config.markdown) {
      $summary = Sanitize-NotificationText -Text $rawMessage -MaxLength $Config.maxMessageChars -PreserveLines
    } else {
      $summary = Sanitize-NotificationText -Text $rawMessage -MaxLength $Config.maxMessageChars
    }
  }
  $separator = if ($Config.markdown -and -not [string]::IsNullOrWhiteSpace($summary)) { "`n`n" } else { " $MiddleDot " }
  $suffix = if (-not [string]::IsNullOrWhiteSpace($summary) -and -not [string]::IsNullOrWhiteSpace($context)) {
    $separator + $context
  } elseif (-not [string]::IsNullOrWhiteSpace($context)) {
    $context
  } else {
    ''
  }
  if (-not [string]::IsNullOrWhiteSpace($summary)) {
    $summaryBudget = $MaxNtfyMessageBytes - $Utf8NoBom.GetByteCount($suffix)
    $summary = Limit-Utf8Text -Value $summary -MaxBytes ([Math]::Max(0, $summaryBudget))
    $body = $summary + $suffix
  } else {
    if (-not [string]::IsNullOrWhiteSpace($context)) {
      $body = $context
    } else {
      $fallbackLabel = Get-CompletionLabel -Record $Record
      $body = $fallbackLabel.Substring(0, 1).ToUpperInvariant() + $fallbackLabel.Substring(1)
    }
  }
  $body = Limit-Utf8Text -Value $body -MaxBytes $MaxNtfyMessageBytes

  $payload = [ordered]@{
    topic = $Config.topic
    title = $displayName
    message = $body
    sequence_id = $Record.sequence_id
  }
  if (@($Config.tags).Count -gt 0) { $payload['tags'] = @($Config.tags) }
  if ([int]$Config.priority -ne 3) { $payload['priority'] = [int]$Config.priority }
  if ($Config.markdown -and -not [string]::IsNullOrWhiteSpace($summary)) { $payload['markdown'] = $true }
  return $payload
}

function Send-NtfyEvent {
  param(
    [object]$Record,
    [object]$Config
  )

  if ([string]::IsNullOrWhiteSpace($Config.topic)) {
    throw "ntfy topic is not configured in $ConfigPath"
  }
  if ([string]::IsNullOrWhiteSpace($Config.server)) {
    throw 'ntfy server is empty'
  }
  $serverUri = $null
  if (-not [Uri]::TryCreate([string]$Config.server, [UriKind]::Absolute, [ref]$serverUri) -or
      $serverUri.Scheme -notin @('http', 'https') -or [string]::IsNullOrWhiteSpace($serverUri.Host)) {
    throw 'ntfy server must be an absolute HTTP or HTTPS URL.'
  }
  if (-not [string]::IsNullOrEmpty($serverUri.UserInfo)) {
    throw 'ntfy server URL must not contain credentials; use token/username/password fields.'
  }
  $hasUsername = -not [string]::IsNullOrWhiteSpace($Config.username)
  $hasPassword = -not [string]::IsNullOrWhiteSpace($Config.password)
  if ([string]::IsNullOrWhiteSpace($Config.token) -and $hasUsername -ne $hasPassword) {
    throw 'Basic authentication requires both username and password.'
  }
  $usesAuth = -not [string]::IsNullOrWhiteSpace($Config.token) -or
    (-not [string]::IsNullOrWhiteSpace($Config.username) -and -not [string]::IsNullOrWhiteSpace($Config.password))
  $isLoopback = $serverUri.IsLoopback -or $serverUri.Host -in @('localhost', '127.0.0.1', '::1')
  if ($usesAuth -and $serverUri.Scheme -ne 'https' -and -not $isLoopback -and -not $Config.allowInsecureAuth) {
    throw 'Refusing to send ntfy credentials over an insecure connection. Use HTTPS or set allow_insecure_auth explicitly.'
  }

  $authorization = ''
  if (-not [string]::IsNullOrWhiteSpace($Config.token)) {
    $authorization = 'Bearer ' + $Config.token
  } elseif (-not [string]::IsNullOrWhiteSpace($Config.username) -and -not [string]::IsNullOrWhiteSpace($Config.password)) {
    $pair = '{0}:{1}' -f $Config.username, $Config.password
    $authorization = 'Basic ' + [Convert]::ToBase64String($Utf8NoBom.GetBytes($pair))
  }

  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  } catch {
    # The system TLS default is acceptable when this flag is unavailable.
  }

  $json = ConvertTo-CompactJson (New-NtfyPayload -Record $Record -Config $Config)
  $bodyBytes = $Utf8NoBom.GetBytes($json)
  $request = [Net.HttpWebRequest]::Create($serverUri)
  $request.Method = 'POST'
  $request.ContentType = 'application/json; charset=utf-8'
  $request.UserAgent = "codex-ntfy-notifier/$ScriptVersion"
  $request.AllowAutoRedirect = $false
  $request.Timeout = [Math]::Max(1, [int]$Config.timeoutSeconds) * 1000
  $request.ReadWriteTimeout = $request.Timeout
  $request.ContentLength = $bodyBytes.Length
  if (-not [string]::IsNullOrWhiteSpace($authorization)) {
    $request.Headers[[Net.HttpRequestHeader]::Authorization] = $authorization
  }

  $requestStream = $null
  try {
    $requestStream = $request.GetRequestStream()
    $requestStream.Write($bodyBytes, 0, $bodyBytes.Length)
  } finally {
    if ($null -ne $requestStream) { $requestStream.Dispose() }
  }

  $response = $null
  try {
    $response = [Net.HttpWebResponse]$request.GetResponse()
    $statusCode = [int]$response.StatusCode
    if ($statusCode -lt 200 -or $statusCode -ge 300) {
      $httpError = [Net.WebException]::new(
        "HTTP $statusCode",
        $null,
        [Net.WebExceptionStatus]::ProtocolError,
        $response
      )
      $httpError.Data['StatusCode'] = $statusCode
      throw $httpError
    }
    $reader = New-Object IO.StreamReader($response.GetResponseStream(), $Utf8NoBom)
    try {
      $responseBody = $reader.ReadToEnd()
    } finally {
      $reader.Dispose()
    }
    if ([string]::IsNullOrWhiteSpace($responseBody)) {
      return [pscustomobject]@{}
    }
    try {
      return $responseBody | ConvertFrom-Json
    } catch {
      return [pscustomobject]@{}
    }
  } finally {
    if ($null -ne $response) { $response.Dispose() }
  }
}

function Get-RetryDelaySeconds {
  param(
    [int]$Attempt,
    [double]$BaseSeconds,
    [double]$MaxSeconds,
    [double]$RetryAfterSeconds = -1
  )

  if ($RetryAfterSeconds -ge 0) {
    return [Math]::Max(0.05, [Math]::Min($MaxSeconds, $RetryAfterSeconds))
  }
  $exponent = [Math]::Min(10, [Math]::Max(0, $Attempt - 1))
  $ceiling = [Math]::Min($MaxSeconds, $BaseSeconds * [Math]::Pow(2, $exponent))
  if ($ceiling -le 0) {
    return 0
  }
  $jitter = Get-Random -Minimum 60 -Maximum 101
  return [Math]::Max(0.05, $ceiling * ($jitter / 100.0))
}

function Get-HttpFailureInfo {
  param([object]$ErrorRecord)

  $statusCode = 0
  $retryAfterSeconds = -1.0
  $messages = @()
  $exception = $ErrorRecord.Exception
  while ($null -ne $exception) {
    $messages += [string]$exception.Message
    if ($statusCode -eq 0) {
      try {
        if ($exception.Data.Contains('StatusCode')) {
          $statusCode = [int]$exception.Data['StatusCode']
        }
      } catch {
        # Ignore malformed custom exception data.
      }
    }
    try {
      $responseProperty = $exception.PSObject.Properties['Response']
      $response = if ($null -ne $responseProperty) { $responseProperty.Value } else { $null }
      if ($statusCode -eq 0 -and $null -ne $response -and $null -ne $response.StatusCode) {
        $statusCode = [int]$response.StatusCode
      }
      if ($retryAfterSeconds -lt 0 -and $null -ne $response -and $null -ne $response.Headers) {
        $retryAfter = [string]$response.Headers['Retry-After']
        $seconds = 0.0
        if ([double]::TryParse($retryAfter, [ref]$seconds)) {
          $retryAfterSeconds = [Math]::Max(0, $seconds)
        } else {
          $retryAt = [DateTimeOffset]::MinValue
          if ([DateTimeOffset]::TryParse($retryAfter, [ref]$retryAt)) {
            $retryAfterSeconds = [Math]::Max(0, ($retryAt - [DateTimeOffset]::UtcNow).TotalSeconds)
          }
        }
      }
    } catch {
      # A network failure may not expose an HTTP response.
    }
    $innerProperty = $exception.PSObject.Properties['InnerException']
    $inner = if ($null -ne $innerProperty) { $innerProperty.Value } else { $null }
    if ($null -eq $inner -or [object]::ReferenceEquals($inner, $exception)) { break }
    $exception = $inner
  }
  $combinedMessage = $messages -join ' '
  if ($statusCode -eq 0 -and $combinedMessage -match '(?i)\bHTTP\s+([1-5][0-9]{2})\b') {
    $statusCode = [int]$Matches[1]
  } elseif ($statusCode -eq 0 -and $combinedMessage -match '\(([1-5][0-9]{2})\)') {
    $statusCode = [int]$Matches[1]
  }
  $retryableClientStatuses = @(401, 403, 408, 409, 425, 429)
  $permanent = ($statusCode -ge 300 -and $statusCode -lt 400) -or
    ($statusCode -ge 400 -and $statusCode -lt 500 -and $statusCode -notin $retryableClientStatuses)
  return [pscustomobject]@{
    statusCode = $statusCode
    retryAfterSeconds = $retryAfterSeconds
    permanent = $permanent
  }
}

function Move-ToDeadLetter {
  param(
    [string]$Path,
    [object]$Record
  )

  $deadPath = Join-Path $DeadDir (Split-Path -Leaf $Path)
  Write-JsonAtomic -Path $deadPath -Value $Record
  Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

function Move-ToSuppressedCore {
  param(
    [string]$Path,
    [object]$Record,
    [string]$Reason = 'subagent'
  )

  $incomingIsStop = Test-IsStopEvidence -Record $Record
  if (-not $incomingIsStop) {
    $candidatePaths = @($Path, (Join-Path $PendingDir ($Record.key + '.json')), (Join-Path $OutboxDir ($Record.key + '.json'))) |
      Select-Object -Unique
    foreach ($candidatePath in @($candidatePaths)) {
      if (-not (Test-Path -LiteralPath $candidatePath)) { continue }
      try {
        $current = Read-JsonFile -Path $candidatePath
        if ($null -ne $current -and (Test-IsStopEvidence -Record $current)) {
          Write-RuntimeLog "kept Stop candidate during suppression race key=$($Record.key.Substring(0, 12))"
          return
        }
      } catch {
        # Preserve the existing invalid-record/dead-letter path for unreadable JSON.
      }
    }
  }
  $suppressedPath = Join-Path $SuppressedDir ($Record.key + '.json')
  $receipt = [ordered]@{
    schema = 1
    key = $Record.key
    thread_id = $Record.thread_id
    turn_id = $Record.turn_id
    origin = $Record.origin
    suppressed_at = [DateTimeOffset]::UtcNow.ToString('o')
    reason = $Reason
  }
  Write-JsonAtomic -Path $suppressedPath -Value $receipt
  Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

function Move-ToSuppressed {
  param(
    [string]$Path,
    [object]$Record,
    [string]$Reason = 'subagent'
  )
  [void](Invoke-WithRecordMutationLock -Key ([string]$Record.key) -Action {
      param($lockedPath, $lockedRecord, $lockedReason)
      Move-ToSuppressedCore -Path $lockedPath -Record $lockedRecord -Reason $lockedReason
    } -Arguments @($Path, $Record, $Reason))
}

function Clean-RuntimeState {
  param(
    [int]$ReceiptRetentionDays,
    [int]$DeadRetentionDays
  )

  $cutoff = [DateTime]::UtcNow.AddDays(-[Math]::Max(1, $ReceiptRetentionDays))
  foreach ($directory in @($SentDir, $SuppressedDir)) {
    Get-ChildItem -LiteralPath $directory -Filter '*.json' -File -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTimeUtc -lt $cutoff } |
      Remove-Item -Force -ErrorAction SilentlyContinue
  }
  $deadCutoff = [DateTime]::UtcNow.AddDays(-[Math]::Max(1, $DeadRetentionDays))
  Get-ChildItem -LiteralPath $DeadDir -Filter '*.json' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTimeUtc -lt $deadCutoff } |
    Remove-Item -Force -ErrorAction SilentlyContinue
}

$script:RolloutProbeCache = @{}
$script:ThreadDatabaseCache = @{}
$script:RolloutDiscoveryCache = @{}

function Initialize-WinSqlite {
  if ($null -ne ([System.Management.Automation.PSTypeName]'CodexNtfyWinSqlite').Type) {
    return
  }
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class CodexNtfyWinSqlite {
    private const int SQLITE_OK = 0;
    private const int SQLITE_ROW = 100;
    private const int SQLITE_DONE = 101;
    private const int SQLITE_OPEN_READONLY = 1;

    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_open_v2(byte[] filename, out IntPtr database, int flags, IntPtr vfs);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_close_v2(IntPtr database);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_busy_timeout(IntPtr database, int milliseconds);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_prepare_v2(IntPtr database, byte[] sql, int bytes, out IntPtr statement, IntPtr tail);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_bind_text(IntPtr statement, int index, byte[] value, int bytes, IntPtr destructor);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_step(IntPtr statement);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_finalize(IntPtr statement);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr sqlite3_column_text(IntPtr statement, int column);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int sqlite3_column_bytes(IntPtr statement, int column);
    [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr sqlite3_errmsg(IntPtr database);

    private static byte[] Utf8Z(string value) {
        byte[] bytes = Encoding.UTF8.GetBytes(value ?? String.Empty);
        byte[] terminated = new byte[bytes.Length + 1];
        Buffer.BlockCopy(bytes, 0, terminated, 0, bytes.Length);
        return terminated;
    }

    private static string Error(IntPtr database, string operation, int code) {
        string detail = database == IntPtr.Zero ? String.Empty : Marshal.PtrToStringAnsi(sqlite3_errmsg(database));
        return operation + " failed (" + code + "): " + detail;
    }

    public static string[] QueryRow(string databasePath, string sql, string parameter, int columnCount) {
        IntPtr database = IntPtr.Zero;
        IntPtr statement = IntPtr.Zero;
        try {
            byte[] databaseBytes = Utf8Z(databasePath);
            int result = sqlite3_open_v2(databaseBytes, out database, SQLITE_OPEN_READONLY, IntPtr.Zero);
            if (result != SQLITE_OK) throw new InvalidOperationException(Error(database, "sqlite open", result));
            sqlite3_busy_timeout(database, 1000);
            byte[] sqlBytes = Utf8Z(sql);
            result = sqlite3_prepare_v2(database, sqlBytes, sqlBytes.Length - 1, out statement, IntPtr.Zero);
            if (result != SQLITE_OK) throw new InvalidOperationException(Error(database, "sqlite prepare", result));
            byte[] parameterBytes = Utf8Z(parameter);
            result = sqlite3_bind_text(statement, 1, parameterBytes, parameterBytes.Length - 1, new IntPtr(-1));
            if (result != SQLITE_OK) throw new InvalidOperationException(Error(database, "sqlite bind", result));
            result = sqlite3_step(statement);
            if (result == SQLITE_DONE) return null;
            if (result != SQLITE_ROW) throw new InvalidOperationException(Error(database, "sqlite step", result));
            string[] values = new string[columnCount];
            for (int index = 0; index < columnCount; index++) {
                IntPtr pointer = sqlite3_column_text(statement, index);
                int length = sqlite3_column_bytes(statement, index);
                if (pointer == IntPtr.Zero || length <= 0) {
                    values[index] = String.Empty;
                    continue;
                }
                byte[] bytes = new byte[length];
                Marshal.Copy(pointer, bytes, 0, length);
                values[index] = Encoding.UTF8.GetString(bytes);
            }
            return values;
        } finally {
            if (statement != IntPtr.Zero) sqlite3_finalize(statement);
            if (database != IntPtr.Zero) sqlite3_close_v2(database);
        }
    }
}
'@
}

function Invoke-SqliteRow {
  param(
    [string]$DatabasePath,
    [string]$Sql,
    [string]$Parameter,
    [int]$ColumnCount
  )

  if (-not (Test-Path -LiteralPath $DatabasePath)) {
    return [pscustomobject]@{ ok = $false; found = $false; missing = $true; values = @(); error = 'database missing' }
  }
  try {
    Initialize-WinSqlite
    $values = [CodexNtfyWinSqlite]::QueryRow($DatabasePath, $Sql, $Parameter, $ColumnCount)
    return [pscustomobject]@{
      ok = $true
      found = $null -ne $values
      missing = $false
      values = @($values)
      error = ''
    }
  } catch {
    return [pscustomobject]@{
      ok = $false
      found = $false
      missing = $false
      values = @()
      error = Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240
    }
  }
}

function Get-StateDatabasePath {
  param([string]$SqliteHome)
  $preferred = Join-Path $SqliteHome 'state_5.sqlite'
  try {
    $candidate = $null
    $highestVersion = [int64]-1
    foreach ($file in @(Get-ChildItem -LiteralPath $SqliteHome -Filter 'state_*.sqlite' -File -ErrorAction Stop)) {
      $match = [regex]::Match($file.Name, '^state_(\d+)\.sqlite$', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
      if (-not $match.Success) { continue }
      [int64]$version = 0
      if (-not [int64]::TryParse($match.Groups[1].Value, [ref]$version)) { continue }
      if ($version -gt $highestVersion) {
        $highestVersion = $version
        $candidate = $file
      }
    }
    if ($null -ne $candidate) { return $candidate.FullName }
  } catch {
    # The preferred path remains the diagnostic target when discovery fails.
  }
  return $preferred
}

function Resolve-RolloutPath {
  param(
    [string]$DatabasePathValue,
    [string]$SessionHome
  )

  if ([string]::IsNullOrWhiteSpace($DatabasePathValue)) { return '' }
  if (Test-Path -LiteralPath $DatabasePathValue) { return $DatabasePathValue }
  $normalized = $DatabasePathValue.Replace('\', '/')
  $marker = $normalized.IndexOf('/.codex/', [StringComparison]::OrdinalIgnoreCase)
  if ($marker -ge 0 -and -not [string]::IsNullOrWhiteSpace($SessionHome)) {
    $relative = $normalized.Substring($marker + '/.codex/'.Length).Replace('/', [IO.Path]::DirectorySeparatorChar)
    $translated = Join-Path $SessionHome $relative
    if (Test-Path -LiteralPath $translated) { return $translated }
  }
  $leaf = Split-Path -Leaf $DatabasePathValue
  if (-not [string]::IsNullOrWhiteSpace($leaf)) {
    foreach ($rootName in @('sessions', 'archived_sessions')) {
      $root = Join-Path $SessionHome $rootName
      if (-not (Test-Path -LiteralPath $root)) { continue }
      $match = Get-ChildItem -LiteralPath $root -Filter $leaf -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($null -ne $match) { return $match.FullName }
    }
  }
  return ''
}

function Find-RolloutPathByThread {
  param(
    [string]$ThreadId,
    [string]$SessionHome
  )
  if ([string]::IsNullOrWhiteSpace($ThreadId) -or [string]::IsNullOrWhiteSpace($SessionHome)) { return '' }
  foreach ($rootName in @('sessions', 'archived_sessions')) {
    $root = Join-Path $SessionHome $rootName
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $match = Get-ChildItem -LiteralPath $root -Filter "*$ThreadId*.jsonl" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $match) { return $match.FullName }
  }
  return ''
}

function Get-ThreadDatabaseInfo {
  param(
    [string]$ThreadId,
    [string]$SqliteHome,
    [string]$SessionHome
  )

  $cacheKey = ($SqliteHome + '|' + $SessionHome + '|' + $ThreadId).ToLowerInvariant()
  if ($script:ThreadDatabaseCache.ContainsKey($cacheKey)) {
    return $script:ThreadDatabaseCache[$cacheKey]
  }
  if ([string]::IsNullOrWhiteSpace($ThreadId) -or [string]::IsNullOrWhiteSpace($SqliteHome) -or [string]::IsNullOrWhiteSpace($SessionHome)) {
    return [pscustomobject]@{ ok = $false; found = $false; classification = 'unknown'; rolloutPath = ''; error = 'thread identity missing' }
  }
  $database = Get-StateDatabasePath -SqliteHome $SqliteHome
  $row = Invoke-SqliteRow -DatabasePath $database -Sql "SELECT rollout_path, COALESCE(thread_source,''), COALESCE(source,'') FROM threads WHERE id=?1 LIMIT 1" -Parameter $ThreadId -ColumnCount 3
  if (-not $row.ok -and $row.error -match '(?i)no such column') {
    $row = Invoke-SqliteRow -DatabasePath $database -Sql "SELECT rollout_path, '', COALESCE(source,'') FROM threads WHERE id=?1 LIMIT 1" -Parameter $ThreadId -ColumnCount 3
  }
  if (-not $row.ok -or -not $row.found) {
    return [pscustomobject]@{ ok = $row.ok; found = $false; classification = 'unknown'; rolloutPath = ''; error = $row.error }
  }
  $threadSource = [string]$row.values[1]
  $source = [string]$row.values[2]
  $classification = if ($threadSource -eq 'subagent' -or $source -match '(?i)"subagent"') {
    'subagent'
  } elseif (-not [string]::IsNullOrWhiteSpace($threadSource) -or -not [string]::IsNullOrWhiteSpace($source)) {
    'root'
  } else {
    'unknown'
  }
  $info = [pscustomobject]@{
    ok = $true
    found = $true
    classification = $classification
    rolloutPath = Resolve-RolloutPath -DatabasePathValue ([string]$row.values[0]) -SessionHome $SessionHome
    error = ''
  }
  if (-not [string]::IsNullOrWhiteSpace($info.rolloutPath)) {
    $script:ThreadDatabaseCache[$cacheKey] = $info
  }
  return $info
}

function Get-GoalDatabaseStatus {
  param(
    [string]$ThreadId,
    [string]$SqliteHome
  )

  $database = Join-Path $SqliteHome 'goals_1.sqlite'
  if (-not (Test-Path -LiteralPath $database)) {
    return [pscustomobject]@{ ok = $true; available = $false; found = $false; status = ''; error = '' }
  }
  $row = Invoke-SqliteRow -DatabasePath $database -Sql 'SELECT status FROM thread_goals WHERE thread_id=?1 LIMIT 1' -Parameter $ThreadId -ColumnCount 1
  return [pscustomobject]@{
    ok = $row.ok
    available = $row.ok
    found = $row.found
    status = if ($row.found) { ([string]$row.values[0]).ToLowerInvariant() } else { '' }
    error = $row.error
  }
}

function Get-DescendantThreadIds {
  param(
    [string]$ThreadId,
    [string]$SqliteHome
  )
  $database = Get-StateDatabasePath -SqliteHome $SqliteHome
  if (-not (Test-Path -LiteralPath $database)) {
    # In strict mode, an absent state database is unknown capability/state, not
    # proof that no subagent exists. Balanced mode may use its normal fallback.
    return [pscustomobject]@{ ok = $false; available = $false; entries = @(); error = 'state database missing' }
  }
  $sql = @'
WITH RECURSIVE descendants(id, status) AS (
  SELECT child_thread_id, COALESCE(status, '') FROM thread_spawn_edges WHERE parent_thread_id = ?1
  UNION
  SELECT edge.child_thread_id, COALESCE(edge.status, '')
  FROM thread_spawn_edges AS edge
  JOIN descendants ON edge.parent_thread_id = descendants.id
)
SELECT COALESCE(group_concat(id || '|' || status, ','), '') FROM descendants
'@
  $row = Invoke-SqliteRow -DatabasePath $database -Sql $sql -Parameter $ThreadId -ColumnCount 1
  if (-not $row.ok -and $row.error -match '(?i)no such table.*thread_spawn_edges') {
    return [pscustomobject]@{ ok = $true; available = $false; entries = @(); error = '' }
  }
  if (-not $row.ok) {
    return [pscustomobject]@{ ok = $false; available = $true; entries = @(); error = $row.error }
  }
  $joined = if ($row.found) { [string]$row.values[0] } else { '' }
  $entries = @()
  foreach ($encoded in @($joined -split ',' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
    $separator = $encoded.IndexOf('|')
    if ($separator -lt 0) {
      $entries += [pscustomobject]@{ id = $encoded; status = '' }
      continue
    }
    $entries += [pscustomobject]@{
      id = $encoded.Substring(0, $separator)
      status = $encoded.Substring($separator + 1).ToLowerInvariant()
    }
  }
  return [pscustomobject]@{ ok = $true; available = $true; entries = $entries; error = '' }
}

function Get-ActiveDescendants {
  param(
    [object]$Record,
    [object]$Config,
    [int64]$NowUnixMs,
    [string]$SessionHome,
    [string]$SqliteHome
  )
  $threadId = [string](Get-ObjectValue $Record 'thread_id' '')
  $descendants = Get-DescendantThreadIds -ThreadId $threadId -SqliteHome $SqliteHome
  if (-not $descendants.ok) {
    return [pscustomobject]@{ ok = $false; busy = $false; count = 0; error = $descendants.error }
  }
  if (-not $descendants.available -or @($descendants.entries).Count -eq 0) {
    return [pscustomobject]@{ ok = $true; busy = $false; count = 0; error = '' }
  }
  $orphanMs = [int64]([Math]::Max(0, $Config.subagentOrphanSeconds) * 1000)
  $unknownSince = @{}
  $storedUnknownSince = Get-ObjectValue $Record 'descendant_unknown_since'
  if ($null -ne $storedUnknownSince) {
    if ($storedUnknownSince -is [Collections.IDictionary]) {
      foreach ($key in @($storedUnknownSince.Keys)) {
        try { $unknownSince[[string]$key] = [int64]$storedUnknownSince[$key] } catch { }
      }
    } else {
      foreach ($property in @($storedUnknownSince.PSObject.Properties)) {
        try { $unknownSince[[string]$property.Name] = [int64]$property.Value } catch { }
      }
    }
  }
  $active = 0
  $seenChildren = @{}
  foreach ($entry in @($descendants.entries)) {
    $childId = [string](Get-ObjectValue $entry 'id' '')
    if ([string]::IsNullOrWhiteSpace($childId)) { continue }
    $seenChildren[$childId] = $true
    $edgeStatus = ([string](Get-ObjectValue $entry 'status' '')).ToLowerInvariant()
    if ($edgeStatus -eq 'closed') {
      # The spawn graph is authoritative for a closed child. Its rollout may
      # still end in task_started because shutdown raced the final file write.
      [void]$unknownSince.Remove($childId)
      continue
    }
    $childInfo = Get-ThreadDatabaseInfo -ThreadId $childId -SqliteHome $SqliteHome -SessionHome $SessionHome
    if (-not $childInfo.ok) {
      return [pscustomobject]@{ ok = $false; busy = $false; count = $active; error = $childInfo.error }
    }
    $rolloutPath = if ($childInfo.found) { [string]$childInfo.rolloutPath } else { '' }
    if ([string]::IsNullOrWhiteSpace($rolloutPath)) {
      $rolloutPath = Find-RolloutPathByThread -ThreadId $childId -SessionHome $SessionHome
    }
    if ([string]::IsNullOrWhiteSpace($rolloutPath)) {
      $firstSeen = if ($unknownSince.ContainsKey($childId)) { [int64]$unknownSince[$childId] } else { [int64]0 }
      if ($firstSeen -le 0) {
        $firstSeen = $NowUnixMs
        $unknownSince[$childId] = $firstSeen
      }
      if ($orphanMs -le 0 -or $NowUnixMs - $firstSeen -lt $orphanMs) {
        $active++
      }
      continue
    }
    $probe = Update-RolloutProbe -Path $rolloutPath
    if (-not $probe.ok) {
      return [pscustomobject]@{ ok = $false; busy = $false; count = $active; error = $probe.error }
    }
    $lifecycle = [string](Get-ObjectValue $probe.state 'lastLifecycleType' 'unknown')
    $modifiedMs = [int64](Get-ObjectValue $probe.state 'modifiedUnixMs' 0)
    $fresh = $modifiedMs -gt 0 -and ($orphanMs -le 0 -or $NowUnixMs - $modifiedMs -lt $orphanMs)
    if (($lifecycle -eq 'task_started' -or $lifecycle -eq 'unknown') -and $fresh) {
      $active++
    } else {
      [void]$unknownSince.Remove($childId)
    }
  }
  foreach ($knownChild in @($unknownSince.Keys)) {
    if (-not $seenChildren.ContainsKey([string]$knownChild)) {
      [void]$unknownSince.Remove([string]$knownChild)
    }
  }
  Set-RecordValue -Record $Record -Name 'descendant_unknown_since' -Value $unknownSince
  return [pscustomobject]@{ ok = $true; busy = $active -gt 0; count = $active; error = '' }
}

function New-RolloutProbeState {
  param([string]$Path)
  return [pscustomobject]@{
    path = $Path
    offset = [int64]0
    carry = [byte[]]@()
    sequence = [int64]0
    openTurns = @{}
    completedTurns = @{}
    abortedTurns = @{}
    terminalTurns = @{}
    terminalEventTypes = @{}
    terminalMessages = @{}
    userMessageTurns = @{}
    finalMessageTurns = @{}
    currentTurnId = ''
    lastLifecycleType = 'unknown'
    lastLifecycleTurnId = ''
    goalStatus = ''
    modifiedUnixMs = [int64]0
    snapshotChanged = $false
  }
}

function Update-RolloutProbe {
  param([string]$Path)

  if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) {
    return [pscustomobject]@{ ok = $false; state = $null; error = 'rollout missing' }
  }
  $cacheKey = $Path.ToLowerInvariant()
  $state = if ($script:RolloutProbeCache.ContainsKey($cacheKey)) {
    $script:RolloutProbeCache[$cacheKey]
  } else {
    New-RolloutProbeState -Path $Path
  }
  $stream = $null
  $memory = $null
  try {
    $sharing = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $sharing)
    $snapshotInfo = Get-Item -LiteralPath $Path -ErrorAction Stop
    $snapshotLength = [int64]$snapshotInfo.Length
    $snapshotWriteTicks = [int64]$snapshotInfo.LastWriteTimeUtc.Ticks
    if ($snapshotLength -lt [int64]$state.offset) {
      $state = New-RolloutProbeState -Path $Path
    }
    $stream.Position = [int64]$state.offset
    $bytesRemaining = [int64]$snapshotLength - $stream.Position
    $memory = New-Object IO.MemoryStream
    $buffer = New-Object byte[] 65536
    while ($bytesRemaining -gt 0) {
      $requested = [int][Math]::Min([int64]$buffer.Length, $bytesRemaining)
      $count = $stream.Read($buffer, 0, $requested)
      if ($count -le 0) { break }
      $memory.Write($buffer, 0, $count)
      $bytesRemaining -= $count
    }
    $state.offset = $stream.Position
    $newBytes = $memory.ToArray()
    $carry = [byte[]]$state.carry
    $combined = New-Object byte[] ($carry.Length + $newBytes.Length)
    if ($carry.Length -gt 0) { [Array]::Copy($carry, 0, $combined, 0, $carry.Length) }
    if ($newBytes.Length -gt 0) { [Array]::Copy($newBytes, 0, $combined, $carry.Length, $newBytes.Length) }
    $lineStart = 0
    for ($index = 0; $index -lt $combined.Length; $index++) {
      if ($combined[$index] -ne 10) { continue }
      $lineLength = $index - $lineStart
      if ($lineLength -gt 0 -and $combined[$index - 1] -eq 13) { $lineLength-- }
      if ($lineLength -gt 0) {
        $line = $Utf8NoBom.GetString($combined, $lineStart, $lineLength).TrimStart([char]0xFEFF)
        if ($line -match '"(?:task_started|task_complete|turn_aborted|thread_goal_updated|user_message)"') {
          try {
            $item = $line | ConvertFrom-Json -ErrorAction Stop
            if ([string](Get-ObjectValue $item 'type' '') -eq 'event_msg') {
              $payload = Get-ObjectValue $item 'payload'
              $eventType = [string](Get-ObjectValue $payload 'type' '')
              $turn = [string](Get-FirstObjectValue $payload @('turn_id', 'turn-id', 'turnId'))
              $state.sequence = [int64]$state.sequence + 1
              if ($eventType -eq 'task_started' -and -not [string]::IsNullOrWhiteSpace($turn)) {
                $state.openTurns[$turn] = [int64]$state.sequence
                $state.lastLifecycleType = 'task_started'
                $state.lastLifecycleTurnId = $turn
                $state.currentTurnId = $turn
              } elseif ($eventType -eq 'task_complete' -and -not [string]::IsNullOrWhiteSpace($turn)) {
                [void]$state.openTurns.Remove($turn)
                $state.completedTurns[$turn] = [int64]$state.sequence
                $state.terminalTurns[$turn] = [int64]$state.sequence
                $state.terminalEventTypes[$turn] = 'task_complete'
                $state.lastLifecycleType = 'task_complete'
                $state.lastLifecycleTurnId = $turn
                $lastAgentMessage = [string](Get-FirstObjectValue $payload @('last_agent_message', 'last-assistant-message', 'last_assistant_message'))
                $state.terminalMessages[$turn] = Sanitize-NotificationText -Text $lastAgentMessage -MaxLength 4000 -PreserveLines
                if (-not [string]::IsNullOrWhiteSpace($lastAgentMessage)) { $state.finalMessageTurns[$turn] = $true }
                if ([string]$state.currentTurnId -eq $turn) { $state.currentTurnId = '' }
              } elseif ($eventType -eq 'turn_aborted' -and -not [string]::IsNullOrWhiteSpace($turn)) {
                [void]$state.openTurns.Remove($turn)
                $state.abortedTurns[$turn] = [int64]$state.sequence
                $state.terminalTurns[$turn] = [int64]$state.sequence
                $state.terminalEventTypes[$turn] = 'turn_aborted'
                $state.terminalMessages[$turn] = ''
                $state.lastLifecycleType = 'turn_aborted'
                $state.lastLifecycleTurnId = $turn
                $state.finalMessageTurns[$turn] = $true
                if ([string]$state.currentTurnId -eq $turn) { $state.currentTurnId = '' }
              } elseif ($eventType -eq 'user_message' -and -not [string]::IsNullOrWhiteSpace([string]$state.currentTurnId)) {
                $state.userMessageTurns[[string]$state.currentTurnId] = $true
              } elseif ($eventType -eq 'thread_goal_updated') {
                $goal = Get-ObjectValue $payload 'goal'
                $status = [string](Get-ObjectValue $goal 'status' '')
                if (-not [string]::IsNullOrWhiteSpace($status)) { $state.goalStatus = $status.ToLowerInvariant() }
              }
            }
          } catch {
            # Ignore complete non-JSON or future-format lines without losing the cursor.
          }
        }
      }
      $lineStart = $index + 1
    }
    $remaining = $combined.Length - $lineStart
    $state.carry = New-Object byte[] $remaining
    if ($remaining -gt 0) { [Array]::Copy($combined, $lineStart, $state.carry, 0, $remaining) }
    $itemInfo = Get-Item -LiteralPath $Path -ErrorAction Stop
    $state.modifiedUnixMs = ([DateTimeOffset]$itemInfo.LastWriteTimeUtc).ToUnixTimeMilliseconds()
    $state.snapshotChanged = [int64]$itemInfo.Length -ne $snapshotLength -or
      [int64]$itemInfo.LastWriteTimeUtc.Ticks -ne $snapshotWriteTicks
    $script:RolloutProbeCache[$cacheKey] = $state
    return [pscustomobject]@{ ok = $true; state = $state; snapshotChanged = [bool]$state.snapshotChanged; error = '' }
  } catch {
    return [pscustomobject]@{ ok = $false; state = $state; error = Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240 }
  } finally {
    if ($null -ne $memory) { $memory.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
  }
}

function New-GateResult {
  param(
    [string]$State,
    [string]$Reason,
    [int64]$RetryAtUnixMs
  )
  return [pscustomobject]@{ state = $State; reason = $Reason; retryAtUnixMs = $RetryAtUnixMs }
}

function Get-UnknownGateResult {
  param(
    [object]$Record,
    [object]$Config,
    [string]$Reason
  )
  $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  $created = [int64](Get-ObjectValue $Record 'created_unix_ms' $now)
  $fallbackAt = $created + [int64]([Math]::Max(0, $Config.idleProbeGraceSeconds) * 1000)
  if ($Config.idleDetectionMode -eq 'balanced' -and $now -ge $fallbackAt) {
    return New-GateResult -State 'ready' -Reason ('balanced-fallback:' + $Reason) -RetryAtUnixMs $now
  }
  $retryAt = [Math]::Min($now + [int64]([Math]::Max(0.1, $Config.goalPollSeconds) * 1000), $fallbackAt)
  if ($Config.idleDetectionMode -eq 'strict') {
    $retryAt = $now + [int64]([Math]::Max(0.1, $Config.goalPollSeconds) * 1000)
  }
  return New-GateResult -State 'unknown' -Reason $Reason -RetryAtUnixMs $retryAt
}

function Test-RecordIdleGate {
  param(
    [object]$Record,
    [object]$Config
  )

  $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  if ($Config.idleDetectionMode -eq 'off') {
    return New-GateResult -State 'ready' -Reason 'idle-detection-off' -RetryAtUnixMs $now
  }
  $threadId = [string](Get-ObjectValue $Record 'thread_id' '')
  $turnId = [string](Get-ObjectValue $Record 'turn_id' '')
  if ([string]::IsNullOrWhiteSpace($threadId)) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'candidate-thread-missing'
  }
  $sessionHome = [string](Get-ObjectValue $Record 'session_codex_home' $CodexHome)
  if ([string]::IsNullOrWhiteSpace($sessionHome)) { $sessionHome = $CodexHome }
  $sqliteHome = [string](Get-ObjectValue $Record 'session_sqlite_home' $sessionHome)
  if ([string]::IsNullOrWhiteSpace($sqliteHome)) { $sqliteHome = $sessionHome }
  $databaseInfo = Get-ThreadDatabaseInfo -ThreadId $threadId -SqliteHome $sqliteHome -SessionHome $sessionHome
  $classification = [string](Get-ObjectValue $Record 'session_classification' 'unknown')
  if ($databaseInfo.found -and $databaseInfo.classification -in @('root', 'subagent')) {
    $classification = $databaseInfo.classification
  } elseif ($classification -notin @('root', 'subagent')) {
    # A Stop hook can arrive before Codex persists its session row/rollout.
    # Reclassify on every strict probe so a later child session is suppressed
    # instead of remaining unknown forever (or being assumed to be root).
    $rolloutClassification = Get-EventClassification -Event $Record.event -ThreadId $threadId -SessionHome $sessionHome
    if ($rolloutClassification -in @('root', 'subagent')) {
      $classification = $rolloutClassification
      Set-RecordValue -Record $Record -Name 'session_classification' -Value $classification
    }
  }
  if ($classification -eq 'subagent' -and $Config.suppressSubagents) {
    return New-GateResult -State 'subagent' -Reason 'subagent' -RetryAtUnixMs $now
  }
  if ($classification -notin @('root', 'subagent')) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'classification-unknown'
  }
  $rolloutPath = if ($databaseInfo.found) { [string]$databaseInfo.rolloutPath } else { '' }
  if ([string]::IsNullOrWhiteSpace($rolloutPath)) {
    $rolloutPath = [string](Get-ObjectValue $Record 'candidate_rollout_path' '')
    $rolloutPath = Resolve-RolloutPath -DatabasePathValue $rolloutPath -SessionHome $sessionHome
  }
  if ([string]::IsNullOrWhiteSpace($rolloutPath)) {
    $rolloutPath = Find-RolloutPathByThread -ThreadId $threadId -SessionHome $sessionHome
  }
  if ([string]::IsNullOrWhiteSpace($rolloutPath)) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'rollout-path-unknown'
  }
  $probe = Update-RolloutProbe -Path $rolloutPath
  if (-not $probe.ok) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'rollout-probe-failed'
  }
  $state = $probe.state
  if ([bool](Get-ObjectValue $state 'snapshotChanged' $false)) {
    return New-GateResult -State 'busy' -Reason 'rollout-changing' -RetryAtUnixMs ($now + [int64]([Math]::Max(0.1, $Config.goalPollSeconds) * 1000))
  }
  if ([string]::IsNullOrWhiteSpace($turnId) -and $candidateKind -eq 'hook_stop') {
    $latestCompleted = $state.completedTurns.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1
    if ($null -ne $latestCompleted) {
      $turnId = [string]$latestCompleted.Key
      Set-RecordValue -Record $Record -Name 'turn_id' -Value $turnId
    }
  }
  if ([string]::IsNullOrWhiteSpace($turnId)) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'candidate-turn-missing'
  }
  $completionEventType = [string](Get-ObjectValue $Record 'completion_event_type' 'task_complete')
  if ([string]::IsNullOrWhiteSpace($completionEventType)) {
    $completionEventType = if ($state.abortedTurns.ContainsKey($turnId) -and -not $state.completedTurns.ContainsKey($turnId)) {
      'turn_aborted'
    } else {
      'task_complete'
    }
    Set-RecordValue -Record $Record -Name 'completion_event_type' -Value $completionEventType
  }
  $terminalTurns = if ($completionEventType -eq 'turn_aborted') { $state.abortedTurns } else { $state.completedTurns }
  if (-not $terminalTurns.ContainsKey($turnId)) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'candidate-task-complete-not-observed'
  }
  $completionSequence = [int64]$terminalTurns[$turnId]
  Set-RecordValue -Record $Record -Name 'candidate_rollout_path' -Value $rolloutPath
  Set-RecordValue -Record $Record -Name 'rollout_sequence' -Value $completionSequence
  $latestTerminal = $state.terminalTurns.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1
  if ($null -ne $latestTerminal -and [int64]$latestTerminal.Value -gt $completionSequence) {
    $latestTurnId = [string]$latestTerminal.Key
    $latestSequence = [int64]$latestTerminal.Value
    if ($latestTurnId -eq $turnId) {
      # A repeated terminal marker for the same logical turn is not a newer
      # notification candidate, but its later rollout position is authoritative.
      $completionSequence = $latestSequence
      Set-RecordValue -Record $Record -Name 'rollout_sequence' -Value $completionSequence
    } else {
      try {
        $latestEventType = if ($state.terminalEventTypes.ContainsKey($latestTurnId)) {
          [string]$state.terminalEventTypes[$latestTurnId]
        } else {
          'task_complete'
        }
        $latestMessage = ''
        if ($Config.includeMessage -and $state.terminalMessages.ContainsKey($latestTurnId)) {
          # The probe cache contains only sanitized text. With include_message
          # disabled, no rollout message is copied into durable notifier state.
          $latestMessage = [string]$state.terminalMessages[$latestTurnId]
        }
        $storedEvent = Get-ObjectValue $Record 'event'
        $recoveredEvent = [pscustomobject][ordered]@{
          type = 'agent-turn-complete'
          'thread-id' = $threadId
          'turn-id' = $latestTurnId
          cwd = [string](Get-ObjectValue $storedEvent 'cwd' '')
          'last-assistant-message' = $latestMessage
          'completion-event-type' = $latestEventType
          transcript_path = $rolloutPath
          'rollout-sequence' = $latestSequence
        }
        $recovered = New-EventRecord -Event $recoveredEvent -EventOrigin ([string](Get-ObjectValue $Record 'origin' (Get-DefaultOrigin))) -EventSessionHome $sessionHome -EventSqliteHome $sqliteHome -EventClassification $classification -EventIncludeMessage ([bool]$Config.includeMessage) -CandidateKind 'rollout_probe' -SourceEvent 'rollout-probe'
        $enqueueResult = Add-PendingEvent -Record $recovered
        if ([string]$enqueueResult.status -notin @('pending', 'queued', 'sent', 'suppressed', 'dead')) {
          throw "unexpected recovered candidate status: $($enqueueResult.status)"
        }
        Write-RuntimeLog "recovered newer completion key=$($recovered.key.Substring(0, 12)) from=$($Record.key.Substring(0, 12)) status=$($enqueueResult.status)"
        # B is durable (or already durably accounted for) before A is allowed
        # to be suppressed by the caller. A's identity and message never change.
        return New-GateResult -State 'superseded' -Reason 'newer-completion-recovered' -RetryAtUnixMs $now
      } catch {
        Write-RuntimeLog "failed to recover newer completion from=$($Record.key.Substring(0, 12)): $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240)"
        return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'newer-completion-recovery-failed'
      }
    }
  }
  foreach ($entry in $state.openTurns.GetEnumerator()) {
    if ([int64]$entry.Value -gt $completionSequence) {
      return New-GateResult -State 'busy' -Reason 'later-task-open' -RetryAtUnixMs ($now + [int64]([Math]::Max(0.1, $Config.goalPollSeconds) * 1000))
    }
  }
  $goal = Get-GoalDatabaseStatus -ThreadId $threadId -SqliteHome $sqliteHome
  if (-not $goal.ok) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'goal-probe-failed'
  }
  $rolloutGoalStatus = [string]$state.goalStatus
  $goalStatus = if ($goal.found) {
    $goal.status
  } elseif ($goal.available) {
    if ($rolloutGoalStatus -in @('paused', 'blocked', 'usage_limited', 'budget_limited', 'complete')) { $rolloutGoalStatus } else { '' }
  } else {
    $rolloutGoalStatus
  }
  Set-RecordValue -Record $Record -Name 'goal_status' -Value $goalStatus
  if ($Config.goalAware -and $goalStatus -eq 'active') {
    return New-GateResult -State 'busy' -Reason 'goal-active' -RetryAtUnixMs ($now + [int64]([Math]::Max(0.1, $Config.goalPollSeconds) * 1000))
  }
  $descendants = Get-ActiveDescendants -Record $Record -Config $Config -NowUnixMs $now -SessionHome $sessionHome -SqliteHome $sqliteHome
  Set-RecordValue -Record $Record -Name 'active_descendants' -Value ([int]$descendants.count)
  if (-not $descendants.ok) {
    return Get-UnknownGateResult -Record $Record -Config $Config -Reason 'descendant-probe-failed'
  }
  if ($descendants.busy) {
    return New-GateResult -State 'busy' -Reason 'subagents-active' -RetryAtUnixMs ($now + [int64]([Math]::Max(0.1, $Config.goalPollSeconds) * 1000))
  }
  $quietAt = [int64]$state.modifiedUnixMs + [int64]([Math]::Max(0, $Config.idleGraceSeconds) * 1000)
  if ($now -lt $quietAt) {
    return New-GateResult -State 'busy' -Reason 'rollout-not-quiet' -RetryAtUnixMs $quietAt
  }
  $sourceEvent = [string](Get-ObjectValue $Record 'source_event' '')
  $goalIsTerminal = $goalStatus -in @('paused', 'blocked', 'usage_limited', 'budget_limited', 'complete')
  if ($Config.suppressTechnicalTurns -and $sourceEvent -ne 'Stop' -and -not $goalIsTerminal) {
    $hasUserMessage = $state.userMessageTurns.ContainsKey($turnId)
    $hasFinalMessage = $state.finalMessageTurns.ContainsKey($turnId)
    if (-not $hasUserMessage -or -not $hasFinalMessage) {
      return New-GateResult -State 'technical' -Reason 'technical-turn' -RetryAtUnixMs $now
    }
  }
  return New-GateResult -State 'ready' -Reason 'thread-idle' -RetryAtUnixMs $now
}

function Add-RolloutWatchEntry {
  param(
    [hashtable]$Found,
    [object]$File,
    [string]$SessionHome,
    [string]$SqliteHome,
    [string]$RootOrigin,
    [bool]$ForceReplay = $false
  )
  if ($null -eq $File -or [string]::IsNullOrWhiteSpace([string]$File.FullName)) { return }
  $fullPathKey = $File.FullName.ToLowerInvariant()
  if ($Found.ContainsKey($fullPathKey)) {
    if ($ForceReplay) { Set-RecordValue -Record $Found[$fullPathKey] -Name 'force_replay' -Value $true }
    return
  }
  $Found[$fullPathKey] = [pscustomobject]@{
    file = $File
    rollout_path = $File.FullName
    session_codex_home = $SessionHome
    session_sqlite_home = $SqliteHome
    origin = $RootOrigin
    force_replay = $ForceReplay
  }
}

function Get-RecentRolloutFiles {
  param([object]$Config)

  $found = @{}
  $localSqliteHome = [string](Get-ObjectValue $Config 'workerSqlitePath' $CodexHome)
  if ([string]::IsNullOrWhiteSpace($localSqliteHome)) { $localSqliteHome = $CodexHome }
  $roots = @([pscustomobject]@{
      path = $CodexHome
      session_codex_home = $CodexHome
      session_sqlite_home = $localSqliteHome
      origin = ''
    })
  if ($null -ne $Config) { $roots += @(Get-ObjectValue $Config 'watchRoots' @()) }
  $today = [DateTime]::Now.Date
  $nowUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  $discoverySeconds = [Math]::Max(0.1, [double](Get-ObjectValue $Config 'watchDiscoverySeconds' 60))
  $initialReplaySeconds = [Math]::Max(0, [double](Get-ObjectValue $Config 'watchInitialReplaySeconds' 15))
  $recentWindowSeconds = [Math]::Max(2 * $discoverySeconds, $initialReplaySeconds)
  $recentCutoffUtc = [DateTime]::UtcNow.AddSeconds(-$recentWindowSeconds)
  $initialReplayCutoffUtc = [DateTime]::UtcNow.AddSeconds(-$initialReplaySeconds)
  $durableCursorPaths = @{}

  # Cursor paths remain authoritative even after a session moves outside the
  # today/yesterday layout or into archived_sessions.
  foreach ($cursorFile in @(Get-ChildItem -LiteralPath $WatchDir -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
    try {
      $cursor = Read-JsonFile -Path $cursorFile.FullName
      $rolloutPath = [string](Get-ObjectValue $cursor 'rollout_path' '')
      if ([string]::IsNullOrWhiteSpace($rolloutPath)) { continue }
      $durableCursorPaths[$rolloutPath.ToLowerInvariant()] = $true
      if (-not (Test-Path -LiteralPath $rolloutPath -PathType Leaf -ErrorAction SilentlyContinue)) { continue }
      $sessionHome = [string](Get-ObjectValue $cursor 'session_codex_home' '')
      $sqliteHome = [string](Get-ObjectValue $cursor 'session_sqlite_home' '')
      $rootOrigin = [string](Get-ObjectValue $cursor 'origin' '')
      foreach ($root in @($roots | Sort-Object { ([string](Get-ObjectValue $_ 'path' '')).Length } -Descending)) {
        $rootPath = ([string](Get-ObjectValue $root 'path' '')).TrimEnd('\', '/')
        if ([string]::IsNullOrWhiteSpace($rootPath)) { continue }
        $underRoot = $rolloutPath.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase) -or
          $rolloutPath.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase) -or
          $rolloutPath.StartsWith($rootPath + '/', [StringComparison]::OrdinalIgnoreCase)
        if (-not $underRoot) { continue }
        if ([string]::IsNullOrWhiteSpace($sessionHome)) { $sessionHome = [string](Get-ObjectValue $root 'session_codex_home' $rootPath) }
        if ([string]::IsNullOrWhiteSpace($sqliteHome)) { $sqliteHome = [string](Get-ObjectValue $root 'session_sqlite_home' $sessionHome) }
        if ([string]::IsNullOrWhiteSpace($rootOrigin)) { $rootOrigin = [string](Get-ObjectValue $root 'origin' '') }
        break
      }
      if ([string]::IsNullOrWhiteSpace($sessionHome)) {
        $normalized = $rolloutPath.Replace('/', '\')
        $markerIndex = $normalized.IndexOf('\sessions\', [StringComparison]::OrdinalIgnoreCase)
        if ($markerIndex -lt 0) { $markerIndex = $normalized.IndexOf('\archived_sessions\', [StringComparison]::OrdinalIgnoreCase) }
        if ($markerIndex -gt 0) { $sessionHome = $normalized.Substring(0, $markerIndex) }
      }
      if ([string]::IsNullOrWhiteSpace($sessionHome)) { continue }
      if ([string]::IsNullOrWhiteSpace($sqliteHome)) { $sqliteHome = $sessionHome }
      $file = Get-Item -LiteralPath $rolloutPath -ErrorAction Stop
      Add-RolloutWatchEntry -Found $found -File $file -SessionHome $sessionHome -SqliteHome $sqliteHome -RootOrigin $rootOrigin
    } catch {
      continue
    }
  }

  foreach ($root in @($roots)) {
    try {
      $sessionHome = [string](Get-ObjectValue $root 'session_codex_home' (Get-ObjectValue $root 'path' ''))
      if ([string]::IsNullOrWhiteSpace($sessionHome)) { continue }
      $sqliteHome = [string](Get-ObjectValue $root 'session_sqlite_home' $sessionHome)
      if ([string]::IsNullOrWhiteSpace($sqliteHome)) { $sqliteHome = $sessionHome }
      $rootOrigin = [string](Get-ObjectValue $root 'origin' '')
      $sessions = Join-Path $sessionHome 'sessions'
      $quickFiles = @()
      if (Test-Path -LiteralPath $sessions -PathType Container -ErrorAction SilentlyContinue) {
        foreach ($daysBack in @(0, 1)) {
          $day = $today.AddDays(-$daysBack)
          $directory = Join-Path (Join-Path (Join-Path $sessions $day.ToString('yyyy')) $day.ToString('MM')) $day.ToString('dd')
          if (Test-Path -LiteralPath $directory -PathType Container -ErrorAction SilentlyContinue) {
            $quickFiles += @(Get-ChildItem -LiteralPath $directory -Filter '*.jsonl' -File -ErrorAction SilentlyContinue)
          }
        }
        $quickFiles += @(Get-ChildItem -LiteralPath $sessions -Filter '*.jsonl' -File -ErrorAction SilentlyContinue)
      }
      $quickPathSet = @{}
      foreach ($file in @($quickFiles)) {
        if ($null -ne $file) { $quickPathSet[$file.FullName.ToLowerInvariant()] = $true }
        Add-RolloutWatchEntry -Found $found -File $file -SessionHome $sessionHome -SqliteHome $sqliteHome -RootOrigin $rootOrigin
      }

      $cacheKey = $sessionHome.ToLowerInvariant()
      $cache = if ($script:RolloutDiscoveryCache.ContainsKey($cacheKey)) { $script:RolloutDiscoveryCache[$cacheKey] } else { $null }
      if ($null -eq $cache -or $nowUnixMs -ge [int64](Get-ObjectValue $cache 'next_unix_ms' 0)) {
        $firstDiscovery = $null -eq $cache
        $previousForcePaths = @{}
        if (-not $firstDiscovery) {
          foreach ($previousForcePath in @(Get-ObjectValue $cache 'force_replay_paths' @())) {
            $previousForcePaths[([string]$previousForcePath).ToLowerInvariant()] = $true
          }
        }
        $discoveredPaths = @()
        $forceReplayPaths = @()
        $discoveredCount = 0
        foreach ($rootName in @('sessions', 'archived_sessions')) {
          $searchRoot = Join-Path $sessionHome $rootName
          if (-not (Test-Path -LiteralPath $searchRoot -PathType Container -ErrorAction SilentlyContinue)) { continue }
          $remainingCapacity = 4096 - $discoveredCount
          $recentFiles = @(Get-ChildItem -LiteralPath $searchRoot -Filter '*.jsonl' -File -Recurse -ErrorAction SilentlyContinue |
              Where-Object { $_.LastWriteTimeUtc -ge $recentCutoffUtc } |
              Select-Object -First $remainingCapacity)
          foreach ($file in $recentFiles) {
            $discoveredPaths += $file.FullName
            $discoveryPathKey = $file.FullName.ToLowerInvariant()
            $withinInitialReplay = $file.LastWriteTimeUtc -ge $initialReplayCutoffUtc
            $newOldLocation = -not $firstDiscovery -and -not $quickPathSet.ContainsKey($discoveryPathKey)
            if (-not $durableCursorPaths.ContainsKey($discoveryPathKey) -and
                ($previousForcePaths.ContainsKey($discoveryPathKey) -or $withinInitialReplay -or $newOldLocation)) {
              $forceReplayPaths += $file.FullName
            }
            $discoveredCount++
          }
          if ($discoveredCount -ge 4096) { break }
        }
        $cache = [pscustomobject]@{
          next_unix_ms = $nowUnixMs + [int64]($discoverySeconds * 1000)
          paths = @($discoveredPaths)
          force_replay_paths = @($forceReplayPaths)
        }
        $script:RolloutDiscoveryCache[$cacheKey] = $cache
      }
      $forceReplaySet = @{}
      foreach ($forcePath in @(Get-ObjectValue $cache 'force_replay_paths' @())) {
        $forceReplaySet[([string]$forcePath).ToLowerInvariant()] = $true
      }
      foreach ($discoveredPath in @(Get-ObjectValue $cache 'paths' @())) {
        try {
          $file = Get-Item -LiteralPath ([string]$discoveredPath) -ErrorAction Stop
          $forceReplay = $forceReplaySet.ContainsKey($file.FullName.ToLowerInvariant())
          Add-RolloutWatchEntry -Found $found -File $file -SessionHome $sessionHome -SqliteHome $sqliteHome -RootOrigin $rootOrigin -ForceReplay $forceReplay
        } catch {
          continue
        }
      }
    } catch {
      # A stopped WSL distro or unavailable root is expected. Never log its path.
      continue
    }
  }
  return @($found.Values)
}

function Get-RolloutMetadata {
  param([string]$Path)
  try {
    $line = Read-FirstLineShared -Path $Path
    if ([string]::IsNullOrWhiteSpace($line)) { return [pscustomobject]@{} }
    $envelope = $line | ConvertFrom-Json -ErrorAction Stop
    $payload = Get-ObjectValue $envelope 'payload'
    if ($null -eq $payload -or $payload -is [string]) { return [pscustomobject]@{} }
    return $payload
  } catch {
    return [pscustomobject]@{}
  }
}

function Scan-RolloutFile {
  param(
    [object]$Entry,
    [object]$Config,
    [int64]$NowUnixMs
  )

  $File = Get-ObjectValue $Entry 'file'
  if ($null -eq $File) { return 0 }
  $sessionHome = [string](Get-ObjectValue $Entry 'session_codex_home' $CodexHome)
  if ([string]::IsNullOrWhiteSpace($sessionHome)) { $sessionHome = $CodexHome }
  $sqliteHome = [string](Get-ObjectValue $Entry 'session_sqlite_home' $sessionHome)
  if ([string]::IsNullOrWhiteSpace($sqliteHome)) { $sqliteHome = $sessionHome }
  $rootOrigin = [string](Get-ObjectValue $Entry 'origin' '')
  $forceReplay = [bool](Get-ObjectValue $Entry 'force_replay' $false)
  $stateId = Get-Sha256Hex $File.FullName
  $statePath = Join-Path $WatchDir ($stateId + '.json')
  $state = $null
  try { $state = Read-JsonFile -Path $statePath } catch { $state = $null }
  $stateWasMissing = $null -eq $state
  $fileInfo = $null
  try { $fileInfo = Get-Item -LiteralPath $File.FullName } catch { return 0 }
  if ($null -eq $state) {
    $modifiedMs = ([DateTimeOffset]$fileInfo.LastWriteTimeUtc).ToUnixTimeMilliseconds()
    $replayMs = [int64]([Math]::Max(0, $Config.watchInitialReplaySeconds) * 1000)
    $offset = if ($forceReplay -or $NowUnixMs - $modifiedMs -le $replayMs) { [int64]0 } else { [int64]$fileInfo.Length }
  } else {
    $offset = [int64](Get-ObjectValue $state 'offset' 0)
    if ($offset -lt 0 -or $offset -gt [int64]$fileInfo.Length) { $offset = [int64]$fileInfo.Length }
  }

  $stream = $null
  $memory = $null
  try {
    $sharing = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.FileStream]::new($File.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, $sharing)
    $snapshotLength = $stream.Length
    if ($offset -gt $snapshotLength) { $offset = $snapshotLength }
    $stream.Position = $offset
    $remaining = [int64]$snapshotLength - $offset
    $memory = New-Object IO.MemoryStream
    $buffer = New-Object byte[] 65536
    while ($remaining -gt 0) {
      $requested = [int][Math]::Min([int64]$buffer.Length, $remaining)
      $count = $stream.Read($buffer, 0, $requested)
      if ($count -le 0) { break }
      $memory.Write($buffer, 0, $count)
      $remaining -= $count
    }
    $data = $memory.ToArray()
  } catch {
    return 0
  } finally {
    if ($null -ne $memory) { $memory.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
  }

  $lastNewline = -1
  for ($index = $data.Length - 1; $index -ge 0; $index--) {
    if ($data[$index] -eq 10) { $lastNewline = $index; break }
  }
  if ($lastNewline -lt 0) {
    Write-JsonAtomic -Path $statePath -Value ([ordered]@{
        schema = 1
        rollout_path = $File.FullName
        session_codex_home = $sessionHome
        session_sqlite_home = $sqliteHome
        origin = $rootOrigin
        offset = $offset
        seen_unix_ms = $NowUnixMs
      })
    return 0
  }

  if ($stateWasMissing) {
    # Persist the starting cursor before accounting any completion. If queueing
    # fails, the next scan must retry from here even after the replay window.
    Write-JsonAtomic -Path $statePath -Value ([ordered]@{
        schema = 1
        rollout_path = $File.FullName
        session_codex_home = $sessionHome
        session_sqlite_home = $sqliteHome
        origin = $rootOrigin
        offset = $offset
        seen_unix_ms = $NowUnixMs
      })
  }
  $completeText = $Utf8NoBom.GetString($data, 0, $lastNewline + 1)
  $newOffset = $offset + $lastNewline + 1
  $metadata = Get-RolloutMetadata -Path $File.FullName
  $threadId = [string](Get-FirstObjectValue $metadata @('id', 'thread_id', 'threadId'))
  if ([string]::IsNullOrWhiteSpace($threadId) -and $null -ne $state) {
    $threadId = [string](Get-ObjectValue $state 'thread_id' '')
  }
  $cwd = [string](Get-ObjectValue $metadata 'cwd' '')
  $source = Get-ObjectValue $metadata 'source'
  $originator = [string](Get-ObjectValue $metadata 'originator' '')
  $observed = 0
  $completionMissingIdentity = $false
  foreach ($line in @($completeText -split "`n")) {
    if ($line -notmatch '"(?:task_complete|turn_aborted)"') { continue }
    $envelope = $null
    $payload = $null
    $event = $null
    try {
      $envelope = $line.TrimEnd("`r") | ConvertFrom-Json -ErrorAction Stop
      $payload = Get-ObjectValue $envelope 'payload'
      $eventType = [string](Get-ObjectValue $payload 'type' '')
      if ($eventType -notin @('task_complete', 'turn_aborted')) { continue }
      $turnId = [string](Get-FirstObjectValue $payload @('turn_id', 'turnId'))
      if ([string]::IsNullOrWhiteSpace($threadId)) {
        $completionMissingIdentity = $true
        continue
      }
      if ([string]::IsNullOrWhiteSpace($turnId)) { continue }
      $lastMessage = if ($eventType -eq 'task_complete') {
        [string](Get-FirstObjectValue $payload @('last_agent_message', 'last-assistant-message', 'last_assistant_message'))
      } else {
        ''
      }
      $event = [pscustomobject][ordered]@{
        type = 'agent-turn-complete'
        'thread-id' = $threadId
        'turn-id' = $turnId
        cwd = $cwd
        'last-assistant-message' = $lastMessage
        'completion-event-type' = $eventType
        source = $source
        transcript_path = $File.FullName
      }
    } catch {
      # Malformed or future-format rollout lines are non-actionable. Durable
      # queue/receipt operations below intentionally remain outside this catch.
      continue
    }
    $classification = Get-EventClassification -Event $event -ThreadId $threadId -SessionHome $sessionHome
    $eventOrigin = if (-not [string]::IsNullOrWhiteSpace($rootOrigin)) {
      $rootOrigin
    } elseif (-not [string]::IsNullOrWhiteSpace($originator)) {
      $originator
    } else {
      Get-DefaultOrigin
    }
    $record = New-EventRecord -Event $event -EventOrigin $eventOrigin -EventSessionHome $sessionHome -EventSqliteHome $sqliteHome -EventClassification $classification -EventIncludeMessage $Config.includeMessage -CandidateKind 'rollout_watch' -SourceEvent 'rollout-watch'
    if ($Config.suppressSubagents -and $classification -eq 'subagent') {
      Move-ToSuppressed -Path (Join-Path $PendingDir ($record.key + '.json')) -Record $record -Reason 'subagent'
    } else {
      [void](Add-CandidateEvent -Record $record -Config $Config)
    }
    $observed++
  }
  if ($completionMissingIdentity) {
    Write-RuntimeLog "watcher retained cursor: session identity unavailable path_hash=$($stateId.Substring(0, 12))"
    return 0
  }
  # This cursor advances only after every actionable line above has a durable
  # pending/outbox record or suppression receipt. Queue failures propagate.
  Write-JsonAtomic -Path $statePath -Value ([ordered]@{
      schema = 1
      rollout_path = $File.FullName
      session_codex_home = $sessionHome
      session_sqlite_home = $sqliteHome
      origin = $rootOrigin
      offset = $newOffset
      seen_unix_ms = $NowUnixMs
      thread_id = $threadId
    })
  return $observed
}

function Invoke-RolloutWatchScan {
  param(
    [object]$Config,
    [int64]$NowUnixMs
  )
  $observed = 0
  foreach ($entry in @(Get-RecentRolloutFiles -Config $Config)) {
    $observed += Scan-RolloutFile -Entry $entry -Config $Config -NowUnixMs $NowUnixMs
  }
  return $observed
}

function Assert-QueuedRecord {
  param(
    [object]$Record,
    [string]$ExpectedKey
  )

  if ($null -eq $Record) {
    throw 'queue item is null'
  }
  if ([int](Get-ObjectValue $Record 'schema' 0) -ne 1) {
    throw 'queue item has an unsupported schema'
  }
  $key = [string](Get-ObjectValue $Record 'key' '')
  if ($key -notmatch '^[0-9a-f]{64}$' -or $key -ne $ExpectedKey) {
    throw 'queue item has an invalid key'
  }
  $sequenceId = [string](Get-ObjectValue $Record 'sequence_id' '')
  if ($sequenceId -notmatch '^codex-[0-9a-f]{32}$') {
    throw 'queue item has an invalid sequence ID'
  }
  $event = Get-ObjectValue $Record 'event'
  if ($null -eq $event -or $event -is [string]) {
    throw 'queue item has an invalid event'
  }
  [void][int64](Get-ObjectValue $Record 'created_unix_ms' 0)
  $attempts = [int](Get-ObjectValue $Record 'attempts' -1)
  if ($attempts -lt 0) {
    throw 'queue item has invalid attempts'
  }
}

function Set-RecordValue {
  param(
    [object]$Record,
    [string]$Name,
    [object]$Value
  )
  $property = $Record.PSObject.Properties[$Name]
  if ($null -eq $property) {
    Add-Member -InputObject $Record -MemberType NoteProperty -Name $Name -Value $Value
  } else {
    $property.Value = $Value
  }
}

function Move-QueueRecord {
  param(
    [string]$SourcePath,
    [string]$DestinationDirectory,
    [object]$Record
  )
  $destination = Join-Path $DestinationDirectory ($Record.key + '.json')
  if (Test-Path -LiteralPath $destination) {
    Remove-Item -LiteralPath $SourcePath -Force -ErrorAction SilentlyContinue
    return $destination
  }
  try {
    [IO.File]::Move($SourcePath, $destination)
  } catch [IO.IOException] {
    if (Test-Path -LiteralPath $destination) {
      Remove-Item -LiteralPath $SourcePath -Force -ErrorAction SilentlyContinue
    } else {
      throw
    }
  }
  return $destination
}

function Resolve-QueueReceiptConflict {
  param(
    [string]$Path,
    [object]$Record
  )
  return Invoke-WithRecordMutationLock -Key ([string]$Record.key) -Action {
    param($lockedPath, $lockedRecord)
    if (-not (Test-Path -LiteralPath $lockedPath)) {
      return [pscustomobject]@{ keep = $false; reason = 'missing'; record = $null }
    }
    $current = Read-JsonFile -Path $lockedPath
    Assert-QueuedRecord -Record $current -ExpectedKey $lockedRecord.key
    $sentPath = Join-Path $SentDir ($lockedRecord.key + '.json')
    if (Test-Path -LiteralPath $sentPath) {
      Remove-Item -LiteralPath $lockedPath -Force -ErrorAction SilentlyContinue
      return [pscustomobject]@{ keep = $false; reason = 'sent'; record = $null }
    }
    $suppressedPath = Join-Path $SuppressedDir ($lockedRecord.key + '.json')
    if (Test-Path -LiteralPath $suppressedPath) {
      if ((Test-IsStopEvidence -Record $current) -and
          (Remove-TechnicalSuppressionForStopCore -Path $suppressedPath -Record $current)) {
        return [pscustomobject]@{ keep = $true; reason = 'stop-revived'; record = $current }
      }
      Remove-Item -LiteralPath $lockedPath -Force -ErrorAction SilentlyContinue
      return [pscustomobject]@{ keep = $false; reason = 'suppressed'; record = $null }
    }
    return [pscustomobject]@{ keep = $true; reason = 'clear'; record = $current }
  } -Arguments @($Path, $Record)
}

function Commit-PendingRecord {
  param(
    [string]$Path,
    [object]$Record,
    [switch]$Promote
  )
  return Invoke-WithRecordMutationLock -Key ([string]$Record.key) -Action {
    param($lockedPath, $incomingRecord, $shouldPromote)
    if (-not (Test-Path -LiteralPath $lockedPath)) {
      return [pscustomobject]@{ status = 'missing' }
    }
    $canonical = Read-JsonFile -Path $lockedPath
    Assert-QueuedRecord -Record $canonical -ExpectedKey $incomingRecord.key
    $canonicalIsStop = Test-IsStopEvidence -Record $canonical
    $incomingIsStop = Test-IsStopEvidence -Record $incomingRecord
    if ($canonicalIsStop -and -not $incomingIsStop) {
      return [pscustomobject]@{ status = 'stop-won' }
    }
    $writeRecord = if ($canonicalIsStop) { $canonical } else { $incomingRecord }
    if ($canonicalIsStop) {
      foreach ($name in @('next_attempt_unix_ms', 'gate_reason', 'goal_status', 'completion_event_type', 'active_descendants', 'descendant_unknown_since', 'candidate_rollout_path', 'rollout_sequence')) {
        $property = $incomingRecord.PSObject.Properties[$name]
        if ($null -ne $property) { Set-RecordValue -Record $writeRecord -Name $name -Value $property.Value }
      }
    }
    if (-not [bool]$shouldPromote) {
      Write-JsonAtomic -Path $lockedPath -Value $writeRecord
      return [pscustomobject]@{ status = 'updated' }
    }

    Write-JsonAtomic -Path $lockedPath -Value $writeRecord
    $outboxPath = Join-Path $OutboxDir ($writeRecord.key + '.json')
    if (Test-Path -LiteralPath $outboxPath) {
      Remove-Item -LiteralPath $lockedPath -Force -ErrorAction SilentlyContinue
      return [pscustomobject]@{ status = 'already-promoted' }
    }
    [void](Move-QueueRecord -SourcePath $lockedPath -DestinationDirectory $OutboxDir -Record $writeRecord)
    return [pscustomobject]@{ status = 'promoted' }
  } -Arguments @($Path, $Record, [bool]$Promote)
}

function Test-UuidV7TurnId {
  param([string]$TurnId)
  return -not [string]::IsNullOrWhiteSpace($TurnId) -and
    $TurnId -match '^(?i:[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$'
}

function Compare-CandidateOrder {
  param(
    [object]$Left,
    [object]$Right
  )
  $leftRollout = [string](Get-ObjectValue $Left 'candidate_rollout_path' '')
  $rightRollout = [string](Get-ObjectValue $Right 'candidate_rollout_path' '')
  $leftSequence = [int64](Get-FirstObjectValue $Left @('rollout_sequence', 'completion_end_offset', 'rollout_offset'))
  $rightSequence = [int64](Get-FirstObjectValue $Right @('rollout_sequence', 'completion_end_offset', 'rollout_offset'))
  if (-not [string]::IsNullOrWhiteSpace($leftRollout) -and
      $leftRollout.Equals($rightRollout, [StringComparison]::OrdinalIgnoreCase) -and
      $leftSequence -gt 0 -and $rightSequence -gt 0) {
    if ($leftSequence -lt $rightSequence) { return -1 }
    if ($leftSequence -gt $rightSequence) { return 1 }
    return 0
  }
  $leftTurn = [string](Get-ObjectValue $Left 'turn_id' '')
  $rightTurn = [string](Get-ObjectValue $Right 'turn_id' '')
  if ((Test-UuidV7TurnId -TurnId $leftTurn) -and (Test-UuidV7TurnId -TurnId $rightTurn)) {
    $turnComparison = [String]::CompareOrdinal($leftTurn.ToLowerInvariant(), $rightTurn.ToLowerInvariant())
    if ($turnComparison -lt 0) { return -1 }
    if ($turnComparison -gt 0) { return 1 }
    return 0
  }
  # Creation time and the content hash describe queue arrival, not logical
  # rollout order. When neither proof is available, retain both candidates.
  return 0
}

function Coalesce-ThreadCandidates {
  param([object]$Config)
  if ($null -eq $Config -or $Config.idleDetectionMode -eq 'off') { return }
  $newest = @{}
  $files = @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object CreationTimeUtc, Name)
  foreach ($file in $files) {
    try {
      $record = Read-JsonFile -Path $file.FullName
      Assert-QueuedRecord -Record $record -ExpectedKey $file.BaseName
    } catch {
      $badRecord = [pscustomobject]@{ schema = 1; key = $file.BaseName; last_error = 'invalid queue JSON'; failed_at = [DateTimeOffset]::UtcNow.ToString('o') }
      Move-ToDeadLetter -Path $file.FullName -Record $badRecord
      Write-RuntimeLog "dead-letter invalid candidate key=$($file.BaseName.Substring(0, [Math]::Min(12, $file.BaseName.Length)))"
      continue
    }
    $thread = [string](Get-ObjectValue $record 'thread_id' '')
    if ([string]::IsNullOrWhiteSpace($thread)) { continue }
    $candidate = [pscustomobject]@{
      path = $file.FullName
      record = $record
    }
    if (-not $newest.ContainsKey($thread)) {
      $newest[$thread] = $candidate
      continue
    }
    $current = $newest[$thread]
    if ($current.record.key -eq $record.key) {
      Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
      continue
    }
    $comparison = Compare-CandidateOrder -Left $record -Right $current.record
    if ($comparison -gt 0) {
      Move-ToSuppressed -Path $current.path -Record $current.record -Reason 'superseded'
      Write-RuntimeLog "superseded idle candidate key=$($current.record.key.Substring(0, 12)) thread=$($thread.Substring(0, [Math]::Min(8, $thread.Length)))"
      $newest[$thread] = $candidate
    } elseif ($comparison -lt 0) {
      Move-ToSuppressed -Path $file.FullName -Record $record -Reason 'superseded'
      Write-RuntimeLog "superseded idle candidate key=$($record.key.Substring(0, 12)) thread=$($thread.Substring(0, [Math]::Min(8, $thread.Length)))"
    }
  }
}

function Process-PendingCandidates {
  param([object]$Config)

  $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  $nextDue = $null
  $files = @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object CreationTimeUtc, Name)
  foreach ($file in $files) {
    try {
      $record = Read-JsonFile -Path $file.FullName
      Assert-QueuedRecord -Record $record -ExpectedKey $file.BaseName
    } catch {
      $badRecord = [pscustomobject]@{ schema = 1; key = $file.BaseName; last_error = 'invalid pending JSON'; failed_at = [DateTimeOffset]::UtcNow.ToString('o') }
      Move-ToDeadLetter -Path $file.FullName -Record $badRecord
      continue
    }
    $conflict = Resolve-QueueReceiptConflict -Path $file.FullName -Record $record
    if (-not $conflict.keep) { continue }
    $record = $conflict.record
    $due = [int64](Get-ObjectValue $record 'next_attempt_unix_ms' 0)
    if ($due -gt $now) {
      if ($null -eq $nextDue -or $due -lt $nextDue) { $nextDue = $due }
      continue
    }
    $promote = $true
    $gate = $null
    for ($confirmation = 1; $confirmation -le 2; $confirmation++) {
      # The second pass is the final epoch check. It happens while the record is
      # still pending, immediately before the durable move into the outbox.
      $gate = Test-RecordIdleGate -Record $record -Config $Config
      if ($gate.state -eq 'subagent') {
        Move-ToSuppressed -Path $file.FullName -Record $record -Reason 'subagent'
        $promote = $false
        break
      }
      if ($gate.state -eq 'technical') {
        Move-ToSuppressed -Path $file.FullName -Record $record -Reason 'technical-turn'
        Write-RuntimeLog "suppressed technical turn key=$($record.key.Substring(0, 12))"
        $promote = $false
        break
      }
      if ($gate.state -eq 'superseded') {
        Move-ToSuppressed -Path $file.FullName -Record $record -Reason 'superseded'
        Write-RuntimeLog "suppressed recovered predecessor key=$($record.key.Substring(0, 12))"
        $promote = $false
        break
      }
      if ($gate.state -ne 'ready') {
        Set-RecordValue -Record $record -Name 'next_attempt_unix_ms' -Value ([int64]$gate.retryAtUnixMs)
        Set-RecordValue -Record $record -Name 'gate_reason' -Value $gate.reason
        $commit = Commit-PendingRecord -Path $file.FullName -Record $record
        if ($null -eq $nextDue -or [int64]$gate.retryAtUnixMs -lt $nextDue) { $nextDue = [int64]$gate.retryAtUnixMs }
        if ($commit.status -eq 'stop-won') { $nextDue = $now }
        $promote = $false
        break
      }
    }
    if (-not $promote) { continue }

    Set-RecordValue -Record $record -Name 'next_attempt_unix_ms' -Value $now
    Set-RecordValue -Record $record -Name 'gate_reason' -Value $gate.reason
    $commit = Commit-PendingRecord -Path $file.FullName -Record $record -Promote
    if ($commit.status -in @('promoted', 'already-promoted')) {
      Write-RuntimeLog "idle candidate promoted key=$($record.key.Substring(0, 12)) reason=$($gate.reason)"
    } elseif ($commit.status -eq 'stop-won') {
      if ($null -eq $nextDue -or $now -lt $nextDue) { $nextDue = $now }
    }
  }
  return [pscustomobject]@{ nextDueUnixMs = $nextDue }
}

function Invoke-OutboxWorker {
  Ensure-RuntimeDirectories
  $lockStream = $null
  try {
    $lockStream = [System.IO.File]::Open($WorkerLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  } catch {
    return 0
  }

  $cleaned = $false
  $nextWatchScanMs = [int64]0
  try {
    Write-RuntimeLog "worker started continuous=$([bool]$Continuous)"
    while ($true) {
      $config = Get-Config
      if (-not $cleaned) {
        Clean-RuntimeState -ReceiptRetentionDays $config.sentRetentionDays -DeadRetentionDays $config.deadRetentionDays
        $cleaned = $true
      }

      $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
      if ($Continuous -and $config.watchRollouts -and $nowMs -ge $nextWatchScanMs) {
        try {
          $observed = Invoke-RolloutWatchScan -Config $config -NowUnixMs $nowMs
          if ($observed -gt 0) { Write-RuntimeLog "rollout watcher observed completions=$observed" }
        } catch {
          Write-RuntimeLog "rollout watcher error: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 300)"
        }
        $nextWatchScanMs = $nowMs + [int64]([Math]::Max(0.1, $config.watchScanSeconds) * 1000)
      }
      Coalesce-ThreadCandidates -Config $config
      $pendingResult = Process-PendingCandidates -Config $config
      $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
      $files = @(Get-ChildItem -LiteralPath $OutboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object CreationTimeUtc, Name)
      $nextDueMs = $pendingResult.nextDueUnixMs

      foreach ($file in $files) {
        $record = $null
        try {
          $record = Read-JsonFile -Path $file.FullName
          Assert-QueuedRecord -Record $record -ExpectedKey $file.BaseName
        } catch {
          $badRecord = [pscustomobject]@{ schema = 1; key = $file.BaseName; last_error = 'invalid queue JSON'; failed_at = [DateTimeOffset]::UtcNow.ToString('o') }
          Move-ToDeadLetter -Path $file.FullName -Record $badRecord
          Write-RuntimeLog "dead-letter invalid event key=$($file.BaseName.Substring(0, [Math]::Min(12, $file.BaseName.Length)))"
          continue
        }

        $conflict = Resolve-QueueReceiptConflict -Path $file.FullName -Record $record
        if (-not $conflict.keep) { continue }
        $record = $conflict.record
        $sentPath = Join-Path $SentDir ($record.key + '.json')

        $dueMs = [int64](Get-ObjectValue $record 'next_attempt_unix_ms' 0)
        if ($dueMs -gt $nowMs) {
          if ($null -eq $nextDueMs -or $dueMs -lt $nextDueMs) {
            $nextDueMs = $dueMs
          }
          continue
        }

        $isNetworkRetry = [int](Get-ObjectValue $record 'attempts' 0) -gt 0
        # Entering the outbox closes the idle/coalescing epoch. A later pending
        # turn must not suppress, re-gate, or move this durable delivery back.
        if (-not $isNetworkRetry -and $config.suppressSubagents) {
          $classification = [string](Get-ObjectValue $record 'session_classification' 'unknown')
          if ($classification -notin @('root', 'subagent')) {
            $sessionHome = [string](Get-ObjectValue $record 'session_codex_home' $CodexHome)
            if ([string]::IsNullOrWhiteSpace($sessionHome)) {
              $sessionHome = $CodexHome
            }
            $classification = Get-EventClassification -Event $record.event -ThreadId ([string]$record.thread_id) -SessionHome $sessionHome
          }
          if ($classification -eq 'subagent') {
            Move-ToSuppressed -Path $file.FullName -Record $record -Reason 'subagent'
            Write-RuntimeLog "suppressed subagent event key=$($record.key.Substring(0, 12)) thread=$(([string]$record.thread_id).Substring(0, [Math]::Min(8, ([string]$record.thread_id).Length)))"
            continue
          }
          if ($classification -eq 'unknown') {
            $createdMs = [int64](Get-ObjectValue $record 'created_unix_ms' $nowMs)
            $classificationDueMs = $createdMs + [int64]([Math]::Max(0, $config.subagentClassificationGraceSeconds) * 1000)
            if ($classificationDueMs -gt $nowMs) {
              if ($null -eq $nextDueMs -or $classificationDueMs -lt $nextDueMs) {
                $nextDueMs = $classificationDueMs
              }
              continue
            }
          }
        }

        try {
          $response = Send-NtfyEvent -Record $record -Config $config
          $receipt = [ordered]@{
            schema = 1
            key = $record.key
            sequence_id = $record.sequence_id
            thread_id = $record.thread_id
            turn_id = $record.turn_id
            origin = $record.origin
            sent_at = [DateTimeOffset]::UtcNow.ToString('o')
            ntfy_id = [string](Get-ObjectValue $response 'id' '')
          }
          Write-JsonAtomic -Path $sentPath -Value $receipt
          Remove-Item -LiteralPath $file.FullName -Force
          Write-RuntimeLog "sent event key=$($record.key.Substring(0, 12)) origin=$($record.origin)"
        } catch {
          $failure = Get-HttpFailureInfo -ErrorRecord $_
          $record.attempts = [int]$record.attempts + 1
          $record.last_error = Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 400
          if ($failure.permanent -or ($config.maxAttempts -gt 0 -and $record.attempts -ge $config.maxAttempts)) {
            Move-ToDeadLetter -Path $file.FullName -Record $record
            Write-RuntimeLog "dead-letter event key=$($record.key.Substring(0, 12)) attempts=$($record.attempts) error=$($record.last_error)"
            continue
          }
          $delay = Get-RetryDelaySeconds -Attempt $record.attempts -BaseSeconds $RetryBaseSeconds -MaxSeconds $config.retryMaxSeconds -RetryAfterSeconds $failure.retryAfterSeconds
          $record.next_attempt_unix_ms = [DateTimeOffset]::UtcNow.AddSeconds($delay).ToUnixTimeMilliseconds()
          Write-JsonAtomic -Path $file.FullName -Value $record
          Write-RuntimeLog "retry event key=$($record.key.Substring(0, 12)) attempt=$($record.attempts) in=$([Math]::Round($delay, 2))s error=$($record.last_error)"
          if ($null -eq $nextDueMs -or $record.next_attempt_unix_ms -lt $nextDueMs) {
            $nextDueMs = $record.next_attempt_unix_ms
          }
        }
      }

      $remaining = @(Get-ChildItem -LiteralPath $OutboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count +
        @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
      if (-not $Continuous -and $remaining -eq 0) {
        return 0
      }
      $sleepMs = [Math]::Max(100, $PollSeconds * 1000)
      if ($null -ne $nextDueMs) {
        $untilDue = [Math]::Max(100, [int64]$nextDueMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $sleepMs = [Math]::Min($sleepMs, $untilDue)
      }
      if ($Continuous -and $config.watchRollouts) {
        $untilWatch = [Math]::Max(100, $nextWatchScanMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $sleepMs = [Math]::Min($sleepMs, $untilWatch)
      }
      Start-Sleep -Milliseconds ([int]$sleepMs)
    }
  } finally {
    if ($null -ne $lockStream) {
      $lockStream.Dispose()
    }
    Write-RuntimeLog 'worker stopped'
  }
}

function Start-DetachedWorker {
  if ($NoSpawn -or $env:CODEX_NTFY_NO_SPAWN -eq '1') {
    return
  }
  try {
    $powerShellExe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShellExe)) {
      $powerShellExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    }
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Worker -PollSeconds {1}' -f $PSCommandPath, $PollSeconds
    Start-Process -FilePath $powerShellExe -ArgumentList $arguments -WindowStyle Hidden | Out-Null
  } catch {
    Write-RuntimeLog "failed to start worker: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 300)"
  }
}

function Get-SafeServerDisplay {
  param([string]$Server)
  $uri = $null
  if (-not [Uri]::TryCreate($Server, [UriKind]::Absolute, [ref]$uri) -or
      $uri.Scheme -notin @('http', 'https') -or [string]::IsNullOrWhiteSpace($uri.Host)) {
    return 'invalid'
  }
  $hostName = $uri.Host
  if ($hostName.Contains(':') -and -not $hostName.StartsWith('[')) { $hostName = "[$hostName]" }
  $port = if ($uri.IsDefaultPort) { '' } else { ':' + $uri.Port }
  return $uri.Scheme + '://' + $hostName + $port
}

function Show-Doctor {
  Ensure-RuntimeDirectories
  $config = Get-Config
  $result = [ordered]@{
    version = $ScriptVersion
    codex_home = $CodexHome
    config_path = $ConfigPath
    config_exists = Test-Path -LiteralPath $ConfigPath
    server = Get-SafeServerDisplay -Server $config.server
    topic_configured = -not [string]::IsNullOrWhiteSpace($config.topic)
    auth_mode = if (-not [string]::IsNullOrWhiteSpace($config.token)) {
      'token'
    } elseif ((-not [string]::IsNullOrWhiteSpace($config.username)) -ne (-not [string]::IsNullOrWhiteSpace($config.password))) {
      'invalid'
    } elseif (-not [string]::IsNullOrWhiteSpace($config.username)) {
      'basic'
    } else {
      'anonymous'
    }
    state_dir = $StateRoot
    idle_detection_mode = $config.idleDetectionMode
    idle_grace_seconds = $config.idleGraceSeconds
    goal_aware = $config.goalAware
    watch_rollouts = $config.watchRollouts
    watch_discovery_seconds = $config.watchDiscoverySeconds
    watch_roots = @($config.watchRoots).Count
    sqlite_home_configured = [bool]$config.workerSqliteConfigured
    pending_idle = @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    pending = @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    watched_rollouts = @(Get-ChildItem -LiteralPath $WatchDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    queued = @(Get-ChildItem -LiteralPath $OutboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    sent_receipts = @(Get-ChildItem -LiteralPath $SentDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    suppressed = @(Get-ChildItem -LiteralPath $SuppressedDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    dead_letters = @(Get-ChildItem -LiteralPath $DeadDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    dead_retention_days = $config.deadRetentionDays
  }
  $result | ConvertTo-Json -Depth 5
}

try {
  Ensure-RuntimeDirectories
  if ($Doctor) {
    Show-Doctor
    exit 0
  }
  if ($Worker -or $Continuous) {
    exit (Invoke-OutboxWorker)
  }

  $raw = if ($Test) {
    ConvertTo-CompactJson ([ordered]@{
      type = 'agent-turn-complete'
      'thread-id' = '00000000-0000-4000-8000-000000000001'
      'turn-id' = [Guid]::NewGuid().ToString()
      cwd = (Get-Location).Path
      'last-assistant-message' = 'Test codex-ntfy-notifier: delivery, queueing, and deduplication work.'
    })
  } else {
    Get-RawNotification
  }
  $event = ConvertTo-NotificationEvent -Raw $raw
  if ($null -eq $event) {
    if ($HookEvent) { Write-Output '{}' }
    exit 0
  }
  $candidateKind = 'legacy'
  if ($HookEvent) {
    $hookName = [string](Get-FirstObjectValue $event @('hook_event_name', 'hook-event-name', 'hookEventName', 'event_name', 'eventName', 'type'))
    if ($hookName -eq 'SubagentStop') {
      Write-RuntimeLog 'ignored SubagentStop hook event'
      Write-Output '{}'
      exit 0
    }
    if ($hookName -ne 'Stop') {
      Write-RuntimeLog "ignored unsupported hook event type=$(Sanitize-NotificationText -Text $hookName -MaxLength 80)"
      Write-Output '{}'
      exit 0
    }
    $hookInput = $event
    $event = [pscustomobject][ordered]@{
      type = 'agent-turn-complete'
      'thread-id' = [string](Get-FirstObjectValue $hookInput @('thread-id', 'thread_id', 'threadId', 'session_id', 'session-id', 'sessionId'))
      'turn-id' = [string](Get-FirstObjectValue $hookInput @('turn-id', 'turn_id', 'turnId', 'task_id', 'task-id', 'taskId'))
      cwd = [string](Get-FirstObjectValue $hookInput @('cwd', 'working-directory', 'working_directory'))
      'last-assistant-message' = [string](Get-FirstObjectValue $hookInput @('last-assistant-message', 'last_assistant_message', 'lastAgentMessage', 'last_agent_message', 'message'))
      transcript_path = [string](Get-FirstObjectValue $hookInput @('transcript_path', 'transcript-path', 'transcriptPath', 'rollout_path', 'rollout-path'))
      session_codex_home = [string](Get-FirstObjectValue $hookInput @('session_codex_home', 'codex_home', 'codexHome'))
      session_sqlite_home = [string](Get-FirstObjectValue $hookInput @('session_sqlite_home', 'sqlite_home', 'sqliteHome'))
      source = Get-ObjectValue $hookInput 'source'
      'is-subagent' = Get-FirstObjectValue $hookInput @('is-subagent', 'is_subagent')
      'parent-thread-id' = [string](Get-FirstObjectValue $hookInput @('parent-thread-id', 'parent_thread_id'))
    }
    $candidateKind = 'hook_stop'
  }
  $eventType = [string](Get-ObjectValue $event 'type' 'agent-turn-complete')
  if ($eventType -ne 'agent-turn-complete') {
    if ($HookEvent) { Write-Output '{}' }
    exit 0
  }

  $threadId = [string](Get-FirstObjectValue $event @('thread-id', 'thread_id'))
  $config = Get-Config
  $payloadSessionHome = [string](Get-FirstObjectValue $event @('session_codex_home', 'codex_home', 'codexHome'))
  $eventSessionHome = if (-not [string]::IsNullOrWhiteSpace($SessionCodexHome)) {
    $SessionCodexHome
  } elseif (-not [string]::IsNullOrWhiteSpace($payloadSessionHome)) {
    $payloadSessionHome
  } else {
    $CodexHome
  }
  $payloadSqliteHome = [string](Get-FirstObjectValue $event @('session_sqlite_home', 'sqlite_home', 'sqliteHome'))
  $eventSqliteHome = if (-not [string]::IsNullOrWhiteSpace($SessionSqliteHome)) {
    $SessionSqliteHome
  } elseif (-not [string]::IsNullOrWhiteSpace($env:CODEX_SQLITE_HOME)) {
    $env:CODEX_SQLITE_HOME
  } elseif (-not [string]::IsNullOrWhiteSpace($payloadSqliteHome)) {
    $payloadSqliteHome
  } else {
    $eventSessionHome
  }
  $detectedClassification = Get-EventClassification -Event $event -ThreadId $threadId -SessionHome $eventSessionHome
  $eventClassification = if ($detectedClassification -eq 'subagent') {
    'subagent'
  } elseif ($SessionClassification -in @('root', 'subagent')) {
    $SessionClassification
  } else {
    $detectedClassification
  }
  $sourceEvent = if ($HookEvent) { 'Stop' } else { 'agent-turn-complete' }
  $record = New-EventRecord -Event $event -EventOrigin (Get-DefaultOrigin) -EventSessionHome $eventSessionHome -EventSqliteHome $eventSqliteHome -EventClassification $eventClassification -EventIncludeMessage $config.includeMessage -CandidateKind $candidateKind -SourceEvent $sourceEvent
  if ($config.suppressSubagents -and $eventClassification -eq 'subagent') {
    Move-ToSuppressed -Path (Join-Path $OutboxDir ($record.key + '.json')) -Record $record
    Write-RuntimeLog "suppressed subagent completion thread=$($threadId.Substring(0, [Math]::Min(8, $threadId.Length)))"
    if ($HookEvent) { Write-Output '{}' }
    exit 0
  }

  $queued = if ($Test) { Add-OutboxEvent -Record $record } else { Add-CandidateEvent -Record $record -Config $config }
  Start-DetachedWorker

  if ($HookEvent) {
    Write-Output '{}'
    exit 0
  }

  if ($Test) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    $sentPath = Join-Path $SentDir ($record.key + '.json')
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
      if (Test-Path -LiteralPath $sentPath) {
        Write-Output 'Test notification delivered.'
        exit 0
      }
      Start-Sleep -Milliseconds 250
    }
    Write-Output "Test notification queued; delivery is still pending. Check $LogPath"
    exit 2
  }
  exit 0
} catch {
  Write-RuntimeLog "hook error: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 500)"
  if ($HookEvent) {
    if ($BridgeFallback) { exit 1 }
    Write-Output '{}'
    exit 0
  }
  exit 1
}
