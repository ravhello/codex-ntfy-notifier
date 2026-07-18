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
  [switch]$ClaudeHook,
  [switch]$BridgeFallback,
  [switch]$Worker,
  [switch]$Continuous,
  [switch]$DeliveryOnly,
  [switch]$ScanRollouts,
  [ValidateSet('All', 'Local', 'Remote')]
  [string]$ScanScope = 'All',
  [switch]$Maintenance,
  [switch]$CleanupTestState,
  [int]$ScanParentPid = 0,
  [string]$ScanParentToken = '',
  [switch]$NoSpawn,
  [switch]$Doctor,
  [switch]$Test,
  [int]$PollSeconds = 2,
  [double]$RetryBaseSeconds = 5
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ScriptVersion = '2.5.1'
$MaxNtfyMessageBytes = 3500
$SyntheticTestThreadId = '00000000-0000-4000-8000-000000000001'
$ChatGptTaskUrlPrefix = 'https://chatgpt.com/codex/tasks/'
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
$MutationLocksDir = Join-Path $StateRoot 'mutation-locks'
$ClaudeSessionsDir = Join-Path $StateRoot 'claude-sessions'
$WorkerLockPath = Join-Path $StateRoot 'worker.lock'
$DeliveryLockPath = Join-Path $StateRoot 'delivery.lock'
$WorkerHealthPath = Join-Path $StateRoot 'worker-health.json'
$DeliveryHealthPath = Join-Path $StateRoot 'delivery-health.json'
$ScanLockPath = Join-Path $StateRoot 'watch-scan.lock'
$ScanHealthPath = Join-Path $StateRoot 'watch-health.json'
$RemoteScanLockPath = Join-Path $StateRoot 'remote-watch-scan.lock'
$RemoteScanHealthPath = Join-Path $StateRoot 'remote-watch-health.json'
$MaintenanceLockPath = Join-Path $StateRoot 'maintenance.lock'
$LogPath = Join-Path $StateRoot 'notify.log'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object System.Text.UTF8Encoding($false, $true)
$script:ClaudeGoalStateCache = @{}
$ClaudePromptBaselineMaxBytes = [int64](1024 * 1024)
$ClaudeGoalMaxLineBytes = 1024 * 1024

function Ensure-RuntimeDirectories {
  foreach ($path in @($StateRoot, $PendingDir, $OutboxDir, $WatchDir, $SentDir, $SuppressedDir, $DeadDir, $MutationLocksDir, $ClaudeSessionsDir)) {
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

function Get-StrongEventKey {
  param(
    [ValidateSet('codex', 'claude')]
    [string]$Provider,
    [string]$ThreadId,
    [string]$TurnId
  )

  $identity = if ($Provider -eq 'codex') {
    "codex-ntfy/v1|$ThreadId|$TurnId"
  } else {
    "codex-ntfy/v1|$Provider|$ThreadId|$TurnId"
  }
  return Get-Sha256Hex $identity
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
    includeTaskLink = [bool](Get-ObjectValue $fileConfig 'include_task_link' $false)
    includeTaskLinkAction = [bool](Get-ObjectValue $fileConfig 'include_task_link_action' $false)
    markdown = [bool](Get-ObjectValue $fileConfig 'markdown' $false)
    includeFullPath = [bool](Get-ObjectValue $fileConfig 'include_full_path' $false)
    suppressSubagents = [bool](Get-ObjectValue $fileConfig 'suppress_subagents' $true)
    subagentClassificationGraceSeconds = [double](Get-ObjectValue $fileConfig 'subagent_classification_grace_seconds' 8)
    idleDetectionMode = $idleDetectionMode
    idleGraceSeconds = [double](Get-ObjectValue $fileConfig 'idle_grace_seconds' 1.5)
    idleProbeGraceSeconds = [double](Get-ObjectValue $fileConfig 'idle_probe_grace_seconds' 30)
    unknownRetryMaxSeconds = [double](Get-ObjectValue $fileConfig 'unknown_retry_max_seconds' 60)
    goalAware = [bool](Get-ObjectValue $fileConfig 'goal_aware' $true)
    goalPollSeconds = [double](Get-ObjectValue $fileConfig 'goal_poll_seconds' 1)
    suppressTechnicalTurns = [bool](Get-ObjectValue $fileConfig 'suppress_technical_turns' $true)
    watchRollouts = [bool](Get-ObjectValue $fileConfig 'watch_rollouts' $true)
    watchScanSeconds = [double](Get-ObjectValue $fileConfig 'watch_scan_seconds' 2)
    watchInitialReplaySeconds = [double](Get-ObjectValue $fileConfig 'watch_initial_replay_seconds' 15)
    watchDiscoverySeconds = [double](Get-ObjectValue $fileConfig 'watch_discovery_seconds' 60)
    watchCursorBatchSize = [int][Math]::Min(4096, [Math]::Max(1, [int](Get-ObjectValue $fileConfig 'watch_cursor_batch_size' 64)))
    remoteWatchTimeoutSeconds = [double](Get-ObjectValue $fileConfig 'watch_remote_timeout_seconds' 90)
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

function Convert-MarkdownToPlainText {
  param([AllowEmptyString()][string]$Text)

  if ([string]::IsNullOrEmpty($Text)) { return '' }
  $value = $Text -replace "`r`n?", "`n"

  # Protect literal code and escaped Markdown punctuation while formatting the
  # surrounding prose. Without this, identifiers such as __init__,
  # last_assistant_message, and a*b*c would be mistaken for emphasis.
  $literals = New-Object 'System.Collections.Generic.List[string]'
  $protectLiteral = {
    param([AllowEmptyString()][string]$Literal)
    $index = $literals.Count
    [void]$literals.Add($Literal)
    return "$([char]0xE000)$index$([char]0xE001)"
  }
  $preparedLines = New-Object 'System.Collections.Generic.List[string]'
  $insideFence = $false
  foreach ($rawLine in $value.Split([char]10)) {
    $line = [string]$rawLine
    if ($line -match '^\s{0,3}(?:`{3,}|~{3,})') {
      $insideFence = -not $insideFence
      continue
    }
    if ($insideFence -and -not [string]::IsNullOrWhiteSpace($line)) {
      $line = & $protectLiteral $line
    }
    $preparedLines.Add($line)
  }
  $value = $preparedLines -join "`n"
  $value = [regex]::Replace($value, '`([^`\r\n]+)`', [System.Text.RegularExpressions.MatchEvaluator]{
      param($match)
      return (& $protectLiteral ([string]$match.Groups[1].Value))
    })
  $value = [regex]::Replace($value, '\\([^\w\s])', [System.Text.RegularExpressions.MatchEvaluator]{
      param($match)
      return (& $protectLiteral ([string]$match.Groups[1].Value))
    })

  $value = [regex]::Replace($value, '!\[([^\]]*)\]\([^\r\n)]*\)', '$1')
  $value = [regex]::Replace($value, '\[([^\]]+)\]\([^\r\n)]*\)', '$1')
  $value = [regex]::Replace($value, '!\[([^\]]*)\]\[[^\]\r\n]*\]', '$1')
  $value = [regex]::Replace($value, '(?<!!)\[([^\]]+)\]\[[^\]\r\n]*\]', '$1')
  $value = [regex]::Replace($value, '<(https?://[^>\s]+)>', '$1', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)

  $plainLines = New-Object 'System.Collections.Generic.List[string]'
  foreach ($rawLine in $value.Split([char]10)) {
    $line = [string]$rawLine
    if ($line -match '^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$' -or
        $line -match '^\s{0,3}(?:[-*_]\s*){3,}$' -or
        $line -match '^\s{0,3}\[[^\]]+\]:\s+\S+') {
      continue
    }
    $line = [regex]::Replace($line, '^\s{0,3}#{1,6}\s+', '')
    $line = [regex]::Replace($line, '^(?:(?:\s{0,3}>\s?)|(?:\s*[-*+]\s+)|(?:\s*\d+[.)]\s+))+', '')
    $looksLikeTable = $line.Trim().StartsWith('|') -or $line.Trim().EndsWith('|') -or $line.Contains(' | ')
    if ($looksLikeTable -and $line.Contains('|')) {
      $cells = @($line.Trim().Trim('|').Split('|') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
      $line = $cells -join (" $MiddleDot ")
    }
    $line = $line.Trim()
    if (-not [string]::IsNullOrWhiteSpace($line)) {
      $plainLines.Add($line)
    }
  }

  $value = $plainLines -join " $MiddleDot "
  for ($pass = 0; $pass -lt 2; $pass++) {
    $value = [regex]::Replace($value, '\*\*(.+?)\*\*', '$1')
    $value = [regex]::Replace($value, '__(.+?)__', '$1')
    $value = [regex]::Replace($value, '~~(.+?)~~', '$1')
    $value = [regex]::Replace($value, '(?<![\w*])\*([^*\r\n]+)\*(?![\w*])', '$1')
    $value = [regex]::Replace($value, '(?<![\w_])_([^_\r\n]+)_(?![\w_])', '$1')
  }
  for ($index = 0; $index -lt $literals.Count; $index++) {
    $token = "$([char]0xE000)$index$([char]0xE001)"
    $value = $value.Replace($token, $literals[$index])
  }
  return $value.Trim()
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
  function Read-Utf8StandardInput {
    $stream = $null
    $reader = $null
    try {
      $stream = [Console]::OpenStandardInput()
      # Never let BOM detection replace the strict decoder with UTF-16 or with
      # a replacement-fallback UTF-8 instance. Decode one contract only, then
      # remove an optional UTF-8 BOM after it has been validated as UTF-8.
      $reader = New-Object System.IO.StreamReader($stream, $Utf8StrictNoBom, $false, 4096, $false)
      $value = $reader.ReadToEnd()
      if ($value.Length -gt 0 -and $value[0] -eq [char]0xFEFF) {
        return $value.Substring(1)
      }
      return $value
    } finally {
      if ($null -ne $reader) {
        $reader.Dispose()
      } elseif ($null -ne $stream) {
        $stream.Dispose()
      }
    }
  }

  if ($HookEvent -or $ClaudeHook -or $ReadStdin) {
    return Read-Utf8StandardInput
  }
  if ($NotificationArgs.Count -gt 0) {
    return ($NotificationArgs -join ' ')
  }
  if ([Console]::IsInputRedirected) {
    return Read-Utf8StandardInput
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
    [string]$SourceEvent = 'agent-turn-complete',
    [ValidateSet('codex', 'claude')]
    [string]$Provider = 'codex'
  )

  $threadId = [string](Get-FirstObjectValue $Event @('thread-id', 'thread_id'))
  $turnId = [string](Get-FirstObjectValue $Event @('turn-id', 'turn_id'))
  $weakIdentity = [string]::IsNullOrWhiteSpace($threadId) -or [string]::IsNullOrWhiteSpace($turnId)
  $identity = if ($weakIdentity) {
    if ($Provider -eq 'codex') {
      'codex-ntfy/v1|weak|' + [Guid]::NewGuid().ToString('N')
    } else {
      "codex-ntfy/v1|$Provider|weak|" + [Guid]::NewGuid().ToString('N')
    }
  } else {
    if ($Provider -eq 'codex') { "codex-ntfy/v1|$threadId|$turnId" } else { "codex-ntfy/v1|$Provider|$threadId|$turnId" }
  }
  $key = if ($weakIdentity) {
    Get-Sha256Hex $identity
  } else {
    Get-StrongEventKey -Provider $Provider -ThreadId $threadId -TurnId $turnId
  }
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
    sequence_id = $Provider + '-' + $key.Substring(0, 32)
    provider = $Provider
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
    goal_status = [string](Get-FirstObjectValue $Event @('goal-status', 'goal_status'))
    candidate_revision = [Guid]::NewGuid().ToString('N')
    claude_session_epoch = [int64](Get-FirstObjectValue $Event @('claude-session-epoch', 'claude_session_epoch'))
    claude_goal_state = [string](Get-FirstObjectValue $Event @('claude-goal-state', 'claude_goal_state'))
    claude_goal_marker = [string](Get-FirstObjectValue $Event @('claude-goal-marker', 'claude_goal_marker'))
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
  if ([string](Get-ObjectValue $Record 'provider' 'codex') -eq 'claude') { return $true }
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
  # A bounded shard set avoids leaving one permanent file per observed turn.
  # Same-key operations still serialize, while unrelated keys only contend on
  # the rare two-hex-digit collision.
  $lockPath = Join-Path $MutationLocksDir ($Key.Substring(0, 2) + '.lock')
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

  # A /goal can emit several Stop events with the same prompt id. Keep the
  # original active marker as the gate anchor while refreshing the candidate
  # with the newest assistant message and a new revision. The transcript then
  # proves the transition to achieved/failed/cleared without ever delivering
  # an intermediate result.
  if ([string](Get-ObjectValue $existing 'provider' '') -eq 'claude' -and
      [string](Get-ObjectValue $IncomingRecord 'provider' '') -eq 'claude' -and
      [string](Get-ObjectValue $existing 'candidate_kind' '') -eq 'claude_stop' -and
      [string](Get-ObjectValue $IncomingRecord 'candidate_kind' '') -eq 'claude_stop' -and
      [string](Get-ObjectValue $existing 'claude_goal_state' '') -eq 'active') {
    Set-RecordValue -Record $IncomingRecord -Name 'claude_goal_state' -Value 'active'
    Set-RecordValue -Record $IncomingRecord -Name 'claude_goal_marker' -Value ([string](Get-ObjectValue $existing 'claude_goal_marker' ''))
    Set-RecordValue -Record $IncomingRecord -Name 'goal_status' -Value ([string](Get-ObjectValue $existing 'goal_status' ''))
  }

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
    if ([string](Get-ObjectValue $receipt 'reason' '') -notin @('technical-turn', 'unverifiable')) { return $false }
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $Path) { return $false }
    Write-RuntimeLog "revived provisional suppression with Stop evidence key=$($Record.key.Substring(0, 12))"
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

  $provider = [string](Get-ObjectValue $Record 'provider' 'codex')
  $kind = [string](Get-ObjectValue $Record 'candidate_kind' '')
  if ($Config.idleDetectionMode -eq 'off' -and
      -not ($provider -eq 'claude' -and $kind -ne 'claude_stop_failure')) {
    return Add-OutboxEvent -Record $Record
  }
  return Add-PendingEvent -Record $Record
}

function Remove-ClaudePendingCandidate {
  param(
    [string]$SessionId,
    [string]$PromptId
  )

  if ([string]::IsNullOrWhiteSpace($SessionId) -or [string]::IsNullOrWhiteSpace($PromptId)) { return }
  $key = Get-StrongEventKey -Provider 'claude' -ThreadId $SessionId -TurnId $PromptId
  [void](Invoke-WithRecordMutationLock -Key $key -Action {
      param($lockedKey)
      $path = Join-Path $PendingDir ($lockedKey + '.json')
      if (-not (Test-Path -LiteralPath $path)) { return }
      try {
        $record = Read-JsonFile -Path $path
        if ([string](Get-ObjectValue $record 'provider' '') -eq 'claude') {
          Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
          Write-RuntimeLog "cancelled Claude candidate with active work key=$($lockedKey.Substring(0, 12))"
        }
      } catch {
        Write-RuntimeLog "could not cancel Claude candidate key=$($lockedKey.Substring(0, 12))"
      }
    } -Arguments @($key))
}

function Get-ClaudeSessionStateInfo {
  param([string]$SessionId)

  if ([string]::IsNullOrWhiteSpace($SessionId)) { return $null }
  $key = Get-Sha256Hex ("codex-ntfy/v1|claude-session|$SessionId")
  return [pscustomobject]@{
    key = $key
    path = Join-Path $ClaudeSessionsDir ($key + '.json')
    lock_path = Join-Path $ClaudeSessionsDir ($key + '.lock')
  }
}

function Invoke-WithClaudeSessionLock {
  param(
    [object]$Info,
    [scriptblock]$Action,
    [object[]]$Arguments = @()
  )

  Ensure-RuntimeDirectories
  if ($null -eq $Info -or [string]::IsNullOrWhiteSpace([string]$Info.lock_path)) {
    throw 'invalid Claude session lock information'
  }
  $lockStream = $null
  for ($attempt = 0; $attempt -lt 200 -and $null -eq $lockStream; $attempt++) {
    try {
      $lockStream = [IO.File]::Open([string]$Info.lock_path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
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

function Read-ClaudeSessionState {
  param([string]$SessionId)

  $info = Get-ClaudeSessionStateInfo -SessionId $SessionId
  if ($null -eq $info -or -not (Test-Path -LiteralPath $info.path)) { return $null }
  try {
    $state = Read-JsonFile -Path $info.path
    if ([string](Get-ObjectValue $state 'session_id' '') -ne $SessionId) { return $null }
    return $state
  } catch {
    return $null
  }
}

function Set-ClaudeSessionBusy {
  param(
    [string]$SessionId,
    [string]$PromptId,
    [string]$TranscriptPath
  )

  $info = Get-ClaudeSessionStateInfo -SessionId $SessionId
  if ($null -eq $info) { return [int64]0 }
  # Take the session lock before reading the baseline. This is the linearization
  # point between a new prompt and a worker promoting the previous prompt.
  $epoch = Invoke-WithClaudeSessionLock -Info $info -Action {
    param($lockedPath, $lockedSessionId, $lockedPromptId, $lockedTranscriptPath, $baselineMaxBytes)
    $baseline = Get-ClaudeGoalTranscriptState -TranscriptPath $lockedTranscriptPath -MaxBytes ([int64]$baselineMaxBytes)
    $baselineCaptured = -not [string]::IsNullOrWhiteSpace($lockedTranscriptPath) -and
      (Test-Path -LiteralPath $lockedTranscriptPath -PathType Leaf) -and
      [string]$baseline.state -notin @('unknown', 'unverifiable')
    $previous = $null
    try { $previous = Read-JsonFile -Path $lockedPath } catch { $previous = $null }
    $nextEpoch = [int64](Get-ObjectValue $previous 'epoch' 0) + 1
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    Write-JsonAtomic -Path $lockedPath -Value ([ordered]@{
        schema = 1
        session_id = $lockedSessionId
        epoch = $nextEpoch
        state = 'busy'
        prompt_id = $lockedPromptId
        transcript_path = $lockedTranscriptPath
        busy_unix_ms = $now
        idle_unix_ms = 0
        notification_type = ''
        goal_baseline_state = [string]$baseline.state
        goal_baseline_marker = [string]$baseline.marker
        goal_baseline_captured = [bool]$baselineCaptured
      })
    return $nextEpoch
  } -Arguments @($info.path, $SessionId, $PromptId, $TranscriptPath, $ClaudePromptBaselineMaxBytes)

  # A follow-up prompt means the session is no longer idle. Remove every older
  # unconfirmed Claude candidate for this session; no terminal receipt is left,
  # so the same prompt can be armed again by a later Stop.
  $files = @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue)
  foreach ($file in $files) {
    $candidate = $null
    try { $candidate = Read-JsonFile -Path $file.FullName } catch { continue }
    if ([string](Get-ObjectValue $candidate 'provider' '') -ne 'claude' -or
        [string](Get-ObjectValue $candidate 'thread_id' '') -ne $SessionId) { continue }
    [void](Invoke-WithRecordMutationLock -Key ([string]$candidate.key) -Action {
        param($lockedPath, $lockedSessionId)
        if (-not (Test-Path -LiteralPath $lockedPath)) { return }
        try {
          $current = Read-JsonFile -Path $lockedPath
          if ([string](Get-ObjectValue $current 'provider' '') -eq 'claude' -and
              [string](Get-ObjectValue $current 'thread_id' '') -eq $lockedSessionId) {
            Remove-Item -LiteralPath $lockedPath -Force -ErrorAction SilentlyContinue
          }
        } catch { }
      } -Arguments @($file.FullName, $SessionId))
  }
  Write-RuntimeLog "Claude session marked busy; cancelled stale candidates session=$($SessionId.Substring(0, [Math]::Min(8, $SessionId.Length)))"
  return [int64]$epoch
}

function Set-ClaudeSessionIdle {
  param(
    [string]$SessionId,
    [string]$PromptId,
    [string]$TranscriptPath,
    [string]$NotificationType
  )

  $info = Get-ClaudeSessionStateInfo -SessionId $SessionId
  if ($null -eq $info) { return $false }
  $updated = Invoke-WithClaudeSessionLock -Info $info -Action {
      param($lockedPath, $lockedSessionId, $lockedPromptId, $lockedTranscriptPath, $lockedNotificationType)
      $previous = $null
      try { $previous = Read-JsonFile -Path $lockedPath } catch { $previous = $null }
      $previousPrompt = [string](Get-ObjectValue $previous 'prompt_id' '')
      if (-not [string]::IsNullOrWhiteSpace($previousPrompt) -and
          -not [string]::IsNullOrWhiteSpace($lockedPromptId) -and
          $previousPrompt -ne $lockedPromptId) {
        return $false
      }
      $epoch = [int64](Get-ObjectValue $previous 'epoch' 0)
      if ($epoch -le 0) { $epoch = 1 }
      $busyAt = [int64](Get-ObjectValue $previous 'busy_unix_ms' 0)
      $baselineState = [string](Get-ObjectValue $previous 'goal_baseline_state' 'none')
      $baselineMarker = [string](Get-ObjectValue $previous 'goal_baseline_marker' '')
      $baselineCaptured = [bool](Get-ObjectValue $previous 'goal_baseline_captured' $false)
      Write-JsonAtomic -Path $lockedPath -Value ([ordered]@{
          schema = 1
          session_id = $lockedSessionId
          epoch = $epoch
          state = 'idle'
          prompt_id = $lockedPromptId
          transcript_path = $lockedTranscriptPath
          busy_unix_ms = $busyAt
          idle_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
          notification_type = $lockedNotificationType
          goal_baseline_state = $baselineState
          goal_baseline_marker = $baselineMarker
          goal_baseline_captured = $baselineCaptured
        })
      return $true
    } -Arguments @($info.path, $SessionId, $PromptId, $TranscriptPath, $NotificationType)
  if ([bool]$updated) {
    Write-RuntimeLog "Claude session confirmed idle type=$(Sanitize-NotificationText -Text $NotificationType -MaxLength 40) session=$($SessionId.Substring(0, [Math]::Min(8, $SessionId.Length)))"
  } else {
    Write-RuntimeLog "ignored stale Claude idle notification session=$($SessionId.Substring(0, [Math]::Min(8, $SessionId.Length)))"
  }
  return [bool]$updated
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

function Get-Utf8HeadLinesFast {
  param(
    [string]$Path,
    [int]$MaxLines
  )

  if ($MaxLines -le 0) { return @() }
  $stream = $null
  $reader = $null
  try {
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    $reader = New-Object System.IO.StreamReader($stream, $Utf8NoBom, $true, 65536, $false)
    $lines = New-Object 'System.Collections.Generic.List[string]'
    while ($lines.Count -lt $MaxLines -and -not $reader.EndOfStream) {
      $lines.Add([string]$reader.ReadLine())
    }
    return @($lines.ToArray())
  } finally {
    if ($null -ne $reader) { $reader.Dispose() } elseif ($null -ne $stream) { $stream.Dispose() }
  }
}

function ConvertFrom-ReverseUtf8Chunk {
  param(
    [byte[]]$Buffer,
    [int]$Count,
    [byte[]]$RightPrefix
  )

  if ($Count -lt 0 -or $Count -gt $Buffer.Length) {
    throw 'invalid reverse UTF-8 chunk length'
  }
  if ($null -eq $RightPrefix) { $RightPrefix = [byte[]]@() }
  if ($RightPrefix.Length -gt 3) {
    throw 'invalid UTF-8 continuation prefix'
  }

  $leadingContinuationCount = 0
  while ($leadingContinuationCount -lt $Count -and
      (($Buffer[$leadingContinuationCount] -band 0xC0) -eq 0x80)) {
    $leadingContinuationCount++
  }
  if ($leadingContinuationCount -gt 3) {
    throw 'invalid UTF-8 continuation prefix'
  }

  $bodyLength = $Count - $leadingContinuationCount
  $decodeBytes = New-Object byte[] ($bodyLength + $RightPrefix.Length)
  if ($bodyLength -gt 0) {
    [Array]::Copy($Buffer, $leadingContinuationCount, $decodeBytes, 0, $bodyLength)
  }
  if ($RightPrefix.Length -gt 0) {
    [Array]::Copy($RightPrefix, 0, $decodeBytes, $bodyLength, $RightPrefix.Length)
  }

  $nextPrefix = New-Object byte[] $leadingContinuationCount
  if ($leadingContinuationCount -gt 0) {
    [Array]::Copy($Buffer, 0, $nextPrefix, 0, $leadingContinuationCount)
  }
  return [pscustomobject]@{
    text = $Utf8StrictNoBom.GetString($decodeBytes)
    prefix = $nextPrefix
  }
}

function Get-Utf8TailLinesFast {
  param(
    [string]$Path,
    [int]$MaxLines
  )

  if ($MaxLines -le 0) { return @() }
  $stream = $null
  try {
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    $reversed = New-Object 'System.Collections.Generic.List[string]'
    $buffer = New-Object byte[] 65536
    $position = [int64]$stream.Length
    $carry = ''
    $rightPrefix = [byte[]]@()
    while ($position -gt 0 -and $reversed.Count -lt $MaxLines) {
      $count = [int][Math]::Min($buffer.Length, $position)
      $position -= $count
      [void]$stream.Seek($position, [IO.SeekOrigin]::Begin)
      $read = $stream.Read($buffer, 0, $count)
      if ($read -le 0) { break }
      $decoded = ConvertFrom-ReverseUtf8Chunk -Buffer $buffer -Count $read -RightPrefix $rightPrefix
      $rightPrefix = [byte[]]$decoded.prefix
      $combined = [string]$decoded.text + $carry
      $parts = $combined.Split([char]10)
      $carry = [string]$parts[0]
      for ($index = $parts.Length - 1; $index -ge 1 -and $reversed.Count -lt $MaxLines; $index--) {
        $reversed.Add(([string]$parts[$index]).TrimEnd([char]13))
      }
    }
    if ($position -eq 0 -and $rightPrefix.Length -gt 0) {
      throw 'transcript begins with invalid UTF-8 continuation bytes'
    }
    if ($position -eq 0 -and $reversed.Count -lt $MaxLines -and -not [string]::IsNullOrEmpty($carry)) {
      $reversed.Add($carry.TrimEnd([char]13))
    }
    $result = $reversed.ToArray()
    [Array]::Reverse($result)
    return @($result)
  } finally {
    if ($null -ne $stream) { $stream.Dispose() }
  }
}

function Get-ClaudeThreadTitle {
  param(
    [string]$TranscriptPath,
    [string]$SessionId
  )

  if ([string]::IsNullOrWhiteSpace($TranscriptPath) -or [string]::IsNullOrWhiteSpace($SessionId)) { return '' }
  try {
    $resolved = [IO.Path]::GetFullPath($TranscriptPath)
    if ([IO.Path]::GetExtension($resolved) -ne '.jsonl' -or
        -not [string]::Equals([IO.Path]::GetFileNameWithoutExtension($resolved), $SessionId, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
      return ''
    }
    # Claude writes generated/manual titles as compact metadata records. Read a
    # bounded prefix plus tail so long sessions do not delay notifications.
    $lines = @()
    $lines += @(Get-Utf8HeadLinesFast -Path $resolved -MaxLines 500)
    $lines += @(Get-Utf8TailLinesFast -Path $resolved -MaxLines 2000)
    $aiTitle = ''
    $customTitle = ''
    foreach ($line in $lines) {
      if ($line -notmatch '"type"\s*:\s*"(?:ai-title|custom-title)"') { continue }
      try {
        $metadata = $line | ConvertFrom-Json -ErrorAction Stop
        $metadataSession = [string](Get-FirstObjectValue $metadata @('sessionId', 'session_id'))
        if (-not [string]::IsNullOrWhiteSpace($metadataSession) -and
            -not [string]::Equals($metadataSession, $SessionId, [StringComparison]::OrdinalIgnoreCase)) {
          continue
        }
        if ([string](Get-ObjectValue $metadata 'type' '') -eq 'custom-title') {
          $customTitle = [string](Get-FirstObjectValue $metadata @('customTitle', 'custom_title'))
        } elseif ([string](Get-ObjectValue $metadata 'type' '') -eq 'ai-title') {
          $aiTitle = [string](Get-FirstObjectValue $metadata @('aiTitle', 'ai_title'))
        }
      } catch {
        continue
      }
    }
    if (-not [string]::IsNullOrWhiteSpace($customTitle)) { return $customTitle }
    return $aiTitle
  } catch {
    return ''
  }
}

function ConvertFrom-ClaudeGoalStatusLine {
  param([string]$Line)

  if ([string]::IsNullOrWhiteSpace($Line) -or $Line -notmatch '"goal_status"') { return $null }
  $marker = Get-Sha256Hex $Line
  try {
    $entry = $Line | ConvertFrom-Json -ErrorAction Stop
  } catch {
    return [pscustomobject]@{ state = 'unknown'; marker = $marker; marker_unix_ms = [int64]0 }
  }
  if ([string](Get-ObjectValue $entry 'type' '') -ne 'attachment') { return $null }
  $attachment = Get-ObjectValue $entry 'attachment'
  if ($null -eq $attachment -or [string](Get-ObjectValue $attachment 'type' '') -ne 'goal_status') { return $null }

  $uuid = ([string](Get-ObjectValue $entry 'uuid' '')).Trim()
  if (-not [string]::IsNullOrWhiteSpace($uuid)) { $marker = $uuid }
  $markerUnixMs = [int64]0
  $timestamp = [string](Get-FirstObjectValue $entry @('timestamp', 'created_at', 'createdAt'))
  if (-not [string]::IsNullOrWhiteSpace($timestamp)) {
    $parsedTimestamp = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse($timestamp, [ref]$parsedTimestamp)) {
      $markerUnixMs = $parsedTimestamp.ToUniversalTime().ToUnixTimeMilliseconds()
    }
  }

  $metProperty = $attachment.PSObject.Properties['met']
  if ($null -eq $metProperty -or $metProperty.Value -isnot [bool]) {
    return [pscustomobject]@{ state = 'unknown'; marker = $marker; marker_unix_ms = $markerUnixMs }
  }
  $failed = $false
  $failedProperty = $attachment.PSObject.Properties['failed']
  if ($null -ne $failedProperty) {
    if ($failedProperty.Value -isnot [bool]) {
      return [pscustomobject]@{ state = 'unknown'; marker = $marker; marker_unix_ms = $markerUnixMs }
    }
    $failed = [bool]$failedProperty.Value
  }
  $sentinel = $false
  $sentinelProperty = $attachment.PSObject.Properties['sentinel']
  if ($null -ne $sentinelProperty) {
    if ($sentinelProperty.Value -isnot [bool]) {
      return [pscustomobject]@{ state = 'unknown'; marker = $marker; marker_unix_ms = $markerUnixMs }
    }
    $sentinel = [bool]$sentinelProperty.Value
  }

  $state = if ($failed) {
    'failed'
  } elseif ([bool]$metProperty.Value -and $sentinel) {
    'cleared'
  } elseif ([bool]$metProperty.Value) {
    'achieved'
  } else {
    'active'
  }
  return [pscustomobject]@{ state = $state; marker = $marker; marker_unix_ms = $markerUnixMs }
}

function Get-ClaudeGoalTranscriptState {
  param(
    [string]$TranscriptPath,
    [int64]$MaxBytes = 0,
    [int]$MaxLineBytes = $ClaudeGoalMaxLineBytes
  )

  $empty = [pscustomobject]@{ state = 'none'; marker = ''; marker_unix_ms = [int64]0 }
  $unknown = [pscustomobject]@{ state = 'unknown'; marker = ''; marker_unix_ms = [int64]0 }
  $hardUnknown = [pscustomobject]@{ state = 'unverifiable'; marker = 'oversize-goal-record'; marker_unix_ms = [int64]0 }
  if ([string]::IsNullOrWhiteSpace($TranscriptPath) -or
      -not (Test-Path -LiteralPath $TranscriptPath -PathType Leaf)) { return $empty }
  if ($MaxBytes -lt 0) { $MaxBytes = 0 }
  if ($MaxLineBytes -le 0) { $MaxLineBytes = $ClaudeGoalMaxLineBytes }
  $resolved = [IO.Path]::GetFullPath($TranscriptPath)
  for ($scanAttempt = 0; $scanAttempt -lt 2; $scanAttempt++) {
    try {
      $before = Get-Item -LiteralPath $resolved -ErrorAction Stop
      $cacheKey = $resolved.ToLowerInvariant() + '|' + $MaxBytes + '|' + $MaxLineBytes
      $cached = if ($script:ClaudeGoalStateCache.ContainsKey($cacheKey)) { $script:ClaudeGoalStateCache[$cacheKey] } else { $null }
      if ($null -ne $cached -and [int64]$cached.length -eq [int64]$before.Length -and
          [int64]$cached.last_write_ticks -eq [int64]$before.LastWriteTimeUtc.Ticks) {
        return $cached.result
      }

      $stream = $null
      $result = $empty
      try {
        $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        $stream = [IO.File]::Open($resolved, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
        $buffer = New-Object byte[] 65536
        $position = [int64]$stream.Length
        $scanFloor = if ($MaxBytes -gt 0) { [Math]::Max([int64]0, $position - $MaxBytes) } else { [int64]0 }
        $limited = $scanFloor -gt 0
        $goalToken = '"goal_status"'
        $goalTokenOverlapLength = $goalToken.Length - 1
        $carry = ''
        $rightPrefix = [byte[]]@()
        $lineOversize = $false
        $oversizeBoundary = ''
        $found = $false
        while ($position -gt $scanFloor -and -not $found) {
          $count = [int][Math]::Min($buffer.Length, $position - $scanFloor)
          $position -= $count
          [void]$stream.Seek($position, [IO.SeekOrigin]::Begin)
          $read = $stream.Read($buffer, 0, $count)
          if ($read -le 0) { break }
          $decoded = ConvertFrom-ReverseUtf8Chunk -Buffer $buffer -Count $read -RightPrefix $rightPrefix
          $rightPrefix = [byte[]]$decoded.prefix
          $chunkText = [string]$decoded.text
          $parts = $chunkText.Split([char]10)
          if ($parts.Length -eq 1) {
            $piece = [string]$parts[0]
            if ($lineOversize) {
              $probe = $piece + $oversizeBoundary
              if ($probe.IndexOf($goalToken, [StringComparison]::Ordinal) -ge 0) {
                $result = $hardUnknown
                $found = $true
              } else {
                $oversizeBoundary = $probe.Substring(0, [Math]::Min($goalTokenOverlapLength, $probe.Length))
              }
            } else {
              $candidate = $piece + $carry
              if ($Utf8NoBom.GetByteCount($candidate) -gt $MaxLineBytes) {
                if ($candidate.IndexOf($goalToken, [StringComparison]::Ordinal) -ge 0) {
                  $result = $hardUnknown
                  $found = $true
                } else {
                  $lineOversize = $true
                  $oversizeBoundary = $candidate.Substring(0, [Math]::Min($goalTokenOverlapLength, $candidate.Length))
                  $carry = ''
                }
              } else {
                $carry = $candidate
              }
            }
            continue
          }

          # Complete the line that began in later chunks. Oversize records are
          # skipped only after every byte has been checked for the lifecycle
          # token, including a token split across chunk boundaries.
          $trailingPiece = [string]$parts[$parts.Length - 1]
          if ($lineOversize) {
            $probe = $trailingPiece + $oversizeBoundary
            if ($probe.IndexOf($goalToken, [StringComparison]::Ordinal) -ge 0) {
              $result = $hardUnknown
              $found = $true
            }
          } else {
            $line = ($trailingPiece + $carry).TrimEnd([char]13)
            if ($line.IndexOf($goalToken, [StringComparison]::Ordinal) -ge 0) {
              $parsed = if ($Utf8NoBom.GetByteCount($line) -gt $MaxLineBytes) {
                $hardUnknown
              } else {
                ConvertFrom-ClaudeGoalStatusLine -Line $line
              }
              if ($null -ne $parsed) {
                $result = $parsed
                $found = $true
              }
            }
          }
          $carry = ''
          $lineOversize = $false
          $oversizeBoundary = ''
          if ($found) { break }

          # Every middle element is a complete line wholly contained in this
          # chunk. Visit newest to oldest so the first parsed marker wins.
          for ($partIndex = $parts.Length - 2; $partIndex -ge 1 -and -not $found; $partIndex--) {
            $line = ([string]$parts[$partIndex]).TrimEnd([char]13)
            if ($line.IndexOf($goalToken, [StringComparison]::Ordinal) -lt 0) { continue }
            $parsed = if ($Utf8NoBom.GetByteCount($line) -gt $MaxLineBytes) {
              $hardUnknown
            } else {
              ConvertFrom-ClaudeGoalStatusLine -Line $line
            }
            if ($null -ne $parsed) {
              $result = $parsed
              $found = $true
              break
            }
          }
          if ($found) { break }

          $carry = [string]$parts[0]
          if ($Utf8NoBom.GetByteCount($carry) -gt $MaxLineBytes) {
            if ($carry.IndexOf($goalToken, [StringComparison]::Ordinal) -ge 0) {
              $result = $hardUnknown
              $found = $true
            } else {
              $lineOversize = $true
              $oversizeBoundary = $carry.Substring(0, [Math]::Min($goalTokenOverlapLength, $carry.Length))
              $carry = ''
            }
          }
        }
        if (-not $limited -and $position -eq 0 -and $rightPrefix.Length -gt 0) {
          throw 'transcript begins with invalid UTF-8 continuation bytes'
        }
        if (-not $found -and $position -eq 0) {
          # At the beginning of the file an oversize record without the token is
          # known irrelevant and can be skipped safely. A token-bearing one was
          # converted to unknown while streaming above.
          $line = $carry.TrimEnd([char]13)
          if (-not $lineOversize -and $line.IndexOf($goalToken, [StringComparison]::Ordinal) -ge 0) {
            $parsed = ConvertFrom-ClaudeGoalStatusLine -Line $line
            if ($null -ne $parsed) { $result = $parsed }
          }
        } elseif (-not $found -and $limited -and $position -le $scanFloor) {
          # The bounded synchronous prompt hook did not see enough transcript to
          # prove there is no older active goal. Unknown is the safe result.
          $result = $unknown
        }
      } finally {
        if ($null -ne $stream) { $stream.Dispose() }
      }

      $after = Get-Item -LiteralPath $resolved -ErrorAction Stop
      if ([int64]$before.Length -eq [int64]$after.Length -and
          [int64]$before.LastWriteTimeUtc.Ticks -eq [int64]$after.LastWriteTimeUtc.Ticks) {
        if ($script:ClaudeGoalStateCache.Count -ge 64) { $script:ClaudeGoalStateCache.Clear() }
        $script:ClaudeGoalStateCache[$cacheKey] = [pscustomobject]@{
          length = [int64]$after.Length
          last_write_ticks = [int64]$after.LastWriteTimeUtc.Ticks
          result = $result
        }
        return $result
      }
    } catch {
      if ($scanAttempt -ge 1) {
        return $unknown
      }
    }
  }
  return $unknown
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

function Get-CodexTaskUrl {
  param([object]$ThreadId)

  $raw = ([string]$ThreadId).Trim()
  if ([string]::IsNullOrWhiteSpace($raw)) { return '' }
  $parsed = [Guid]::Empty
  if (-not [Guid]::TryParseExact($raw, 'D', [ref]$parsed)) { return '' }
  return $ChatGptTaskUrlPrefix + $parsed.ToString('D')
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
    $provider = [string](Get-ObjectValue $Record 'provider' 'codex')
    $threadTitleValue = if ($provider -eq 'claude') {
      Get-ClaudeThreadTitle -TranscriptPath ([string](Get-ObjectValue $Record 'candidate_rollout_path' '')) -SessionId ([string]$Record.thread_id)
    } else {
      Get-ThreadTitle -ThreadId $Record.thread_id -SessionHome $sessionHome -SqliteHome $sessionSqliteHome
    }
    $threadTitle = Sanitize-NotificationText -Text $threadTitleValue -MaxLength 60
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
      $plainMessage = Convert-MarkdownToPlainText -Text $rawMessage
      $summary = Sanitize-NotificationText -Text $plainMessage -MaxLength $Config.maxMessageChars
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
  if ($Config.includeTaskLink -and [string](Get-ObjectValue $Record 'provider' 'codex') -eq 'codex') {
    $taskUrl = Get-CodexTaskUrl -ThreadId $Record.thread_id
    if (-not [string]::IsNullOrWhiteSpace($taskUrl)) {
      $payload['click'] = $taskUrl
      if ($Config.includeTaskLinkAction) {
        $payload['actions'] = @([ordered]@{
          action = 'view'
          label = 'Open task'
          url = $taskUrl
          clear = $true
        })
      }
    }
  }
  if ([string](Get-ObjectValue $Record 'completion_event_type' '') -eq 'turn_aborted' -or
      [string](Get-ObjectValue $Record 'goal_status' '') -eq 'blocked') {
    # One compact error glyph keeps failed turns distinguishable even when the
    # user has disabled assistant-message previews for privacy.
    $payload['tags'] = @('warning')
  } elseif (@($Config.tags).Count -gt 0) {
    $payload['tags'] = @($Config.tags)
  }
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
    candidate_revision = [string](Get-ObjectValue $Record 'candidate_revision' '')
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

function Test-ClaudeSessionHasPendingRecord {
  param([string]$SessionKey)

  foreach ($file in @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
    try {
      $record = Read-JsonFile -Path $file.FullName
      if ([string](Get-ObjectValue $record 'provider' '') -ne 'claude') { continue }
      $info = Get-ClaudeSessionStateInfo -SessionId ([string](Get-ObjectValue $record 'thread_id' ''))
      if ($null -ne $info -and $info.key -eq $SessionKey) { return $true }
    } catch {
      # An unreadable pending record may still reference this session. Preserve
      # state until the delivery worker has dead-lettered the bad record.
      return $true
    }
  }
  return $false
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
  foreach ($sessionFile in @(Get-ChildItem -LiteralPath $ClaudeSessionsDir -Filter '*.json' -File -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTimeUtc -lt $cutoff })) {
    $sessionInfo = [pscustomobject]@{
      key = $sessionFile.BaseName
      path = $sessionFile.FullName
      lock_path = Join-Path $ClaudeSessionsDir ($sessionFile.BaseName + '.lock')
    }
    [void](Invoke-WithClaudeSessionLock -Info $sessionInfo -Action {
        param($lockedInfo, $lockedCutoff)
        if (-not (Test-Path -LiteralPath $lockedInfo.path -PathType Leaf)) { return }
        $current = Get-Item -LiteralPath $lockedInfo.path -ErrorAction Stop
        if ($current.LastWriteTimeUtc -ge $lockedCutoff) { return }
        if (Test-ClaudeSessionHasPendingRecord -SessionKey $lockedInfo.key) { return }
        Remove-Item -LiteralPath $lockedInfo.path -Force -ErrorAction SilentlyContinue
      } -Arguments @($sessionInfo, $cutoff))
  }

  # Versions before 2.4.3 left one zero-byte lock per completion in the state
  # root. New code uses 256 lock shards in mutation-locks instead.
  Get-ChildItem -LiteralPath $StateRoot -Filter 'mutation-*.lock' -File -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
}

function Invoke-RuntimeMaintenance {
  Ensure-RuntimeDirectories
  $lockStream = $null
  try {
    $lockStream = [System.IO.File]::Open($MaintenanceLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  } catch {
    return 0
  }
  try {
    $config = Get-Config
    Clean-RuntimeState -ReceiptRetentionDays $config.sentRetentionDays -DeadRetentionDays $config.deadRetentionDays
    Write-RuntimeLog 'maintenance completed'
    return 0
  } catch {
    Write-RuntimeLog "maintenance error: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240)"
    return 1
  } finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
  }
}

function Clear-SyntheticTestState {
  Ensure-RuntimeDirectories
  $removed = 0
  foreach ($directory in @($PendingDir, $OutboxDir, $SentDir, $SuppressedDir, $DeadDir)) {
    $files = @(Get-ChildItem -LiteralPath $directory -Filter '*.json' -File -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) { continue }
    # Most durable histories contain no synthetic records. Search for the exact
    # fixed test UUID first so reinstall does not JSON-decode thousands of real
    # receipts; matching files are still parsed and structurally verified below.
    $candidatePaths = @(Select-String -LiteralPath $files.FullName -Pattern $SyntheticTestThreadId -SimpleMatch -List -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Path -Unique)
    foreach ($candidatePath in $candidatePaths) {
      try {
        $record = Read-JsonFile -Path $candidatePath
        if ([string](Get-ObjectValue $record 'thread_id' '') -ne $SyntheticTestThreadId) { continue }
        Remove-Item -LiteralPath $candidatePath -Force -ErrorAction Stop
        $removed++
      } catch {
        # Never remove malformed or unrelated state by filename or free text.
      }
    }
  }
  return $removed
}

$script:RolloutProbeCache = @{}
$script:ThreadDatabaseCache = @{}
$script:StateDatabasePathCache = @{}
$script:RolloutDiscoveryCache = @{}
$script:DurableCursorCache = @{}
$script:NextDurableCursorRefreshUnixMs = [int64]0
$script:ColdDiscoveryRanThisScan = $false

function Initialize-WinSqlite {
  if ($null -ne ([System.Management.Automation.PSTypeName]'CodexNtfyWinSqlite').Type) {
    return
  }
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
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

    public static string[][] QueryRows(string databasePath, string sql, string parameter, int columnCount, int maxRows) {
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
            List<string[]> rows = new List<string[]>();
            while (rows.Count < Math.Max(1, maxRows)) {
                result = sqlite3_step(statement);
                if (result == SQLITE_DONE) break;
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
                rows.Add(values);
            }
            return rows.ToArray();
        } finally {
            if (statement != IntPtr.Zero) sqlite3_finalize(statement);
            if (database != IntPtr.Zero) sqlite3_close_v2(database);
        }
    }

    private static string JsonString(string line, string name, int startIndex) {
        string token = "\"" + name + "\"";
        int key = line.IndexOf(token, Math.Max(0, startIndex), StringComparison.Ordinal);
        if (key < 0) return String.Empty;
        int colon = line.IndexOf(':', key + token.Length);
        if (colon < 0) return String.Empty;
        int index = colon + 1;
        while (index < line.Length && Char.IsWhiteSpace(line[index])) index++;
        if (index >= line.Length || line[index] != '"') return String.Empty;
        index++;
        StringBuilder value = new StringBuilder();
        bool escaped = false;
        bool closed = false;
        for (; index < line.Length; index++) {
            char current = line[index];
            if (escaped) {
                value.Append(current);
                escaped = false;
            } else if (current == '\\') {
                escaped = true;
            } else if (current == '"') {
                closed = true;
                break;
            } else {
                value.Append(current);
            }
        }
        return closed ? value.ToString() : String.Empty;
    }

    public static string[] ScanLifecycleSummary(string path, string candidateTurnId) {
        FileInfo before = new FileInfo(path);
        long initialLength = before.Length;
        long initialTicks = before.LastWriteTimeUtc.Ticks;
        long sequence = 0;
        long candidateSequence = 0;
        string candidateType = String.Empty;
        string candidateLine = String.Empty;
        long candidateCompletedSequence = 0;
        long candidateAbortedSequence = 0;
        string candidateCompletedLine = String.Empty;
        string candidateAbortedLine = String.Empty;
        bool candidateUserMessage = false;
        bool candidateFinalMessage = false;
        string currentTurn = String.Empty;
        string latestTerminalType = String.Empty;
        string latestTerminalTurn = String.Empty;
        string latestTerminalLine = String.Empty;
        long latestTerminalSequence = 0;
        string goalStatus = String.Empty;
        Dictionary<string, long> openTurns = new Dictionary<string, long>(StringComparer.OrdinalIgnoreCase);
        HashSet<string> userMessageTurns = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        List<string> lifecycleLines = new List<string>();

        if (initialLength > 0) {
            using (FileStream tail = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) {
                tail.Seek(initialLength - 1, SeekOrigin.Begin);
                if (tail.ReadByte() != 10) return new string[0];
            }
        }

        using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
        using (StreamReader reader = new StreamReader(stream, Encoding.UTF8, true, 65536)) {
            string line;
            while ((line = reader.ReadLine()) != null) {
                if (line.IndexOf("\"event_msg\"", StringComparison.Ordinal) < 0 ||
                    line.IndexOf("\"payload\"", StringComparison.Ordinal) < 0) continue;
                if (line.IndexOf("\"task_started\"", StringComparison.Ordinal) < 0 &&
                    line.IndexOf("\"task_complete\"", StringComparison.Ordinal) < 0 &&
                    line.IndexOf("\"turn_aborted\"", StringComparison.Ordinal) < 0 &&
                    line.IndexOf("\"user_message\"", StringComparison.Ordinal) < 0 &&
                    line.IndexOf("\"thread_goal_updated\"", StringComparison.Ordinal) < 0) continue;
                lifecycleLines.Add(line);
                int payloadAt = line.IndexOf("\"payload\"", StringComparison.Ordinal);
                string eventType = JsonString(line, "type", payloadAt);
                if (eventType != "task_started" && eventType != "task_complete" &&
                    eventType != "turn_aborted" && eventType != "user_message" &&
                    eventType != "thread_goal_updated") continue;
                sequence++;
                string turnId = JsonString(line, "turn_id", payloadAt);
                if (String.IsNullOrEmpty(turnId)) turnId = JsonString(line, "turn-id", payloadAt);
                if (eventType == "task_started" && !String.IsNullOrEmpty(turnId)) {
                    openTurns[turnId] = sequence;
                    currentTurn = turnId;
                } else if (eventType == "user_message") {
                    if (!String.IsNullOrEmpty(currentTurn)) {
                        userMessageTurns.Add(currentTurn);
                        if (String.Equals(currentTurn, candidateTurnId, StringComparison.OrdinalIgnoreCase))
                            candidateUserMessage = true;
                    }
                } else if ((eventType == "task_complete" || eventType == "turn_aborted") && !String.IsNullOrEmpty(turnId)) {
                    openTurns.Remove(turnId);
                    latestTerminalType = eventType;
                    latestTerminalTurn = turnId;
                    latestTerminalLine = line;
                    latestTerminalSequence = sequence;
                    if (String.Equals(turnId, candidateTurnId, StringComparison.OrdinalIgnoreCase)) {
                        candidateType = eventType;
                        candidateLine = line;
                        candidateSequence = sequence;
                        candidateFinalMessage = eventType == "turn_aborted";
                        if (eventType == "task_complete") {
                            candidateCompletedSequence = sequence;
                            candidateCompletedLine = line;
                        } else {
                            candidateAbortedSequence = sequence;
                            candidateAbortedLine = line;
                        }
                    }
                    if (String.Equals(currentTurn, turnId, StringComparison.OrdinalIgnoreCase)) currentTurn = String.Empty;
                } else if (eventType == "thread_goal_updated") {
                    int goalAt = line.IndexOf("\"goal\"", payloadAt, StringComparison.Ordinal);
                    string status = JsonString(line, "status", goalAt < 0 ? payloadAt : goalAt);
                    if (!String.IsNullOrWhiteSpace(status)) goalStatus = status.ToLowerInvariant();
                }
            }
        }

        string openLaterTurn = String.Empty;
        long openLaterSequence = 0;
        foreach (KeyValuePair<string, long> entry in openTurns) {
            if (entry.Value > openLaterSequence) {
                openLaterTurn = entry.Key;
                openLaterSequence = entry.Value;
            }
        }
        FileInfo after = new FileInfo(path);
        List<string> result = new List<string>(new[] {
            candidateType, candidateLine, candidateUserMessage ? "1" : "0", candidateFinalMessage ? "1" : "0",
            candidateSequence.ToString(), latestTerminalType, latestTerminalTurn, latestTerminalLine,
            latestTerminalSequence.ToString(), openLaterTurn, openLaterSequence.ToString(), goalStatus,
            initialLength.ToString(), initialTicks.ToString(), after.Length.ToString(), after.LastWriteTimeUtc.Ticks.ToString(),
            new DateTimeOffset(after.LastWriteTimeUtc).ToUnixTimeMilliseconds().ToString(),
            userMessageTurns.Contains(latestTerminalTurn) ? "1" : "0",
            candidateCompletedSequence.ToString(), candidateAbortedSequence.ToString(),
            candidateCompletedLine, candidateAbortedLine
        });
        result.AddRange(lifecycleLines);
        return result.ToArray();
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
  $cacheKey = ([string]$SqliteHome).ToLowerInvariant()
  if ($script:StateDatabasePathCache.ContainsKey($cacheKey)) {
    return $script:StateDatabasePathCache[$cacheKey]
  }
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
    if ($null -ne $candidate) {
      $script:StateDatabasePathCache[$cacheKey] = $candidate.FullName
      return $candidate.FullName
    }
  } catch {
    # The preferred path remains the diagnostic target when discovery fails.
  }
  $script:StateDatabasePathCache[$cacheKey] = $preferred
  return $preferred
}

function Get-RecentThreadRolloutPaths {
  param(
    [string]$SqliteHome,
    [int64]$CutoffUnixMs,
    [int]$MaxRows = 64
  )

  if ([string]::IsNullOrWhiteSpace($SqliteHome)) { return @() }
  $databasePath = Get-StateDatabasePath -SqliteHome $SqliteHome
  $queries = @(
    'SELECT rollout_path FROM threads WHERE COALESCE(NULLIF(updated_at_ms, 0), updated_at * 1000) >= CAST(?1 AS INTEGER) AND COALESCE(source, '''') NOT LIKE ''%subagent%'' ORDER BY COALESCE(NULLIF(updated_at_ms, 0), updated_at * 1000) DESC LIMIT 64',
    'SELECT rollout_path FROM threads WHERE updated_at * 1000 >= CAST(?1 AS INTEGER) AND COALESCE(source, '''') NOT LIKE ''%subagent%'' ORDER BY updated_at DESC LIMIT 64',
    'SELECT rollout_path FROM threads WHERE ?1 = ?1 AND COALESCE(source, '''') NOT LIKE ''%subagent%'' ORDER BY rowid DESC LIMIT 64'
  )
  foreach ($query in $queries) {
    $result = Invoke-SqliteRows -DatabasePath $databasePath -Sql $query -Parameter ([string]$CutoffUnixMs) -ColumnCount 1 -MaxRows $MaxRows
    if (-not $result.ok) { continue }
    $paths = @()
    foreach ($row in @($result.rows)) {
      if ($null -eq $row -or $row.Count -lt 1) { continue }
      $path = [string]$row[0]
      if (-not [string]::IsNullOrWhiteSpace($path)) { $paths += $path }
    }
    return @($paths)
  }
  return @()
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
    if ($orphanMs -gt 0) {
      try {
        $childFile = Get-Item -LiteralPath $rolloutPath -ErrorAction Stop
        $childModifiedMs = ([DateTimeOffset]$childFile.LastWriteTimeUtc).ToUnixTimeMilliseconds()
        if ($childModifiedMs -gt 0 -and $NowUnixMs - $childModifiedMs -ge $orphanMs) {
          [void]$unknownSince.Remove($childId)
          continue
        }
      } catch {
        # The authoritative probe below retains strict unknown/error semantics.
      }
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

function Get-FastRolloutProbe {
  param(
    [string]$Path,
    [string]$CandidateTurnId
  )

  if ([string]::IsNullOrWhiteSpace($Path) -or [string]::IsNullOrWhiteSpace($CandidateTurnId) -or
      -not (Test-Path -LiteralPath $Path)) {
    return [pscustomobject]@{ ok = $false; state = $null; error = 'fast lifecycle probe unavailable' }
  }
  try {
    Initialize-WinSqlite
    $values = [CodexNtfyWinSqlite]::ScanLifecycleSummary($Path, $CandidateTurnId)
    if ($null -eq $values -or $values.Count -lt 22 -or [string]$values[0] -notin @('task_complete', 'turn_aborted')) {
      return [pscustomobject]@{ ok = $false; state = $null; error = 'candidate terminal event unavailable' }
    }
    for ($lineIndex = 22; $lineIndex -lt $values.Count; $lineIndex++) {
      try {
        $validatedEnvelope = [string]$values[$lineIndex] | ConvertFrom-Json -ErrorAction Stop
        if ([string](Get-ObjectValue $validatedEnvelope 'type' '') -ne 'event_msg') {
          return [pscustomobject]@{ ok = $false; state = $null; error = 'non-event lifecycle envelope' }
        }
        $validatedPayload = Get-ObjectValue $validatedEnvelope 'payload'
        if ($null -eq $validatedPayload) {
          return [pscustomobject]@{ ok = $false; state = $null; error = 'lifecycle payload unavailable' }
        }
      } catch {
        return [pscustomobject]@{ ok = $false; state = $null; error = 'invalid lifecycle JSON' }
      }
    }
    $state = New-RolloutProbeState -Path $Path
    $candidateType = [string]$values[0]
    $candidateSequence = [int64]$values[4]
    $latestType = [string]$values[5]
    $latestTurn = [string]$values[6]
    $latestSequence = [int64]$values[8]
    $openLaterTurn = [string]$values[9]
    $openLaterSequence = [int64]$values[10]
    $candidateCompletedSequence = [int64]$values[18]
    $candidateAbortedSequence = [int64]$values[19]
    $state.offset = [int64]$values[14]
    $state.sequence = [Math]::Max($candidateSequence, [Math]::Max($latestSequence, $openLaterSequence))
    $state.modifiedUnixMs = [int64]$values[16]
    $state.snapshotChanged = [int64]$values[12] -ne [int64]$values[14] -or [int64]$values[13] -ne [int64]$values[15]
    $state.goalStatus = [string]$values[11]
    if ([string]$values[2] -eq '1') { $state.userMessageTurns[$CandidateTurnId] = $true }

    $seenTerminals = @{}
    $terminalCandidates = @(
        [pscustomobject]@{ turn = $CandidateTurnId; type = 'task_complete'; sequence = $candidateCompletedSequence; line = [string]$values[20] },
        [pscustomobject]@{ turn = $CandidateTurnId; type = 'turn_aborted'; sequence = $candidateAbortedSequence; line = [string]$values[21] },
        [pscustomobject]@{ turn = $latestTurn; type = $latestType; sequence = $latestSequence; line = [string]$values[7] }
      ) | Sort-Object @{ Expression = { [int64]$_.sequence } }, @{ Expression = { [string]$_.type } }
    foreach ($terminal in $terminalCandidates) {
      if ([string]::IsNullOrWhiteSpace($terminal.turn) -or $terminal.type -notin @('task_complete', 'turn_aborted') -or
          [int64]$terminal.sequence -le 0 -or [string]::IsNullOrWhiteSpace($terminal.line)) { continue }
      $terminalKey = '{0}|{1}|{2}' -f $terminal.turn, $terminal.type, $terminal.sequence
      if ($seenTerminals.ContainsKey($terminalKey)) { continue }
      $seenTerminals[$terminalKey] = $true
      $payload = $null
      try {
        $envelope = $terminal.line | ConvertFrom-Json -ErrorAction Stop
        $payload = Get-ObjectValue $envelope 'payload'
        $parsedType = [string](Get-ObjectValue $payload 'type' '')
        $parsedTurn = [string](Get-FirstObjectValue $payload @('turn_id', 'turn-id'))
        if ($parsedType -ne $terminal.type -or $parsedTurn -ne $terminal.turn) { continue }
      } catch {
        continue
      }
      if ($terminal.type -eq 'turn_aborted') {
        $state.abortedTurns[$terminal.turn] = [int64]$terminal.sequence
      } else {
        $state.completedTurns[$terminal.turn] = [int64]$terminal.sequence
      }
      $state.terminalTurns[$terminal.turn] = [int64]$terminal.sequence
      $state.terminalEventTypes[$terminal.turn] = [string]$terminal.type
      if (($terminal.turn -eq $CandidateTurnId -and [string]$values[2] -eq '1') -or
          ($terminal.turn -eq $latestTurn -and [string]$values[17] -eq '1')) {
        $state.userMessageTurns[$terminal.turn] = $true
      }
      $message = ''
      $message = [string](Get-FirstObjectValue $payload @('last_agent_message', 'last-assistant-message', 'last_assistant_message'))
      $state.terminalMessages[$terminal.turn] = Sanitize-NotificationText -Text $message -MaxLength 4000 -PreserveLines
      if ($terminal.type -eq 'turn_aborted' -or -not [string]::IsNullOrWhiteSpace($message)) {
        $state.finalMessageTurns[$terminal.turn] = $true
      }
    }
    if (-not $state.completedTurns.ContainsKey($CandidateTurnId) -and -not $state.abortedTurns.ContainsKey($CandidateTurnId)) {
      return [pscustomobject]@{ ok = $false; state = $null; error = 'candidate terminal JSON invalid' }
    }
    if (-not [string]::IsNullOrWhiteSpace($openLaterTurn) -and $openLaterSequence -gt $candidateSequence) {
      $state.openTurns[$openLaterTurn] = $openLaterSequence
      $state.currentTurnId = $openLaterTurn
      $state.lastLifecycleType = 'task_started'
      $state.lastLifecycleTurnId = $openLaterTurn
    } else {
      $state.lastLifecycleType = $latestType
      $state.lastLifecycleTurnId = $latestTurn
    }
    if (-not $state.snapshotChanged) {
      $script:RolloutProbeCache[$Path.ToLowerInvariant()] = $state
    }
    return [pscustomobject]@{ ok = $true; state = $state; snapshotChanged = [bool]$state.snapshotChanged; error = '' }
  } catch {
    return [pscustomobject]@{ ok = $false; state = $null; error = Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240 }
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
  if ($Config.idleDetectionMode -eq 'strict' -and $now -ge $fallbackAt) {
    return New-GateResult -State 'unverifiable' -Reason $Reason -RetryAtUnixMs $now
  }

  $previousReason = [string](Get-ObjectValue $Record 'unknown_gate_reason' '')
  $probeCount = if ($previousReason -eq $Reason) { [int](Get-ObjectValue $Record 'unknown_probe_count' 0) } else { 0 }
  $baseSeconds = [Math]::Max(0.1, [double]$Config.goalPollSeconds)
  $maxSeconds = [Math]::Max($baseSeconds, [double]$Config.unknownRetryMaxSeconds)
  $delaySeconds = [Math]::Min($maxSeconds, $baseSeconds * [Math]::Pow(2, [Math]::Min(10, $probeCount)))
  Set-RecordValue -Record $Record -Name 'unknown_gate_reason' -Value $Reason
  Set-RecordValue -Record $Record -Name 'unknown_probe_count' -Value ($probeCount + 1)
  $retryAt = $now + [int64]($delaySeconds * 1000)
  if ($fallbackAt -gt $now) { $retryAt = [Math]::Min($retryAt, $fallbackAt) }
  return New-GateResult -State 'unknown' -Reason $Reason -RetryAtUnixMs $retryAt
}

function Invoke-SqliteRows {
  param(
    [string]$DatabasePath,
    [string]$Sql,
    [string]$Parameter,
    [int]$ColumnCount,
    [int]$MaxRows = 64
  )

  if (-not (Test-Path -LiteralPath $DatabasePath)) {
    return [pscustomobject]@{ ok = $false; missing = $true; rows = @(); error = 'database missing' }
  }
  try {
    Initialize-WinSqlite
    $rows = [CodexNtfyWinSqlite]::QueryRows($DatabasePath, $Sql, $Parameter, $ColumnCount, $MaxRows)
    return [pscustomobject]@{ ok = $true; missing = $false; rows = @($rows); error = '' }
  } catch {
    return [pscustomobject]@{
      ok = $false
      missing = $false
      rows = @()
      error = Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240
    }
  }
}

function Test-RecordIdleGate {
  param(
    [object]$Record,
    [object]$Config
  )

  $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  if ([string](Get-ObjectValue $Record 'provider' 'codex') -eq 'claude') {
    $candidateKind = [string](Get-ObjectValue $Record 'candidate_kind' '')
    $recordGoalState = ([string](Get-ObjectValue $Record 'claude_goal_state' '')).Trim().ToLowerInvariant()
    $recordGoalMarker = [string](Get-ObjectValue $Record 'claude_goal_marker' '')
    $needsIdleFallback = $recordGoalState -eq 'unknown'
    $sessionState = $null
    if ($candidateKind -ne 'claude_stop_failure') {
      if ($recordGoalState -eq 'unverifiable') {
        return New-GateResult -State 'unverifiable' -Reason 'claude-goal-record-oversize' -RetryAtUnixMs $now
      }
      if ($recordGoalState -eq 'cleared') {
        return New-GateResult -State 'cancelled' -Reason 'claude-goal-cleared' -RetryAtUnixMs $now
      }
      if ($recordGoalState -eq 'failed') {
        Set-RecordValue -Record $Record -Name 'goal_status' -Value 'blocked'
      } elseif ($recordGoalState -eq 'achieved') {
        Set-RecordValue -Record $Record -Name 'goal_status' -Value 'complete'
      } elseif ($recordGoalState -eq 'active') {
        $latestGoal = Get-ClaudeGoalTranscriptState -TranscriptPath ([string](Get-ObjectValue $Record 'candidate_rollout_path' ''))
        $latestState = [string]$latestGoal.state
        $latestMarker = [string]$latestGoal.marker
        if ($latestState -eq 'unverifiable') {
          return New-GateResult -State 'unverifiable' -Reason 'claude-goal-record-oversize' -RetryAtUnixMs $now
        }
        if ($latestState -eq 'active') {
          return New-GateResult -State 'busy' -Reason 'claude-goal-active' -RetryAtUnixMs ($now + 500)
        }
        $terminalTransition = $latestState -in @('achieved', 'failed', 'cleared') -and
          -not [string]::IsNullOrWhiteSpace($recordGoalMarker) -and
          -not [string]::IsNullOrWhiteSpace($latestMarker) -and
          $latestMarker -ne $recordGoalMarker
        if ($terminalTransition) {
          if ($latestState -eq 'cleared') {
            return New-GateResult -State 'cancelled' -Reason 'claude-goal-cleared' -RetryAtUnixMs $now
          }
          Set-RecordValue -Record $Record -Name 'goal_status' -Value $(if ($latestState -eq 'failed') { 'blocked' } else { 'complete' })
        } else {
          # No terminal marker newer than the active marker is proof that the
          # /goal loop has ended. A host idle event is only a fallback for a
          # missing/temporarily unreadable transcript, never a prerequisite.
          $needsIdleFallback = $true
        }
      } elseif ($recordGoalState -eq 'unknown') {
        # A transcript can change while the async Stop process is reading it.
        # Reconcile on every poll so a transient partial line never makes a
        # candidate depend permanently on the optional Notification hook.
        $latestGoal = Get-ClaudeGoalTranscriptState -TranscriptPath ([string](Get-ObjectValue $Record 'candidate_rollout_path' ''))
        $latestState = [string]$latestGoal.state
        $latestMarker = [string]$latestGoal.marker
        if ($latestState -eq 'unverifiable') {
          return New-GateResult -State 'unverifiable' -Reason 'claude-goal-record-oversize' -RetryAtUnixMs $now
        }
        if ($latestState -eq 'active') {
          Set-RecordValue -Record $Record -Name 'claude_goal_state' -Value 'active'
          Set-RecordValue -Record $Record -Name 'claude_goal_marker' -Value $latestMarker
          return New-GateResult -State 'busy' -Reason 'claude-goal-active' -RetryAtUnixMs ($now + 500)
        }
        if ($latestState -eq 'none') {
          $needsIdleFallback = $false
        } elseif ($latestState -in @('achieved', 'failed', 'cleared')) {
          $sessionState = Read-ClaudeSessionState -SessionId ([string](Get-ObjectValue $Record 'thread_id' ''))
          $recordEpoch = [int64](Get-ObjectValue $Record 'claude_session_epoch' 0)
          $stateEpoch = [int64](Get-ObjectValue $sessionState 'epoch' 0)
          $baselineCaptured = [bool](Get-ObjectValue $sessionState 'goal_baseline_captured' $false)
          $baselineMarker = [string](Get-ObjectValue $sessionState 'goal_baseline_marker' '')
          $isCurrentTerminal = $recordEpoch -gt 0 -and $stateEpoch -eq $recordEpoch -and
            $baselineCaptured -and -not [string]::IsNullOrWhiteSpace($latestMarker) -and
            $latestMarker -ne $baselineMarker
          if ($isCurrentTerminal) {
            if ($latestState -eq 'cleared') {
              return New-GateResult -State 'cancelled' -Reason 'claude-goal-cleared' -RetryAtUnixMs $now
            }
            Set-RecordValue -Record $Record -Name 'goal_status' -Value $(if ($latestState -eq 'failed') { 'blocked' } else { 'complete' })
          }
          # A terminal marker equal to the prompt baseline is historical. Both
          # historical and newly proven terminal states are non-running.
          $needsIdleFallback = $false
        }
      }
    }

    if ($candidateKind -ne 'claude_stop_failure' -and $needsIdleFallback) {
      if ($null -eq $sessionState) {
        $sessionState = Read-ClaudeSessionState -SessionId ([string](Get-ObjectValue $Record 'thread_id' ''))
      }
      $idleConfirmed = $null -ne $sessionState -and [string](Get-ObjectValue $sessionState 'state' '') -eq 'idle'
      $recordEpoch = [int64](Get-ObjectValue $Record 'claude_session_epoch' 0)
      $stateEpoch = [int64](Get-ObjectValue $sessionState 'epoch' 0)
      if ($recordEpoch -gt 0 -and $stateEpoch -gt 0 -and $recordEpoch -ne $stateEpoch) { $idleConfirmed = $false }
      $statePrompt = [string](Get-ObjectValue $sessionState 'prompt_id' '')
      $recordPrompt = [string](Get-ObjectValue $Record 'turn_id' '')
      if ([string]::IsNullOrWhiteSpace($statePrompt) -or
          [string]::IsNullOrWhiteSpace($recordPrompt) -or
          $statePrompt -ne $recordPrompt) { $idleConfirmed = $false }
      $stateTranscript = [string](Get-ObjectValue $sessionState 'transcript_path' '')
      $recordTranscript = [string](Get-ObjectValue $Record 'candidate_rollout_path' '')
      if (-not [string]::IsNullOrWhiteSpace($stateTranscript) -and
          -not [string]::IsNullOrWhiteSpace($recordTranscript) -and
          -not [string]::Equals($stateTranscript, $recordTranscript, [StringComparison]::OrdinalIgnoreCase)) {
        $idleConfirmed = $false
      }
      $idleAt = [int64](Get-ObjectValue $sessionState 'idle_unix_ms' 0)
      $createdAt = [int64](Get-ObjectValue $Record 'created_unix_ms' $now)
      if ($idleAt -le 0 -or $idleAt -lt ($createdAt - 30000)) { $idleConfirmed = $false }
      if (-not $idleConfirmed) {
        return New-GateResult -State 'busy' -Reason 'claude-goal-awaiting-finality' -RetryAtUnixMs ($now + 500)
      }
    }
    $createdAt = [int64](Get-ObjectValue $Record 'created_unix_ms' $now)
    $quietAt = $createdAt + [int64]([Math]::Max(0, $Config.idleGraceSeconds) * 1000)
    $transcriptPath = [string](Get-ObjectValue $Record 'candidate_rollout_path' '')
    if (-not [string]::IsNullOrWhiteSpace($transcriptPath)) {
      try {
        $transcript = Get-Item -LiteralPath $transcriptPath -ErrorAction Stop
        $transcriptQuietAt = [DateTimeOffset]$transcript.LastWriteTimeUtc
        $transcriptQuietAt = $transcriptQuietAt.ToUnixTimeMilliseconds() + [int64]([Math]::Max(0, $Config.idleGraceSeconds) * 1000)
        if ($transcriptQuietAt -gt $quietAt) { $quietAt = $transcriptQuietAt }
      } catch {
        # Stop.last_assistant_message is authoritative; a missing/lagging
        # transcript must not suppress an otherwise verifiable completion.
      }
    }
    if ($now -lt $quietAt) {
      return New-GateResult -State 'busy' -Reason 'claude-not-quiet' -RetryAtUnixMs $quietAt
    }
    return New-GateResult -State 'ready' -Reason 'claude-idle' -RetryAtUnixMs $now
  }
  if ($Config.idleDetectionMode -eq 'off') {
    return New-GateResult -State 'ready' -Reason 'idle-detection-off' -RetryAtUnixMs $now
  }
  $previousGateReason = [string](Get-ObjectValue $Record 'gate_reason' '')
  $createdAt = [int64](Get-ObjectValue $Record 'created_unix_ms' $now)
  $unverifiableAt = $createdAt + [int64]([Math]::Max(0, $Config.idleProbeGraceSeconds) * 1000)
  if ($Config.idleDetectionMode -eq 'strict' -and
      $now -ge $unverifiableAt -and
      $previousGateReason -in @(
        'candidate-thread-missing', 'classification-unknown', 'rollout-path-unknown',
        'rollout-probe-failed', 'candidate-turn-missing',
        'candidate-task-complete-not-observed', 'newer-completion-recovery-failed',
        'goal-probe-failed', 'descendant-probe-failed'
      )) {
    return New-GateResult -State 'unverifiable' -Reason $previousGateReason -RetryAtUnixMs $now
  }
  $threadId = [string](Get-ObjectValue $Record 'thread_id' '')
  $turnId = [string](Get-ObjectValue $Record 'turn_id' '')
  $candidateKind = [string](Get-ObjectValue $Record 'candidate_kind' 'legacy')
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
  $probeCacheKey = $rolloutPath.ToLowerInvariant()
  $probe = if ($script:RolloutProbeCache.ContainsKey($probeCacheKey)) {
    Update-RolloutProbe -Path $rolloutPath
  } else {
    Get-FastRolloutProbe -Path $rolloutPath -CandidateTurnId $turnId
  }
  if ($probe.ok -and -not [string]::IsNullOrWhiteSpace($turnId) -and
      -not $probe.state.completedTurns.ContainsKey($turnId) -and
      -not $probe.state.abortedTurns.ContainsKey($turnId)) {
    # The cache is path-scoped while the native cold summary materializes the
    # requested candidate plus the absolute latest terminal. A second pending
    # candidate on the same already-scanned rollout needs one fresh native pass.
    [void]$script:RolloutProbeCache.Remove($probeCacheKey)
    $probe = Get-FastRolloutProbe -Path $rolloutPath -CandidateTurnId $turnId
  }
  if (-not $probe.ok) {
    # Stop hooks can race the terminal rollout write, and future/legacy formats
    # may not expose enough lifecycle data for the native summary. Preserve the
    # original full parser as a correctness fallback.
    $probe = Update-RolloutProbe -Path $rolloutPath
  }
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
      # This candidate is an intermediate completion. The open turn will create
      # its own terminal candidate; retaining the predecessor can only produce
      # a late, misleading notification.
      return New-GateResult -State 'superseded' -Reason 'later-task-open' -RetryAtUnixMs $now
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

  $script:ColdDiscoveryRanThisScan = $false
  $found = @{}
  $localSqliteHome = [string](Get-ObjectValue $Config 'workerSqlitePath' $CodexHome)
  if ([string]::IsNullOrWhiteSpace($localSqliteHome)) { $localSqliteHome = $CodexHome }
  $localRoot = [pscustomobject]@{
      path = $CodexHome
      session_codex_home = $CodexHome
      session_sqlite_home = $localSqliteHome
      origin = ''
    }
  $configuredRoots = if ($null -ne $Config) { @(Get-ObjectValue $Config 'watchRoots' @()) } else { @() }
  $roots = switch ($ScanScope) {
    'Local' {
      @($localRoot) + @($configuredRoots | Where-Object {
          -not ([string](Get-ObjectValue $_ 'session_codex_home' (Get-ObjectValue $_ 'path' ''))).StartsWith('\\')
        })
      break
    }
    'Remote' {
      @($configuredRoots | Where-Object {
          ([string](Get-ObjectValue $_ 'session_codex_home' (Get-ObjectValue $_ 'path' ''))).StartsWith('\\')
        })
      break
    }
    default {
      @($localRoot) + @($configuredRoots)
      break
    }
  }
  $today = [DateTime]::Now.Date
  $nowUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  $discoverySeconds = [Math]::Max(0.1, [double](Get-ObjectValue $Config 'watchDiscoverySeconds' 60))
  $initialReplaySeconds = [Math]::Max(0, [double](Get-ObjectValue $Config 'watchInitialReplaySeconds' 15))
  $recentWindowSeconds = [Math]::Max(2 * $discoverySeconds, $initialReplaySeconds)
  $recentCutoffUtc = [DateTime]::UtcNow.AddSeconds(-$recentWindowSeconds)
  $initialReplayCutoffUtc = [DateTime]::UtcNow.AddSeconds(-$initialReplaySeconds)
  $durableCursorPaths = @{}

  # Cursor paths remain authoritative even after a session moves outside the
  # today/yesterday layout or into archived_sessions. Read their lightweight
  # metadata on the cold cadence, but probe only one rotating batch of rollout
  # files per cycle. Hot files are still found immediately below by mtime.
  # The persistent local scanner must stay hot. Active and recently resumed
  # local threads are obtained from Codex's SQLite index below; historical
  # cursor enumeration is reserved for isolated remote/manual scans.
  $refreshDurableCursors = $ScanScope -ne 'Local' -and $nowUnixMs -ge $script:NextDurableCursorRefreshUnixMs
  if ($refreshDurableCursors) {
    $script:ColdDiscoveryRanThisScan = $true
    $refreshedCursorCache = @{}
    $cursorFiles = @(Get-ChildItem -LiteralPath $WatchDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object Name)
    $cursorBatchSize = [int](Get-ObjectValue $Config 'watchCursorBatchSize' 64)
    $cursorMetadata = @{}
    $localCursorFiles = @()
    $remoteCursorFiles = @()
    foreach ($cursorFile in $cursorFiles) {
      try {
        $cursor = Read-JsonFile -Path $cursorFile.FullName
        $rolloutPath = [string](Get-ObjectValue $cursor 'rollout_path' '')
        if ([string]::IsNullOrWhiteSpace($rolloutPath)) { continue }
        $durableCursorPaths[$rolloutPath.ToLowerInvariant()] = $true
        $sessionHome = [string](Get-ObjectValue $cursor 'session_codex_home' '')
        $isRemoteCursor = $sessionHome.StartsWith('\\') -or $rolloutPath.StartsWith('\\')
        $cursorMetadata[$cursorFile.FullName.ToLowerInvariant()] = $cursor
        if ($isRemoteCursor) {
          $remoteCursorFiles += $cursorFile
        } else {
          $localCursorFiles += $cursorFile
        }
      } catch {
        continue
      }
    }

    # Local histories can contain hundreds of immutable cursors, so rotate a
    # bounded batch. Remote scans already run in their own timeout-supervised
    # process and must inspect every remote cursor independently: sharing the
    # local batch could otherwise postpone a resumed WSL/SSH task for minutes.
    $selectedLocalCursorFiles = @()
    if ($localCursorFiles.Count -gt 0) {
      $cursorBatchCount = [int][Math]::Max(1, [Math]::Ceiling($localCursorFiles.Count / [double]$cursorBatchSize))
      $cadenceMs = [int64][Math]::Max(1, $discoverySeconds * 1000)
      $cursorBatchIndex = [int]([Math]::Floor($nowUnixMs / [double]$cadenceMs) % $cursorBatchCount)
      $cursorBatchStart = $cursorBatchIndex * $cursorBatchSize
      $cursorBatchEnd = [int][Math]::Min($localCursorFiles.Count, $cursorBatchStart + $cursorBatchSize)
      for ($cursorIndex = $cursorBatchStart; $cursorIndex -lt $cursorBatchEnd; $cursorIndex++) {
        $selectedLocalCursorFiles += $localCursorFiles[$cursorIndex]
      }
    }
    $selectedCursorFiles = switch ($ScanScope) {
      'Local' { @($selectedLocalCursorFiles); break }
      'Remote' { @($remoteCursorFiles); break }
      default { @($selectedLocalCursorFiles) + @($remoteCursorFiles); break }
    }
    foreach ($cursorFile in @($selectedCursorFiles)) {
      try {
        $cursor = $cursorMetadata[$cursorFile.FullName.ToLowerInvariant()]
        $rolloutPath = [string](Get-ObjectValue $cursor 'rollout_path' '')
        if ([string]::IsNullOrWhiteSpace($rolloutPath)) { continue }
        $sessionHome = [string](Get-ObjectValue $cursor 'session_codex_home' '')
        $sqliteHome = [string](Get-ObjectValue $cursor 'session_sqlite_home' '')
        $rootOrigin = [string](Get-ObjectValue $cursor 'origin' '')
        if (-not (Test-Path -LiteralPath $rolloutPath -PathType Leaf -ErrorAction SilentlyContinue)) { continue }
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
        $entry = [pscustomobject]@{
          file = $file
          rollout_path = $file.FullName
          session_codex_home = $sessionHome
          session_sqlite_home = $sqliteHome
          origin = $rootOrigin
          force_replay = $false
        }
        $refreshedCursorCache[$file.FullName.ToLowerInvariant()] = $entry
      } catch {
        continue
      }
    }
    $script:DurableCursorCache = $refreshedCursorCache
    $script:NextDurableCursorRefreshUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + [int64]($discoverySeconds * 1000)
  }
  foreach ($cursorPathKey in @($script:DurableCursorCache.Keys)) {
    $durableCursorPaths[$cursorPathKey] = $true
    if ($refreshDurableCursors) {
      $entry = $script:DurableCursorCache[$cursorPathKey]
      Add-RolloutWatchEntry -Found $found -File (Get-ObjectValue $entry 'file') -SessionHome ([string](Get-ObjectValue $entry 'session_codex_home' '')) -SqliteHome ([string](Get-ObjectValue $entry 'session_sqlite_home' '')) -RootOrigin ([string](Get-ObjectValue $entry 'origin' ''))
    }
  }

  foreach ($root in @($roots)) {
    try {
      $sessionHome = [string](Get-ObjectValue $root 'session_codex_home' (Get-ObjectValue $root 'path' ''))
      if ([string]::IsNullOrWhiteSpace($sessionHome)) { continue }
      $sqliteHome = [string](Get-ObjectValue $root 'session_sqlite_home' $sessionHome)
      if ([string]::IsNullOrWhiteSpace($sqliteHome)) { $sqliteHome = $sessionHome }
      $rootOrigin = [string](Get-ObjectValue $root 'origin' '')
      $isRemoteRoot = $sessionHome.StartsWith('\\')
      $cacheKey = $sessionHome.ToLowerInvariant()
      $cache = if ($script:RolloutDiscoveryCache.ContainsKey($cacheKey)) { $script:RolloutDiscoveryCache[$cacheKey] } else { $null }
      $rootRefreshDue = $null -eq $cache -or $nowUnixMs -ge [int64](Get-ObjectValue $cache 'next_unix_ms' 0)
      $sessions = Join-Path $sessionHome 'sessions'
      $quickFiles = @()
      $recentCutoffUnixMs = $nowUnixMs - [int64]($recentWindowSeconds * 1000)
      foreach ($recentRolloutPath in @(Get-RecentThreadRolloutPaths -SqliteHome $sqliteHome -CutoffUnixMs $recentCutoffUnixMs -MaxRows 16)) {
        try {
          $resolvedRecentPath = Resolve-RolloutPath -DatabasePathValue $recentRolloutPath -SessionHome $sessionHome
          $isRemoteRecentPath = $resolvedRecentPath.StartsWith('\\')
          if (($ScanScope -eq 'Local' -and $isRemoteRecentPath) -or
              ($ScanScope -eq 'Remote' -and -not $isRemoteRecentPath -and $isRemoteRoot)) { continue }
          $quickFiles += Get-Item -LiteralPath $resolvedRecentPath -ErrorAction Stop
        } catch {
          continue
        }
      }
      # Direct WSL hooks bridge immediately. The UNC fallback is intentionally
      # sampled on the cold cadence; touching it every two seconds can block a
      # healthy local scan for many seconds when WSL is suspended or busy.
      if ((-not $isRemoteRoot -or $rootRefreshDue) -and
          (Test-Path -LiteralPath $sessions -PathType Container -ErrorAction SilentlyContinue)) {
        foreach ($daysBack in @(0, 1)) {
          $day = $today.AddDays(-$daysBack)
          $directory = Join-Path (Join-Path (Join-Path $sessions $day.ToString('yyyy')) $day.ToString('MM')) $day.ToString('dd')
          if (Test-Path -LiteralPath $directory -PathType Container -ErrorAction SilentlyContinue) {
            $quickFiles += @(Get-ChildItem -LiteralPath $directory -Filter '*.jsonl' -File -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTimeUtc -ge $recentCutoffUtc })
          }
        }
        $quickFiles += @(Get-ChildItem -LiteralPath $sessions -Filter '*.jsonl' -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTimeUtc -ge $recentCutoffUtc })
      }
      $quickPathSet = @{}
      foreach ($file in @($quickFiles)) {
        $quickMetadata = Get-RolloutMetadata -Path $file.FullName
        $quickSource = Get-ObjectValue $quickMetadata 'source'
        $quickIsSubagent = ($quickSource -is [string] -and $quickSource.Trim().Equals('subagent', [StringComparison]::OrdinalIgnoreCase)) -or
          ($null -ne $quickSource -and $quickSource -isnot [string] -and $null -ne (Get-ObjectValue $quickSource 'subagent'))
        if ($quickIsSubagent) { continue }
        if ($null -ne $file) { $quickPathSet[$file.FullName.ToLowerInvariant()] = $true }
        Add-RolloutWatchEntry -Found $found -File $file -SessionHome $sessionHome -SqliteHome $sqliteHome -RootOrigin $rootOrigin
      }

      if ($rootRefreshDue) {
        $script:ColdDiscoveryRanThisScan = $true
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
        # Full archive walks are kept only for explicit/manual ScanScope=All.
        # Continuous local and remote workers use the SQLite recent-thread index,
        # current-day paths, and durable cursors without traversing multi-GB trees.
        foreach ($rootName in $(if ($isRemoteRoot -or $ScanScope -ne 'All') { @() } else { @('sessions', 'archived_sessions') })) {
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
          # Start the next discovery interval after this potentially slow walk;
          # otherwise a walk longer than the interval immediately repeats.
          next_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + [int64]($discoverySeconds * 1000)
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
  if (-not (Test-RolloutScanParentAlive)) { return 0 }
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
    $observedLengthProperty = $state.PSObject.Properties['observed_length']
    $observedTicksProperty = $state.PSObject.Properties['observed_write_ticks']
    $snapshotUnchanged = $null -ne $observedLengthProperty -and
      $null -ne $observedTicksProperty -and
      [int64]$observedLengthProperty.Value -eq [int64]$fileInfo.Length -and
      [int64]$observedTicksProperty.Value -eq [int64]$fileInfo.LastWriteTimeUtc.Ticks
    # Rollouts are append-only. Legacy cursors do not have snapshot metadata,
    # but offset==length is still a safe no-op and avoids one mass rewrite on
    # upgrade. New cursors also skip unchanged partial-line snapshots.
    if ($snapshotUnchanged -or
        ($null -eq $observedLengthProperty -and $offset -eq [int64]$fileInfo.Length)) {
      return 0
    }
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
    if (-not (Test-RolloutScanParentAlive)) { return 0 }
    Write-JsonAtomic -Path $statePath -Value ([ordered]@{
        schema = 1
        rollout_path = $File.FullName
        session_codex_home = $sessionHome
        session_sqlite_home = $sqliteHome
        origin = $rootOrigin
        offset = $offset
        seen_unix_ms = $NowUnixMs
        observed_length = [int64]$snapshotLength
        observed_write_ticks = [int64]$fileInfo.LastWriteTimeUtc.Ticks
      })
    return 0
  }

  if ($stateWasMissing) {
    # Persist the starting cursor before accounting any completion. If queueing
    # fails, the next scan must retry from here even after the replay window.
    if (-not (Test-RolloutScanParentAlive)) { return 0 }
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
    if (-not (Test-RolloutScanParentAlive)) { return 0 }
    $classification = if ($source -is [string] -and -not [string]::IsNullOrWhiteSpace($source)) {
      if ($source.Trim().Equals('subagent', [StringComparison]::OrdinalIgnoreCase)) { 'subagent' } else { 'root' }
    } elseif ($null -ne $source -and $source -isnot [string] -and $null -ne (Get-ObjectValue $source 'subagent')) {
      'subagent'
    } else {
      Get-EventClassification -Event $event -ThreadId $threadId -SessionHome $sessionHome
    }
    $eventOrigin = if (-not [string]::IsNullOrWhiteSpace($rootOrigin)) {
      $rootOrigin
    } elseif (-not [string]::IsNullOrWhiteSpace($originator)) {
      $originator
    } else {
      Get-DefaultOrigin
    }
    $record = New-EventRecord -Event $event -EventOrigin $eventOrigin -EventSessionHome $sessionHome -EventSqliteHome $sqliteHome -EventClassification $classification -EventIncludeMessage $Config.includeMessage -CandidateKind 'rollout_watch' -SourceEvent 'rollout-watch'
    if (-not (Test-RolloutScanParentAlive)) { return 0 }
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
  if (-not (Test-RolloutScanParentAlive)) { return 0 }
  Write-JsonAtomic -Path $statePath -Value ([ordered]@{
      schema = 1
      rollout_path = $File.FullName
      session_codex_home = $sessionHome
      session_sqlite_home = $sqliteHome
      origin = $rootOrigin
      offset = $newOffset
      seen_unix_ms = $NowUnixMs
      thread_id = $threadId
      observed_length = [int64]$snapshotLength
      observed_write_ticks = [int64]$fileInfo.LastWriteTimeUtc.Ticks
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
  if ($sequenceId -notmatch '^(?:codex|claude)-[0-9a-f]{32}$') {
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

function Invoke-PendingRecordCommit {
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
    $canonicalRevision = [string](Get-ObjectValue $canonical 'candidate_revision' '')
    $incomingRevision = [string](Get-ObjectValue $incomingRecord 'candidate_revision' '')
    if (-not [string]::IsNullOrWhiteSpace($canonicalRevision) -and
        -not [string]::IsNullOrWhiteSpace($incomingRevision) -and
        $canonicalRevision -ne $incomingRevision) {
      return [pscustomobject]@{ status = 'stale-snapshot' }
    }
    $canonicalIsStop = Test-IsStopEvidence -Record $canonical
    $incomingIsStop = Test-IsStopEvidence -Record $incomingRecord
    if ($canonicalIsStop -and -not $incomingIsStop) {
      return [pscustomobject]@{ status = 'stop-won' }
    }
    $writeRecord = if ($canonicalIsStop) { $canonical } else { $incomingRecord }
    if ($canonicalIsStop) {
      foreach ($name in @('next_attempt_unix_ms', 'gate_reason', 'goal_status', 'completion_event_type', 'active_descendants', 'descendant_unknown_since', 'candidate_rollout_path', 'rollout_sequence', 'claude_goal_state', 'claude_goal_marker')) {
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

function Get-ClaudeSessionCommitDisposition {
  param(
    [object]$Record,
    [object]$SessionState
  )

  $recordEpoch = [int64](Get-ObjectValue $Record 'claude_session_epoch' 0)
  if ($null -eq $SessionState) {
    if ($recordEpoch -gt 0) { return 'unverifiable' }
    return 'current'
  }
  $recordSession = [string](Get-ObjectValue $Record 'thread_id' '')
  if ([string](Get-ObjectValue $SessionState 'session_id' '') -ne $recordSession) { return 'unverifiable' }
  $stateEpoch = [int64](Get-ObjectValue $SessionState 'epoch' 0)
  if ($recordEpoch -gt 0) {
    if ($stateEpoch -le 0) { return 'unverifiable' }
    if ($stateEpoch -ne $recordEpoch) { return 'stale' }
  } elseif ($stateEpoch -gt 0 -and [string](Get-ObjectValue $SessionState 'state' '') -eq 'busy') {
    return 'stale'
  }
  $recordPrompt = [string](Get-ObjectValue $Record 'turn_id' '')
  $statePrompt = [string](Get-ObjectValue $SessionState 'prompt_id' '')
  if (-not [string]::IsNullOrWhiteSpace($recordPrompt) -and
      -not [string]::IsNullOrWhiteSpace($statePrompt) -and
      $recordPrompt -ne $statePrompt) {
    return 'stale'
  }
  return 'current'
}

function Commit-PendingRecord {
  param(
    [string]$Path,
    [object]$Record,
    [switch]$Promote
  )

  if ([string](Get-ObjectValue $Record 'provider' 'codex') -ne 'claude') {
    return Invoke-PendingRecordCommit -Path $Path -Record $Record -Promote:$Promote
  }
  $sessionInfo = Get-ClaudeSessionStateInfo -SessionId ([string](Get-ObjectValue $Record 'thread_id' ''))
  if ($null -eq $sessionInfo) {
    return Suppress-ClaudeSessionUnverifiablePendingRecord -Path $Path -Record $Record
  }
  return Invoke-WithClaudeSessionLock -Info $sessionInfo -Action {
    param($lockedInfo, $lockedPath, $lockedRecord, $shouldPromote)
    $sessionState = $null
    if (Test-Path -LiteralPath $lockedInfo.path -PathType Leaf) {
      try { $sessionState = Read-JsonFile -Path $lockedInfo.path } catch { $sessionState = $null }
    }
    $disposition = Get-ClaudeSessionCommitDisposition -Record $lockedRecord -SessionState $sessionState
    if ($disposition -eq 'unverifiable') {
      return Suppress-ClaudeSessionUnverifiablePendingRecord -Path $lockedPath -Record $lockedRecord
    }
    if ($disposition -eq 'stale') {
      $discard = Discard-PendingRecord -Path $lockedPath -Record $lockedRecord
      if ($discard.status -in @('discarded', 'missing')) {
        return [pscustomobject]@{ status = 'stale-session' }
      }
      return $discard
    }
    return Invoke-PendingRecordCommit -Path $lockedPath -Record $lockedRecord -Promote:$shouldPromote
  } -Arguments @($sessionInfo, $Path, $Record, [bool]$Promote)
}

function Suppress-ClaudeSessionUnverifiablePendingRecord {
  param(
    [string]$Path,
    [object]$Record
  )

  return Invoke-WithRecordMutationLock -Key ([string]$Record.key) -Action {
    param($lockedPath, $incomingRecord)
    $suppressedPath = Join-Path $SuppressedDir ([string]$incomingRecord.key + '.json')
    if (-not (Test-Path -LiteralPath $lockedPath)) {
      if (Test-Path -LiteralPath $suppressedPath -PathType Leaf) {
        try {
          $receipt = Read-JsonFile -Path $suppressedPath
          if ([string](Get-ObjectValue $receipt 'reason' '') -eq 'claude-session-unverifiable') {
            return [pscustomobject]@{ status = 'already-session-suppressed' }
          }
        } catch {}
      }
      return [pscustomobject]@{ status = 'missing' }
    }

    $canonical = Read-JsonFile -Path $lockedPath
    Assert-QueuedRecord -Record $canonical -ExpectedKey ([string]$incomingRecord.key)
    $sameClaudeTurn =
      [string](Get-ObjectValue $canonical 'provider' '') -eq 'claude' -and
      [string](Get-ObjectValue $canonical 'thread_id' '') -eq [string](Get-ObjectValue $incomingRecord 'thread_id' '') -and
      [string](Get-ObjectValue $canonical 'turn_id' '') -eq [string](Get-ObjectValue $incomingRecord 'turn_id' '')
    if (-not $sameClaudeTurn) {
      Set-RecordValue -Record $canonical -Name 'last_error' -Value 'Claude session identity changed during terminal suppression'
      Move-ToDeadLetter -Path $lockedPath -Record $canonical
      return [pscustomobject]@{ status = 'invalid-session-record' }
    }

    # Intentionally suppress the current canonical revision. A same-prompt Stop
    # refresh cannot repair a missing or corrupt session state while the caller
    # owns the session lock, and retrying it could incorrectly promote epoch 0.
    Move-ToSuppressedCore -Path $lockedPath -Record $canonical -Reason 'claude-session-unverifiable'
    return [pscustomobject]@{ status = 'session-unverifiable-suppressed' }
  } -Arguments @($Path, $Record)
}

function Discard-PendingRecord {
  param(
    [string]$Path,
    [object]$Record
  )
  return Invoke-WithRecordMutationLock -Key ([string]$Record.key) -Action {
    param($lockedPath, $incomingRecord)
    if (-not (Test-Path -LiteralPath $lockedPath)) {
      return [pscustomobject]@{ status = 'missing' }
    }
    $canonical = Read-JsonFile -Path $lockedPath
    Assert-QueuedRecord -Record $canonical -ExpectedKey $incomingRecord.key
    $canonicalRevision = [string](Get-ObjectValue $canonical 'candidate_revision' '')
    $incomingRevision = [string](Get-ObjectValue $incomingRecord 'candidate_revision' '')
    if (-not [string]::IsNullOrWhiteSpace($canonicalRevision) -and
        -not [string]::IsNullOrWhiteSpace($incomingRevision) -and
        $canonicalRevision -ne $incomingRevision) {
      return [pscustomobject]@{ status = 'stale-snapshot' }
    }
    Remove-Item -LiteralPath $lockedPath -Force -ErrorAction Stop
    return [pscustomobject]@{ status = 'discarded' }
  } -Arguments @($Path, $Record)
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
  # New completions must never sit behind hours-old unverifiable records.
  $files = @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue |
      Sort-Object @{ Expression = 'CreationTimeUtc'; Descending = $true }, Name)
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
      if ($gate.state -eq 'unverifiable') {
        Move-ToSuppressed -Path $file.FullName -Record $record -Reason 'unverifiable'
        Write-RuntimeLog "suppressed unverifiable candidate key=$($record.key.Substring(0, 12)) reason=$($gate.reason)"
        $promote = $false
        break
      }
      if ($gate.state -eq 'cancelled') {
        $discard = Discard-PendingRecord -Path $file.FullName -Record $record
        if ($discard.status -eq 'discarded') {
          Write-RuntimeLog "discarded cancelled Claude goal key=$($record.key.Substring(0, 12))"
        } elseif ($discard.status -eq 'stale-snapshot') {
          $nextDue = $now
        }
        $promote = $false
        break
      }
      if ($gate.state -ne 'ready') {
        Set-RecordValue -Record $record -Name 'next_attempt_unix_ms' -Value ([int64]$gate.retryAtUnixMs)
        Set-RecordValue -Record $record -Name 'gate_reason' -Value $gate.reason
        $commit = Commit-PendingRecord -Path $file.FullName -Record $record
        if ($commit.status -eq 'updated') {
          if ($null -eq $nextDue -or [int64]$gate.retryAtUnixMs -lt $nextDue) { $nextDue = [int64]$gate.retryAtUnixMs }
        } elseif ($commit.status -in @('stop-won', 'stale-snapshot')) {
          $nextDue = $now
        } elseif ($commit.status -eq 'stale-session') {
          Write-RuntimeLog "discarded stale Claude candidate after prompt epoch changed key=$($record.key.Substring(0, 12))"
        } elseif ($commit.status -in @('session-unverifiable-suppressed', 'already-session-suppressed')) {
          Write-RuntimeLog "suppressed Claude candidate with unverifiable session state key=$($record.key.Substring(0, 12))"
        }
        $promote = $false
        break
      }
    }
    if (-not $promote) { continue }

    Set-RecordValue -Record $record -Name 'next_attempt_unix_ms' -Value $now
    Set-RecordValue -Record $record -Name 'gate_reason' -Value $gate.reason
    $testPromoteDelayMs = 0
    if ([int]::TryParse([string]$env:CODEX_NTFY_TEST_BEFORE_PROMOTE_MS, [ref]$testPromoteDelayMs) -and $testPromoteDelayMs -gt 0) {
      $testMarker = [string]$env:CODEX_NTFY_TEST_BEFORE_PROMOTE_MARKER
      if (-not [string]::IsNullOrWhiteSpace($testMarker)) {
        [System.IO.File]::WriteAllText($testMarker, [string]$record.candidate_revision, $Utf8NoBom)
      }
      $testRelease = [string]$env:CODEX_NTFY_TEST_BEFORE_PROMOTE_RELEASE
      if (-not [string]::IsNullOrWhiteSpace($testRelease)) {
        $testDeadline = [DateTimeOffset]::UtcNow.AddMilliseconds([Math]::Min(10000, $testPromoteDelayMs))
        while (-not (Test-Path -LiteralPath $testRelease) -and [DateTimeOffset]::UtcNow -lt $testDeadline) {
          Start-Sleep -Milliseconds 25
        }
      } else {
        Start-Sleep -Milliseconds ([Math]::Min(10000, $testPromoteDelayMs))
      }
    }
    $commit = Commit-PendingRecord -Path $file.FullName -Record $record -Promote
    if ($commit.status -in @('promoted', 'already-promoted')) {
      Write-RuntimeLog "idle candidate promoted key=$($record.key.Substring(0, 12)) reason=$($gate.reason)"
    } elseif ($commit.status -in @('stop-won', 'stale-snapshot')) {
      if ($null -eq $nextDue -or $now -lt $nextDue) { $nextDue = $now }
    } elseif ($commit.status -in @('session-unverifiable-suppressed', 'already-session-suppressed')) {
      Write-RuntimeLog "suppressed Claude candidate with unverifiable session state key=$($record.key.Substring(0, 12))"
    } elseif ($commit.status -eq 'stale-session') {
      Write-RuntimeLog "discarded stale Claude candidate before promotion key=$($record.key.Substring(0, 12))"
    }
  }
  return [pscustomobject]@{ nextDueUnixMs = $nextDue }
}

function Write-RolloutScanHealth {
  param(
    [string]$Status,
    [string]$StartedAt,
    [string]$CompletedAt = '',
    [int64]$DurationMs = 0,
    [int]$Observed = 0,
    [string]$ErrorText = ''
  )

  try {
    $healthPath = if ($ScanScope -eq 'Remote') { $RemoteScanHealthPath } else { $ScanHealthPath }
    $previous = if (Test-Path -LiteralPath $healthPath) { Read-JsonFile -Path $healthPath } else { [pscustomobject]@{} }
    $lastCompletedAt = if (-not [string]::IsNullOrWhiteSpace($CompletedAt)) {
      $CompletedAt
    } else {
      [string](Get-ObjectValue $previous 'last_completed_at' (Get-ObjectValue $previous 'completed_at' ''))
    }
    $lastDurationMs = if (-not [string]::IsNullOrWhiteSpace($CompletedAt)) {
      $DurationMs
    } else {
      [int64](Get-ObjectValue $previous 'last_duration_ms' (Get-ObjectValue $previous 'duration_ms' 0))
    }
    Write-JsonAtomic -Path $healthPath -Value ([ordered]@{
        schema = 1
        scope = $ScanScope.ToLowerInvariant()
        status = $Status
        pid = $PID
        started_at = $StartedAt
        completed_at = $CompletedAt
        duration_ms = $DurationMs
        last_completed_at = $lastCompletedAt
        last_duration_ms = $lastDurationMs
        observed = $Observed
        error = $ErrorText
      })
  } catch {
    Write-RuntimeLog "rollout scan health error: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240)"
  }
}

function Test-RolloutScanParentAlive {
  if ($ScanParentPid -le 0 -and [string]::IsNullOrWhiteSpace($ScanParentToken)) {
    return $true
  }
  if ($ScanParentPid -le 0 -or $null -eq (Get-Process -Id $ScanParentPid -ErrorAction SilentlyContinue)) {
    return $false
  }
  try {
    $health = Read-JsonFile -Path $WorkerHealthPath
    return [int](Get-ObjectValue $health 'pid' 0) -eq $ScanParentPid -and
      [string](Get-ObjectValue $health 'token' '') -eq $ScanParentToken
  } catch {
    return $false
  }
}

function Invoke-RolloutScanWorker {
  Ensure-RuntimeDirectories
  $lockStream = $null
  $scanLockPath = if ($ScanScope -eq 'Remote') { $RemoteScanLockPath } else { $ScanLockPath }
  try {
    $lockStream = [System.IO.File]::Open($scanLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  } catch {
    return 0
  }

  try {
    while ($true) {
      if (-not (Test-RolloutScanParentAlive)) {
        return 0
      }
      $started = [DateTimeOffset]::Now
      $startedAt = $started.ToString('o')
      Write-RolloutScanHealth -Status 'running' -StartedAt $startedAt
      Write-RuntimeLog "rollout scan started scope=$($ScanScope.ToLowerInvariant()) pid=$PID"
      try {
        $testDelayMs = 0
        if ([int]::TryParse([string]$env:CODEX_NTFY_TEST_SCAN_DELAY_MS, [ref]$testDelayMs) -and $testDelayMs -gt 0) {
          Start-Sleep -Milliseconds $testDelayMs
        }
        $config = Get-Config
        $observed = Invoke-RolloutWatchScan -Config $config -NowUnixMs ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $completed = [DateTimeOffset]::Now
        if ($script:ColdDiscoveryRanThisScan) {
          $nextColdRefreshMs = $completed.ToUnixTimeMilliseconds() + [int64]([Math]::Max(0.1, $config.watchDiscoverySeconds) * 1000)
          $script:NextDurableCursorRefreshUnixMs = $nextColdRefreshMs
          foreach ($cacheKey in @($script:RolloutDiscoveryCache.Keys)) {
            Set-RecordValue -Record $script:RolloutDiscoveryCache[$cacheKey] -Name 'next_unix_ms' -Value $nextColdRefreshMs
          }
        }
        $durationMs = [int64][Math]::Max(0, ($completed - $started).TotalMilliseconds)
        Write-RolloutScanHealth -Status 'completed' -StartedAt $startedAt -CompletedAt $completed.ToString('o') -DurationMs $durationMs -Observed $observed
        Write-RuntimeLog "rollout scan completed scope=$($ScanScope.ToLowerInvariant()) observed=$observed duration_ms=$durationMs"
      } catch {
        $completed = [DateTimeOffset]::Now
        $durationMs = [int64][Math]::Max(0, ($completed - $started).TotalMilliseconds)
        $errorText = Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 300
        Write-RolloutScanHealth -Status 'failed' -StartedAt $startedAt -CompletedAt $completed.ToString('o') -DurationMs $durationMs -ErrorText $errorText
        Write-RuntimeLog "rollout watcher error scope=$($ScanScope.ToLowerInvariant()): $errorText"
        return 1
      }
      if (-not $Continuous) {
        return 0
      }
      $sleepUntil = [DateTimeOffset]::UtcNow.AddSeconds([Math]::Max(0.1, $config.watchScanSeconds))
      while ([DateTimeOffset]::UtcNow -lt $sleepUntil) {
        if (-not (Test-RolloutScanParentAlive)) {
          return 0
        }
        $remainingMs = [Math]::Max(1, ($sleepUntil - [DateTimeOffset]::UtcNow).TotalMilliseconds)
        Start-Sleep -Milliseconds ([int][Math]::Min(500, $remainingMs))
      }
    }
  } finally {
    if ($null -ne $lockStream) {
      $lockStream.Dispose()
    }
  }
}

function Start-RolloutScanProcess {
  param(
    [string]$ParentToken,
    [ValidateSet('Local', 'Remote')]
    [string]$Scope = 'Local',
    [switch]$OneShot
  )
  try {
    $powerShellExe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShellExe)) {
      $powerShellExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    }
    $scanMode = if ($OneShot -or $env:CODEX_NTFY_SCAN_ONCE -eq '1') { '' } else { ' -Continuous' }
    $arguments = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -ScanRollouts -ScanScope {1} -ScanParentPid {2} -ScanParentToken "{3}"{4}' -f $PSCommandPath, $Scope, $PID, $ParentToken, $scanMode)
    return Start-Process -FilePath $powerShellExe -ArgumentList $arguments -WindowStyle Hidden -PassThru
  } catch {
    Write-RuntimeLog "failed to start rollout scan: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 300)"
    return $null
  }
}

function Start-DeliveryProcess {
  param([string]$ParentToken)
  try {
    $powerShellExe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShellExe)) {
      $powerShellExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    }
    $arguments = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Worker -Continuous -DeliveryOnly -PollSeconds {1} -ScanParentPid {2} -ScanParentToken "{3}"' -f $PSCommandPath, $PollSeconds, $PID, $ParentToken)
    return Start-Process -FilePath $powerShellExe -ArgumentList $arguments -WindowStyle Hidden -PassThru
  } catch {
    Write-RuntimeLog "failed to start delivery worker: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 300)"
    return $null
  }
}

function Start-MaintenanceProcess {
  if ($NoSpawn -or $env:CODEX_NTFY_NO_SPAWN -eq '1') { return $null }
  try {
    $powerShellExe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShellExe)) {
      $powerShellExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    }
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Maintenance' -f $PSCommandPath
    return Start-Process -FilePath $powerShellExe -ArgumentList $arguments -WindowStyle Hidden -PassThru
  } catch {
    Write-RuntimeLog "failed to start maintenance: $(Sanitize-NotificationText -Text $_.Exception.Message -MaxLength 240)"
    return $null
  }
}

function Invoke-OutboxWorker {
  Ensure-RuntimeDirectories
  $lockStream = $null
  $lockPath = if ($DeliveryOnly) { $DeliveryLockPath } else { $WorkerLockPath }
  try {
    $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  } catch {
    return 0
  }

  $workerToken = if ($DeliveryOnly) { $ScanParentToken } else { [Guid]::NewGuid().ToString('N') }
  $nextWatchScanMs = [int64]0
  $nextRemoteWatchScanMs = [int64]0
  $scanProcess = $null
  $remoteScanProcess = $null
  $remoteScanStartedUnixMs = [int64]0
  $deliveryProcess = $null
  $maintenanceProcess = $null
  $maintenanceStarted = $false
  $deliveryReadyForMaintenance = $false
  $localScanReadyForMaintenance = $false
  $remoteScanReadyForMaintenance = $false
  $maintenanceDueUnixMs = [DateTimeOffset]::UtcNow.AddSeconds(60).ToUnixTimeMilliseconds()
  try {
    if (-not $DeliveryOnly) {
      Write-JsonAtomic -Path $WorkerHealthPath -Value ([ordered]@{
          schema = 1
          pid = $PID
          token = $workerToken
          started_at = [DateTimeOffset]::Now.ToString('o')
        })
    } else {
      Write-JsonAtomic -Path $DeliveryHealthPath -Value ([ordered]@{
          schema = 1
          pid = $PID
          parent_pid = $ScanParentPid
          token = $workerToken
          started_at = [DateTimeOffset]::Now.ToString('o')
        })
    }
    if ($Continuous -and -not $DeliveryOnly) {
      $deliveryProcess = Start-DeliveryProcess -ParentToken $workerToken
    }
    Write-RuntimeLog "worker started continuous=$([bool]$Continuous) delivery_only=$([bool]$DeliveryOnly)"
    while ($true) {
      if ($DeliveryOnly -and -not (Test-RolloutScanParentAlive)) {
        return 0
      }
      $config = Get-Config
      $hasRemoteWatchRoots = @($config.watchRoots | Where-Object {
          ([string](Get-ObjectValue $_ 'session_codex_home' (Get-ObjectValue $_ 'path' ''))).StartsWith('\\')
        }).Count -gt 0
      $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
      if (-not $DeliveryOnly -and $Continuous -and $null -ne $deliveryProcess -and $deliveryProcess.HasExited) {
        Write-RuntimeLog "delivery worker exited code=$($deliveryProcess.ExitCode)"
        $deliveryProcess.Dispose()
        $deliveryProcess = Start-DeliveryProcess -ParentToken $workerToken
      }
      if (-not $DeliveryOnly -and $Continuous -and $null -eq $deliveryProcess) {
        $deliveryProcess = Start-DeliveryProcess -ParentToken $workerToken
      }
      if (-not $config.watchRollouts -and $null -ne $scanProcess) {
        try {
          if (-not $scanProcess.HasExited) {
            Stop-Process -Id $scanProcess.Id -Force -ErrorAction SilentlyContinue
          }
          $scanProcess.Dispose()
        } catch {
        }
        $scanProcess = $null
      }
      if ((-not $config.watchRollouts -or -not $hasRemoteWatchRoots) -and $null -ne $remoteScanProcess) {
        try {
          if (-not $remoteScanProcess.HasExited) {
            Stop-Process -Id $remoteScanProcess.Id -Force -ErrorAction SilentlyContinue
          }
          $remoteScanProcess.Dispose()
        } catch {
        }
        $remoteScanProcess = $null
        $remoteScanStartedUnixMs = [int64]0
      }
      if ($null -ne $scanProcess -and $scanProcess.HasExited) {
        if ($scanProcess.ExitCode -ne 0) {
          Write-RuntimeLog "rollout scan process exited code=$($scanProcess.ExitCode)"
        }
        $scanProcess.Dispose()
        $scanProcess = $null
        $nextWatchScanMs = $nowMs + [int64]([Math]::Max(0.1, $config.watchScanSeconds) * 1000)
      }
      if ($null -ne $remoteScanProcess -and -not $remoteScanProcess.HasExited -and
          $remoteScanStartedUnixMs -gt 0 -and
          $nowMs -ge $remoteScanStartedUnixMs + [int64]([Math]::Max(5, $config.remoteWatchTimeoutSeconds) * 1000)) {
        Write-RuntimeLog "remote rollout scan timed out after $([Math]::Round([Math]::Max(5, $config.remoteWatchTimeoutSeconds), 1))s"
        Stop-Process -Id $remoteScanProcess.Id -Force -ErrorAction SilentlyContinue
        try {
          $remoteHealth = if (Test-Path -LiteralPath $RemoteScanHealthPath) { Read-JsonFile -Path $RemoteScanHealthPath } else { [pscustomobject]@{} }
          Write-JsonAtomic -Path $RemoteScanHealthPath -Value ([ordered]@{
              schema = 1
              scope = 'remote'
              status = 'timed-out'
              pid = $remoteScanProcess.Id
              started_at = [string](Get-ObjectValue $remoteHealth 'started_at' '')
              completed_at = [DateTimeOffset]::Now.ToString('o')
              duration_ms = [int64]([Math]::Max(5, $config.remoteWatchTimeoutSeconds) * 1000)
              observed = 0
              error = 'remote scan timeout'
            })
        } catch {
        }
        $remoteScanStartedUnixMs = [int64]0
      }
      if ($null -ne $remoteScanProcess -and $remoteScanProcess.HasExited) {
        if ($remoteScanProcess.ExitCode -ne 0) {
          Write-RuntimeLog "remote rollout scan process exited code=$($remoteScanProcess.ExitCode)"
        }
        $remoteScanProcess.Dispose()
        $remoteScanProcess = $null
        $remoteScanStartedUnixMs = [int64]0
        $nextRemoteWatchScanMs = $nowMs + [int64]([Math]::Max(5, $config.watchDiscoverySeconds) * 1000)
      }
      if (-not $DeliveryOnly -and $Continuous -and $config.watchRollouts -and $null -eq $scanProcess -and $nowMs -ge $nextWatchScanMs) {
        $scanProcess = Start-RolloutScanProcess -ParentToken $workerToken -Scope Local
        if ($null -eq $scanProcess) {
          $nextWatchScanMs = $nowMs + [int64]([Math]::Max(0.1, $config.watchScanSeconds) * 1000)
        }
      }
      if (-not $DeliveryOnly -and $Continuous -and $config.watchRollouts -and $hasRemoteWatchRoots -and
          $null -eq $remoteScanProcess -and $nowMs -ge $nextRemoteWatchScanMs) {
        $remoteScanProcess = Start-RolloutScanProcess -ParentToken $workerToken -Scope Remote -OneShot
        if ($null -eq $remoteScanProcess) {
          $nextRemoteWatchScanMs = $nowMs + [int64]([Math]::Max(5, $config.watchDiscoverySeconds) * 1000)
        } else {
          $remoteScanStartedUnixMs = $nowMs
        }
      }
      if (-not $DeliveryOnly -and $Continuous -and -not $deliveryReadyForMaintenance) {
        try {
          $deliveryHealth = Read-JsonFile -Path $DeliveryHealthPath
          $deliveryReadyForMaintenance = [int](Get-ObjectValue $deliveryHealth 'pid' 0) -gt 0 -and
            [string](Get-ObjectValue $deliveryHealth 'token' '') -eq $workerToken
        } catch {
        }
      }
      if (-not $DeliveryOnly -and -not $localScanReadyForMaintenance) {
        if (-not $config.watchRollouts) {
          $localScanReadyForMaintenance = $true
        } else {
          try {
            $localHealth = Read-JsonFile -Path $ScanHealthPath
            $localScanReadyForMaintenance = -not [string]::IsNullOrWhiteSpace([string](Get-ObjectValue $localHealth 'last_completed_at' '')) -or
              [string](Get-ObjectValue $localHealth 'status' '') -eq 'error'
          } catch {
          }
        }
      }
      if (-not $DeliveryOnly -and -not $remoteScanReadyForMaintenance) {
        if (-not $config.watchRollouts -or -not $hasRemoteWatchRoots) {
          $remoteScanReadyForMaintenance = $true
        } else {
          try {
            $remoteHealth = Read-JsonFile -Path $RemoteScanHealthPath
            $remoteStatus = [string](Get-ObjectValue $remoteHealth 'status' '')
            $remoteScanReadyForMaintenance = -not [string]::IsNullOrWhiteSpace([string](Get-ObjectValue $remoteHealth 'last_completed_at' '')) -or
              $remoteStatus -in @('completed', 'timed-out', 'error')
          } catch {
          }
        }
      }
      if (-not $DeliveryOnly -and $Continuous -and -not $maintenanceStarted -and $nowMs -ge $maintenanceDueUnixMs -and
          $deliveryReadyForMaintenance -and $localScanReadyForMaintenance -and $remoteScanReadyForMaintenance) {
        $maintenanceProcess = Start-MaintenanceProcess
        $maintenanceStarted = $true
      }
      if ($null -ne $maintenanceProcess -and $maintenanceProcess.HasExited) {
        if ($maintenanceProcess.ExitCode -ne 0) {
          Write-RuntimeLog "maintenance process exited code=$($maintenanceProcess.ExitCode)"
        }
        $maintenanceProcess.Dispose()
        $maintenanceProcess = $null
      }
      if (-not $DeliveryOnly) {
        Coalesce-ThreadCandidates -Config $config
        $pendingResult = Process-PendingCandidates -Config $config
      } else {
        $pendingResult = [pscustomobject]@{ nextDueUnixMs = $null }
      }
      $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
      $files = if ($DeliveryOnly -or -not $Continuous) {
        @(Get-ChildItem -LiteralPath $OutboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object CreationTimeUtc, Name)
      } else {
        @()
      }
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
        if (-not $DeliveryOnly) {
          if ($NoSpawn -or $env:CODEX_NTFY_NO_SPAWN -eq '1') {
            Clean-RuntimeState -ReceiptRetentionDays $config.sentRetentionDays -DeadRetentionDays $config.deadRetentionDays
          } else {
            Start-MaintenanceProcess | Out-Null
          }
        }
        return 0
      }
      $sleepMs = [Math]::Max(100, $PollSeconds * 1000)
      if ($null -ne $nextDueMs) {
        $untilDue = [Math]::Max(100, [int64]$nextDueMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $sleepMs = [Math]::Min($sleepMs, $untilDue)
      }
      if (-not $DeliveryOnly -and $Continuous -and $config.watchRollouts -and $null -eq $scanProcess) {
        $untilWatch = [Math]::Max(100, $nextWatchScanMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $sleepMs = [Math]::Min($sleepMs, $untilWatch)
      }
      if (-not $DeliveryOnly -and $Continuous -and $config.watchRollouts -and $hasRemoteWatchRoots -and $null -eq $remoteScanProcess) {
        $untilRemoteWatch = [Math]::Max(100, $nextRemoteWatchScanMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $sleepMs = [Math]::Min($sleepMs, $untilRemoteWatch)
      }
      Start-Sleep -Milliseconds ([int]$sleepMs)
    }
  } finally {
    if ($null -ne $deliveryProcess) {
      try {
        if (-not $deliveryProcess.HasExited) {
          Stop-Process -Id $deliveryProcess.Id -Force -ErrorAction SilentlyContinue
        }
        $deliveryProcess.Dispose()
      } catch {
      }
    }
    if ($null -ne $scanProcess) {
      try {
        if (-not $scanProcess.HasExited) {
          Stop-Process -Id $scanProcess.Id -Force -ErrorAction SilentlyContinue
        }
        $scanProcess.Dispose()
      } catch {
      }
    }
    if ($null -ne $remoteScanProcess) {
      try {
        if (-not $remoteScanProcess.HasExited) {
          Stop-Process -Id $remoteScanProcess.Id -Force -ErrorAction SilentlyContinue
        }
        $remoteScanProcess.Dispose()
      } catch {
      }
    }
    if ($null -ne $maintenanceProcess) {
      try {
        if (-not $maintenanceProcess.HasExited) {
          Stop-Process -Id $maintenanceProcess.Id -Force -ErrorAction SilentlyContinue
        }
        $maintenanceProcess.Dispose()
      } catch {
      }
    }
    if ($null -ne $lockStream) {
      $lockStream.Dispose()
    }
    if (-not $DeliveryOnly) {
      try {
        $health = Read-JsonFile -Path $WorkerHealthPath
        if ([string](Get-ObjectValue $health 'token' '') -eq $workerToken) {
          Remove-Item -LiteralPath $WorkerHealthPath -Force -ErrorAction SilentlyContinue
        }
      } catch {
      }
    } else {
      try {
        $health = Read-JsonFile -Path $DeliveryHealthPath
        if ([string](Get-ObjectValue $health 'token' '') -eq $workerToken) {
          Remove-Item -LiteralPath $DeliveryHealthPath -Force -ErrorAction SilentlyContinue
        }
      } catch {
      }
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
  $outboxFiles = @(Get-ChildItem -LiteralPath $OutboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue)
  $pendingFiles = @(Get-ChildItem -LiteralPath $PendingDir -Filter '*.json' -File -ErrorAction SilentlyContinue)
  $oldestOutboxSeconds = 0
  if ($outboxFiles.Count -gt 0) {
    $oldestOutbox = $outboxFiles | Sort-Object LastWriteTimeUtc | Select-Object -First 1
    $oldestOutboxSeconds = [Math]::Round([Math]::Max(0, ([DateTime]::UtcNow - $oldestOutbox.LastWriteTimeUtc).TotalSeconds), 1)
  }
  $oldestPendingSeconds = 0
  $pendingReasons = [ordered]@{}
  if ($pendingFiles.Count -gt 0) {
    $oldestPending = $pendingFiles | Sort-Object CreationTimeUtc | Select-Object -First 1
    $oldestPendingSeconds = [Math]::Round([Math]::Max(0, ([DateTime]::UtcNow - $oldestPending.CreationTimeUtc).TotalSeconds), 1)
    foreach ($file in $pendingFiles) {
      try {
        $pendingRecord = Read-JsonFile -Path $file.FullName
        $reason = [string](Get-ObjectValue $pendingRecord 'gate_reason' 'new')
        if ([string]::IsNullOrWhiteSpace($reason)) { $reason = 'new' }
        if (-not $pendingReasons.Contains($reason)) { $pendingReasons[$reason] = 0 }
        $pendingReasons[$reason] = [int]$pendingReasons[$reason] + 1
      } catch {
        if (-not $pendingReasons.Contains('invalid')) { $pendingReasons['invalid'] = 0 }
        $pendingReasons['invalid'] = [int]$pendingReasons['invalid'] + 1
      }
    }
  }
  $scanHealth = [pscustomobject]@{}
  if (Test-Path -LiteralPath $ScanHealthPath) {
    try {
      $scanHealth = Read-JsonFile -Path $ScanHealthPath
    } catch {
    }
  }
  $remoteScanHealth = [pscustomobject]@{}
  if (Test-Path -LiteralPath $RemoteScanHealthPath) {
    try {
      $remoteScanHealth = Read-JsonFile -Path $RemoteScanHealthPath
    } catch {
    }
  }
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
    idle_probe_grace_seconds = $config.idleProbeGraceSeconds
    unknown_retry_max_seconds = $config.unknownRetryMaxSeconds
    goal_aware = $config.goalAware
    watch_rollouts = $config.watchRollouts
    watch_discovery_seconds = $config.watchDiscoverySeconds
    watch_cursor_batch_size = $config.watchCursorBatchSize
    watch_remote_timeout_seconds = $config.remoteWatchTimeoutSeconds
    watch_roots = @($config.watchRoots).Count
    sqlite_home_configured = [bool]$config.workerSqliteConfigured
    pending_idle = $pendingFiles.Count
    pending = $pendingFiles.Count
    oldest_pending_seconds = $oldestPendingSeconds
    pending_reasons = $pendingReasons
    watched_rollouts = @(Get-ChildItem -LiteralPath $WatchDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
    queued = $outboxFiles.Count
    oldest_queued_seconds = $oldestOutboxSeconds
    watch_scan_status = [string](Get-ObjectValue $scanHealth 'status' 'unknown')
    watch_scan_started_at = [string](Get-ObjectValue $scanHealth 'started_at' '')
    watch_scan_completed_at = [string](Get-ObjectValue $scanHealth 'completed_at' '')
    watch_scan_duration_ms = [int64](Get-ObjectValue $scanHealth 'duration_ms' 0)
    watch_scan_observed = [int](Get-ObjectValue $scanHealth 'observed' 0)
    remote_watch_scan_status = [string](Get-ObjectValue $remoteScanHealth 'status' 'unknown')
    remote_watch_scan_started_at = [string](Get-ObjectValue $remoteScanHealth 'started_at' '')
    remote_watch_scan_completed_at = [string](Get-ObjectValue $remoteScanHealth 'completed_at' '')
    remote_watch_scan_duration_ms = [int64](Get-ObjectValue $remoteScanHealth 'duration_ms' 0)
    remote_watch_scan_observed = [int](Get-ObjectValue $remoteScanHealth 'observed' 0)
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
  if ($CleanupTestState) {
    Write-Output (Clear-SyntheticTestState)
    exit 0
  }
  if ($Maintenance) {
    exit (Invoke-RuntimeMaintenance)
  }
  if ($ScanRollouts) {
    exit (Invoke-RolloutScanWorker)
  }
  if ($Worker -or $Continuous) {
    exit (Invoke-OutboxWorker)
  }

  $raw = if ($Test) {
    ConvertTo-CompactJson ([ordered]@{
      type = 'agent-turn-complete'
      'thread-id' = $SyntheticTestThreadId
      'turn-id' = [Guid]::NewGuid().ToString()
      cwd = (Get-Location).Path
      'last-assistant-message' = 'Test codex-ntfy-notifier: delivery, queueing, and deduplication work.'
    })
  } else {
    Get-RawNotification
  }
  $event = ConvertTo-NotificationEvent -Raw $raw
  if ($null -eq $event) {
    if ($HookEvent -or $ClaudeHook) { Write-Output '{}' }
    exit 0
  }
  $candidateKind = 'legacy'
  $provider = 'codex'
  $sourceEvent = 'agent-turn-complete'
  if ($ClaudeHook) {
    $hookInput = $event
    $hookName = [string](Get-FirstObjectValue $hookInput @('hook_event_name', 'hook-event-name', 'hookEventName', 'event_name', 'eventName', 'type'))
    $sessionId = [string](Get-FirstObjectValue $hookInput @('session_id', 'session-id', 'sessionId'))
    $promptId = [string](Get-FirstObjectValue $hookInput @('prompt_id', 'prompt-id', 'promptId'))
    $agentId = [string](Get-FirstObjectValue $hookInput @('agent_id', 'agent-id', 'agentId'))
    $transcriptPath = [string](Get-FirstObjectValue $hookInput @('transcript_path', 'transcript-path', 'transcriptPath'))
    if ($hookName -eq 'UserPromptSubmit') {
      if (-not [string]::IsNullOrWhiteSpace($sessionId)) {
        [void](Set-ClaudeSessionBusy -SessionId $sessionId -PromptId $promptId -TranscriptPath $transcriptPath)
      } else {
        Write-RuntimeLog 'ignored Claude UserPromptSubmit without session_id'
      }
      Write-Output '{}'
      exit 0
    }
    if ($hookName -eq 'Notification') {
      $notificationType = [string](Get-FirstObjectValue $hookInput @('notification_type', 'notification-type', 'notificationType'))
      if ($notificationType -in @('idle_prompt', 'agent_completed') -and
          -not [string]::IsNullOrWhiteSpace($sessionId) -and
          -not [string]::IsNullOrWhiteSpace($promptId)) {
        [void](Set-ClaudeSessionIdle -SessionId $sessionId -PromptId $promptId -TranscriptPath $transcriptPath -NotificationType $notificationType)
        Start-DetachedWorker
      } else {
        Write-RuntimeLog "ignored unsupported or uncorrelated Claude notification type=$(Sanitize-NotificationText -Text $notificationType -MaxLength 80)"
      }
      Write-Output '{}'
      exit 0
    }
    if ($hookName -eq 'SubagentStop' -or -not [string]::IsNullOrWhiteSpace($agentId)) {
      Write-RuntimeLog 'ignored Claude subagent completion'
      Write-Output '{}'
      exit 0
    }
    if ($hookName -notin @('Stop', 'StopFailure')) {
      Write-RuntimeLog "ignored unsupported Claude hook event type=$(Sanitize-NotificationText -Text $hookName -MaxLength 80)"
      Write-Output '{}'
      exit 0
    }
    if ([string]::IsNullOrWhiteSpace($sessionId)) {
      Write-RuntimeLog 'ignored unverifiable Claude completion without session_id'
      Write-Output '{}'
      exit 0
    }
    $sessionState = Read-ClaudeSessionState -SessionId $sessionId
    if ([string]::IsNullOrWhiteSpace($promptId)) {
      # Async hooks without prompt identity cannot be correlated safely after a
      # follow-up prompt. Fail closed instead of manufacturing a weak key.
      Write-RuntimeLog 'ignored unverifiable Claude completion without prompt_id'
      Write-Output '{}'
      exit 0
    }
    $activePromptId = [string](Get-ObjectValue $sessionState 'prompt_id' '')
    if (-not [string]::IsNullOrWhiteSpace($activePromptId) -and $activePromptId -ne $promptId) {
      Write-RuntimeLog 'ignored stale Claude completion for a superseded prompt'
      Write-Output '{}'
      exit 0
    }
    if ($hookName -eq 'Stop') {
      $backgroundProperty = $hookInput.PSObject.Properties['background_tasks']
      $cronsProperty = $hookInput.PSObject.Properties['session_crons']
      if ($null -eq $backgroundProperty -or $null -eq $cronsProperty -or
          $null -eq $backgroundProperty.Value -or $null -eq $cronsProperty.Value -or
          $backgroundProperty.Value -isnot [System.Array] -or
          $cronsProperty.Value -isnot [System.Array]) {
        Write-RuntimeLog 'ignored unverifiable Claude Stop without work registries'
        Write-Output '{}'
        exit 0
      }
      if (@($backgroundProperty.Value).Count -gt 0 -or @($cronsProperty.Value).Count -gt 0) {
        Remove-ClaudePendingCandidate -SessionId $sessionId -PromptId $promptId
        Write-RuntimeLog 'ignored Claude Stop while background work remains active'
        Write-Output '{}'
        exit 0
      }
    }
    $sessionEpoch = [int64](Get-ObjectValue $sessionState 'epoch' 0)
    $goalInfo = if ($hookName -eq 'Stop') {
      # Stop is synchronous so repeated lifecycle events retain host ordering.
      # Keep that ordered path bounded; the detached worker performs the full
      # reverse scan whenever this limited read returns unknown.
      Get-ClaudeGoalTranscriptState -TranscriptPath $transcriptPath -MaxBytes $ClaudePromptBaselineMaxBytes
    } else {
      [pscustomobject]@{ state = 'none'; marker = ''; marker_unix_ms = [int64]0 }
    }
    $goalState = [string]$goalInfo.state
    $goalMarker = [string]$goalInfo.marker
    $goalMarkerUnixMs = [int64](Get-ObjectValue $goalInfo 'marker_unix_ms' 0)
    $baselineCaptured = [bool](Get-ObjectValue $sessionState 'goal_baseline_captured' $false)
    $baselineMarker = [string](Get-ObjectValue $sessionState 'goal_baseline_marker' '')
    $sessionBusyUnixMs = [int64](Get-ObjectValue $sessionState 'busy_unix_ms' 0)
    # Terminal markers are session-level and remain forever. They belong to the
    # current prompt when they differ from a captured baseline. If the bounded
    # prompt-start scan could not capture a baseline, only a marker timestamped
    # before prompt start is safely historical; an ambiguous clear fails closed.
    $terminalMarkerIsHistorical = $baselineCaptured -and $goalMarker -eq $baselineMarker
    if (-not $baselineCaptured -and $goalMarkerUnixMs -gt 0 -and
        $sessionBusyUnixMs -gt 0 -and $goalMarkerUnixMs -lt $sessionBusyUnixMs) {
      $terminalMarkerIsHistorical = $true
    }
    # An active marker is always fail-closed so resumed /goal loops stay safe.
    if ($goalState -in @('achieved', 'failed', 'cleared') -and $terminalMarkerIsHistorical) {
      $goalState = 'none'
      $goalMarker = ''
    }
    $lastMessage = [string](Get-FirstObjectValue $hookInput @('last_assistant_message', 'last-assistant-message', 'lastAgentMessage', 'last_agent_message', 'message'))
    if ($hookName -eq 'StopFailure' -and [string]::IsNullOrWhiteSpace($lastMessage)) {
      $errorName = Sanitize-NotificationText -Text ([string](Get-ObjectValue $hookInput 'error' 'unknown')) -MaxLength 80
      $lastMessage = "Claude API error: $errorName"
    }
    $event = [pscustomobject][ordered]@{
      type = 'agent-turn-complete'
      'thread-id' = $sessionId
      'turn-id' = $promptId
      cwd = [string](Get-FirstObjectValue $hookInput @('cwd', 'working-directory', 'working_directory'))
      'last-assistant-message' = $lastMessage
      transcript_path = $transcriptPath
      'claude-session-epoch' = $sessionEpoch
      'claude-goal-state' = $goalState
      'claude-goal-marker' = $goalMarker
      'goal-status' = if ($goalState -eq 'failed') { 'blocked' } elseif ($goalState -eq 'achieved') { 'complete' } else { '' }
      'completion-event-type' = if ($hookName -eq 'StopFailure') { 'turn_aborted' } else { 'task_complete' }
    }
    $provider = 'claude'
    $candidateKind = if ($hookName -eq 'StopFailure') { 'claude_stop_failure' } else { 'claude_stop' }
    $sourceEvent = $hookName
  } elseif ($HookEvent) {
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
    $sourceEvent = 'Stop'
  }
  $eventType = [string](Get-ObjectValue $event 'type' 'agent-turn-complete')
  if ($eventType -ne 'agent-turn-complete') {
    if ($HookEvent -or $ClaudeHook) { Write-Output '{}' }
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
  $detectedClassification = if ($Test -or $provider -eq 'claude') {
    'root'
  } else {
    Get-EventClassification -Event $event -ThreadId $threadId -SessionHome $eventSessionHome
  }
  $eventClassification = if ($detectedClassification -eq 'subagent') {
    'subagent'
  } elseif ($SessionClassification -in @('root', 'subagent')) {
    $SessionClassification
  } else {
    $detectedClassification
  }
  $record = New-EventRecord -Event $event -EventOrigin (Get-DefaultOrigin) -EventSessionHome $eventSessionHome -EventSqliteHome $eventSqliteHome -EventClassification $eventClassification -EventIncludeMessage $config.includeMessage -CandidateKind $candidateKind -SourceEvent $sourceEvent -Provider $provider
  if ($config.suppressSubagents -and $eventClassification -eq 'subagent') {
    Move-ToSuppressed -Path (Join-Path $OutboxDir ($record.key + '.json')) -Record $record
    Write-RuntimeLog "suppressed subagent completion thread=$($threadId.Substring(0, [Math]::Min(8, $threadId.Length)))"
    if ($HookEvent -or $ClaudeHook) { Write-Output '{}' }
    exit 0
  }

  $queued = if ($Test) { Add-OutboxEvent -Record $record } else { Add-CandidateEvent -Record $record -Config $config }
  Start-DetachedWorker

  if ($HookEvent -or $ClaudeHook) {
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
  if ($HookEvent -or $ClaudeHook) {
    if ($BridgeFallback) { exit 1 }
    Write-Output '{}'
    exit 0
  }
  exit 1
}
