[CmdletBinding()]
param(
  [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
  [string[]]$NotificationArgs,
  [string]$Origin,
  [string]$SessionCodexHome,
  [ValidateSet('', 'root', 'subagent', 'unknown')]
  [string]$SessionClassification = '',
  [switch]$ReadStdin,
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

$ScriptVersion = '2.3.0'
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
$OutboxDir = Join-Path $StateRoot 'outbox'
$SentDir = Join-Path $StateRoot 'sent'
$SuppressedDir = Join-Path $StateRoot 'suppressed'
$DeadDir = Join-Path $StateRoot 'dead'
$WorkerLockPath = Join-Path $StateRoot 'worker.lock'
$LogPath = Join-Path $StateRoot 'notify.log'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Ensure-RuntimeDirectories {
  foreach ($path in @($StateRoot, $OutboxDir, $SentDir, $SuppressedDir, $DeadDir)) {
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

  return [pscustomobject]@{
    server = $server.TrimEnd('/')
    topic = $topic.Trim('/')
    token = $token
    username = $user
    password = $password
    allowInsecureAuth = [bool](Get-ObjectValue $fileConfig 'allow_insecure_auth' $false)
    priority = [int](Get-ObjectValue $fileConfig 'priority' 3)
    tags = @(Get-ObjectValue $fileConfig 'tags' @('computer', 'white_check_mark'))
    maxMessageChars = [int](Get-ObjectValue $fileConfig 'max_message_chars' 900)
    includeMessage = [bool](Get-ObjectValue $fileConfig 'include_message' $false)
    includeThreadTitle = [bool](Get-ObjectValue $fileConfig 'include_thread_title' $false)
    markdown = [bool](Get-ObjectValue $fileConfig 'markdown' $true)
    includeFullPath = [bool](Get-ObjectValue $fileConfig 'include_full_path' $false)
    suppressSubagents = [bool](Get-ObjectValue $fileConfig 'suppress_subagents' $true)
    subagentClassificationGraceSeconds = [double](Get-ObjectValue $fileConfig 'subagent_classification_grace_seconds' 8)
    timeoutSeconds = [int](Get-ObjectValue $fileConfig 'timeout_seconds' 12)
    maxAttempts = [int](Get-ObjectValue $fileConfig 'max_attempts' 0)
    retryMaxSeconds = [double](Get-ObjectValue $fileConfig 'retry_max_seconds' 900)
    sentRetentionDays = [int](Get-ObjectValue $fileConfig 'sent_retention_days' 14)
    deadRetentionDays = [int](Get-ObjectValue $fileConfig 'dead_retention_days' 30)
  }
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
  if ($value.Length -gt $MaxLength) {
    return $value.Substring(0, [Math]::Max(0, $MaxLength - 3)) + '...'
  }
  return $value
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
    [string]$SessionHome = $CodexHome
  )

  if ([string]::IsNullOrWhiteSpace($ThreadId)) {
    return $null
  }
  $indexPath = Join-Path $SessionHome 'session_index.jsonl'
  if (-not (Test-Path -LiteralPath $indexPath)) {
    return $null
  }
  try {
    $match = Select-String -LiteralPath $indexPath -SimpleMatch -Pattern $ThreadId | Select-Object -Last 1
    if ($null -eq $match) {
      return $null
    }
    $item = $match.Line | ConvertFrom-Json
    return [string](Get-ObjectValue $item 'thread_name' $null)
  } catch {
    return $null
  }
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
  if ($ReadStdin) {
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
    [string]$EventClassification,
    [bool]$EventIncludeMessage
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
    origin = $EventOrigin
    session_codex_home = $EventSessionHome
    session_classification = $EventClassification
    created_at = $now.ToString('o')
    created_unix_ms = $now.ToUnixTimeMilliseconds()
    next_attempt_unix_ms = $now.ToUnixTimeMilliseconds()
    attempts = 0
    last_error = $null
    event = $storedEvent
  }
}

function Add-OutboxEvent {
  param([object]$Record)

  Ensure-RuntimeDirectories
  $sentPath = Join-Path $SentDir ($Record.key + '.json')
  $suppressedPath = Join-Path $SuppressedDir ($Record.key + '.json')
  $deadPath = Join-Path $DeadDir ($Record.key + '.json')
  $outboxPath = Join-Path $OutboxDir ($Record.key + '.json')
  if (Test-Path -LiteralPath $sentPath) {
    Write-RuntimeLog "deduplicated sent event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'sent' }
  }
  if (Test-Path -LiteralPath $suppressedPath) {
    Write-RuntimeLog "deduplicated suppressed event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'suppressed' }
  }
  if (Test-Path -LiteralPath $deadPath) {
    Write-RuntimeLog "deduplicated dead event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'dead' }
  }
  if (Test-Path -LiteralPath $outboxPath) {
    Write-RuntimeLog "deduplicated queued event key=$($Record.key.Substring(0, 12))"
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'queued' }
  }
  try {
    Write-JsonAtomic -Path $outboxPath -Value $Record -NoOverwrite
    Write-RuntimeLog "queued event key=$($Record.key.Substring(0, 12)) origin=$($Record.origin)"
    return [pscustomobject]@{ queued = $true; key = $Record.key; status = 'queued' }
  } catch [System.IO.IOException] {
    return [pscustomobject]@{ queued = $false; key = $Record.key; status = 'queued' }
  }
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

function New-NtfyPayload {
  param(
    [object]$Record,
    [object]$Config
  )

  $event = $Record.event
  $cwd = [string](Get-FirstObjectValue $event @('cwd', 'working-directory', 'working_directory'))
  $project = Sanitize-NotificationText -Text (Get-ProjectName $cwd) -MaxLength 50
  $sessionHome = [string](Get-ObjectValue $Record 'session_codex_home' $CodexHome)
  if ([string]::IsNullOrWhiteSpace($sessionHome)) {
    $sessionHome = $CodexHome
  }
  $displayName = $project
  if ($Config.includeThreadTitle) {
    $threadTitle = Sanitize-NotificationText -Text (Get-ThreadTitle -ThreadId $Record.thread_id -SessionHome $sessionHome) -MaxLength 58
    if (-not [string]::IsNullOrWhiteSpace($threadTitle)) { $displayName = $threadTitle }
  }
  $lastMessage = [string](Get-FirstObjectValue $event @('last-assistant-message', 'last_assistant_message'))
  $lastMessage = Sanitize-NotificationText -Text $lastMessage -MaxLength $Config.maxMessageChars -PreserveLines
  if ([string]::IsNullOrWhiteSpace($lastMessage)) {
    $lastMessage = 'Turn completed.'
  }

  $metadata = @()
  if ($Config.includeFullPath -and -not [string]::IsNullOrWhiteSpace($cwd)) {
    $metadata += 'Folder: ' + (Sanitize-NotificationText -Text $cwd -MaxLength 180)
  } else {
    $metadata += "Project: $project"
  }
  $metadata += "Source: $($Record.origin)"
  if (-not [string]::IsNullOrWhiteSpace($Record.thread_id)) {
    $metadata += 'Thread: ' + $Record.thread_id.Substring(0, [Math]::Min(8, $Record.thread_id.Length))
  }
  $body = $lastMessage + "`n`n" + ($metadata -join ' | ')

  return [ordered]@{
    topic = $Config.topic
    title = "Codex finished - $displayName"
    message = $body
    tags = @($Config.tags)
    priority = $Config.priority
    markdown = $Config.markdown
    sequence_id = $Record.sequence_id
  }
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
  $serverUri = [Uri]$Config.server
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

function Move-ToSuppressed {
  param(
    [string]$Path,
    [object]$Record
  )

  $suppressedPath = Join-Path $SuppressedDir ($Record.key + '.json')
  $receipt = [ordered]@{
    schema = 1
    key = $Record.key
    thread_id = $Record.thread_id
    turn_id = $Record.turn_id
    origin = $Record.origin
    suppressed_at = [DateTimeOffset]::UtcNow.ToString('o')
    reason = 'subagent'
  }
  Write-JsonAtomic -Path $suppressedPath -Value $receipt
  Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
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

function Invoke-OutboxWorker {
  Ensure-RuntimeDirectories
  $lockStream = $null
  try {
    $lockStream = [System.IO.File]::Open($WorkerLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  } catch {
    return 0
  }

  $cleaned = $false
  try {
    Write-RuntimeLog "worker started continuous=$([bool]$Continuous)"
    while ($true) {
      $config = Get-Config
      if (-not $cleaned) {
        Clean-RuntimeState -ReceiptRetentionDays $config.sentRetentionDays -DeadRetentionDays $config.deadRetentionDays
        $cleaned = $true
      }

      $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
      $files = @(Get-ChildItem -LiteralPath $OutboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object CreationTimeUtc, Name)
      $nextDueMs = $null

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

        $sentPath = Join-Path $SentDir ($record.key + '.json')
        $suppressedPath = Join-Path $SuppressedDir ($record.key + '.json')
        if (Test-Path -LiteralPath $sentPath) {
          Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
          continue
        }
        if (Test-Path -LiteralPath $suppressedPath) {
          Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
          continue
        }

        $dueMs = [int64](Get-ObjectValue $record 'next_attempt_unix_ms' 0)
        if ($dueMs -gt $nowMs) {
          if ($null -eq $nextDueMs -or $dueMs -lt $nextDueMs) {
            $nextDueMs = $dueMs
          }
          continue
        }

        if ($config.suppressSubagents) {
          $classification = [string](Get-ObjectValue $record 'session_classification' 'unknown')
          if ($classification -notin @('root', 'subagent')) {
            $sessionHome = [string](Get-ObjectValue $record 'session_codex_home' $CodexHome)
            if ([string]::IsNullOrWhiteSpace($sessionHome)) {
              $sessionHome = $CodexHome
            }
            $classification = Get-EventClassification -Event $record.event -ThreadId ([string]$record.thread_id) -SessionHome $sessionHome
          }
          if ($classification -eq 'subagent') {
            Move-ToSuppressed -Path $file.FullName -Record $record
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

      $remaining = @(Get-ChildItem -LiteralPath $OutboxDir -Filter '*.json' -File -ErrorAction SilentlyContinue).Count
      if (-not $Continuous -and $remaining -eq 0) {
        return 0
      }
      $sleepMs = [Math]::Max(100, $PollSeconds * 1000)
      if ($null -ne $nextDueMs) {
        $untilDue = [Math]::Max(100, [int64]$nextDueMs - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $sleepMs = [Math]::Min($sleepMs, $untilDue)
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

function Show-Doctor {
  Ensure-RuntimeDirectories
  $config = Get-Config
  $result = [ordered]@{
    version = $ScriptVersion
    codex_home = $CodexHome
    config_path = $ConfigPath
    config_exists = Test-Path -LiteralPath $ConfigPath
    server = $config.server
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
    exit 0
  }
  $eventType = [string](Get-ObjectValue $event 'type' 'agent-turn-complete')
  if ($eventType -ne 'agent-turn-complete') {
    exit 0
  }

  $threadId = [string](Get-FirstObjectValue $event @('thread-id', 'thread_id'))
  $config = Get-Config
  $eventSessionHome = if ([string]::IsNullOrWhiteSpace($SessionCodexHome)) { $CodexHome } else { $SessionCodexHome }
  $detectedClassification = Get-EventClassification -Event $event -ThreadId $threadId -SessionHome $eventSessionHome
  $eventClassification = if ($detectedClassification -eq 'subagent') {
    'subagent'
  } elseif ($SessionClassification -in @('root', 'subagent')) {
    $SessionClassification
  } else {
    $detectedClassification
  }
  $record = New-EventRecord -Event $event -EventOrigin (Get-DefaultOrigin) -EventSessionHome $eventSessionHome -EventClassification $eventClassification -EventIncludeMessage $config.includeMessage
  if ($config.suppressSubagents -and $eventClassification -eq 'subagent') {
    Move-ToSuppressed -Path (Join-Path $OutboxDir ($record.key + '.json')) -Record $record
    Write-RuntimeLog "suppressed subagent completion thread=$($threadId.Substring(0, [Math]::Min(8, $threadId.Length)))"
    exit 0
  }

  $queued = Add-OutboxEvent -Record $record
  Start-DetachedWorker

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
  exit 1
}
