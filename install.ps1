[CmdletBinding()]
param(
  [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex'),
  [string[]]$WslDistro = @('Ubuntu'),
  [switch]$NoWsl,
  [switch]$SkipScheduledTask,
  [switch]$EnableClaudeCode,
  [string]$ClaudeHome = (Join-Path $env:USERPROFILE '.claude'),
  [switch]$EnableAudnCode,
  [string]$AudnCodeHome = $(
    if ([string]::IsNullOrWhiteSpace($env:CLAUDE_CONFIG_DIR)) {
      Join-Path $env:USERPROFILE '.openclaude'
    } else {
      $env:CLAUDE_CONFIG_DIR
    }
  ),
  [ValidateRange(1000, 60000)]
  [int]$AudnCodeIdleThresholdMs = 1000
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$SourceRoot = Join-Path $PSScriptRoot 'src'
$TaskName = 'CodexNtfyWatcher'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object System.Text.UTF8Encoding($false, $true)
$NotifierVersion = '2.6.0'
$AudnCodeHookShapeVersion = 8
$AudnCodeHookObservationMarkerName = '.codex-ntfy-hooks.json'
$AudnCodeSettingsQuietWindowMilliseconds = 750
$AudnCodeSessionMarkerMaxBytes = 64 * 1024
$CodexHome = [IO.Path]::GetFullPath($CodexHome)
if ($EnableClaudeCode) {
  $ClaudeHome = [IO.Path]::GetFullPath($ClaudeHome)
}
if ($EnableAudnCode) {
  $AudnCodeHome = [IO.Path]::GetFullPath($AudnCodeHome)
}

function Write-Status {
  param([string]$Message)
  Write-Host "[codex-ntfy] $Message"
}

function Test-SafeUnicodeScalarText {
  param([AllowNull()][AllowEmptyString()][string]$Value)
  if ($null -eq $Value) { return $true }
  for ($index = 0; $index -lt $Value.Length; $index++) {
    $unit = [int][char]$Value[$index]
    if ($unit -eq 0xFFFD) { return $false }
    if ($unit -ge 0xD800 -and $unit -le 0xDBFF) {
      if ($index + 1 -ge $Value.Length) { return $false }
      $low = [int][char]$Value[$index + 1]
      if ($low -lt 0xDC00 -or $low -gt 0xDFFF) { return $false }
      $index++
    } elseif ($unit -ge 0xDC00 -and $unit -le 0xDFFF) { return $false }
  }
  return $true
}

function Assert-SafeUnicodeScalarText {
  param([AllowNull()][AllowEmptyString()][string]$Value, [string]$Context = 'text')
  if (-not (Test-SafeUnicodeScalarText -Value $Value)) { throw "$Context contains invalid Unicode scalar data" }
}

function Assert-JsonUnicodeScalars {
  param([AllowNull()][object]$Value, [int]$Depth = 0)
  if ($Depth -gt 64) { throw 'JSON nesting exceeds the supported depth' }
  if ($null -eq $Value -or $Value -is [ValueType]) { return }
  if ($Value -is [string]) { Assert-SafeUnicodeScalarText -Value ([string]$Value) -Context 'JSON string'; return }
  if ($Value -is [Collections.IDictionary]) {
    foreach ($key in $Value.Keys) {
      if ($key -is [string]) { Assert-SafeUnicodeScalarText -Value ([string]$key) -Context 'JSON object key' }
      Assert-JsonUnicodeScalars -Value $Value[$key] -Depth ($Depth + 1)
    }
    return
  }
  if ($Value -is [Collections.IEnumerable] -and $Value -isnot [string]) {
    foreach ($item in $Value) { Assert-JsonUnicodeScalars -Value $item -Depth ($Depth + 1) }
    return
  }
  foreach ($property in @($Value.PSObject.Properties | Where-Object { $_.MemberType -in @('NoteProperty', 'Property') })) {
    Assert-SafeUnicodeScalarText -Value ([string]$property.Name) -Context 'JSON object key'
    Assert-JsonUnicodeScalars -Value $property.Value -Depth ($Depth + 1)
  }
}

function ConvertFrom-StrictJsonText {
  param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
  Assert-SafeUnicodeScalarText -Value $Text -Context 'JSON input'
  $value = $Text | ConvertFrom-Json -ErrorAction Stop
  Assert-JsonUnicodeScalars -Value $value
  return $value
}

function Read-StrictUtf8Text {
  param([Parameter(Mandatory = $true)][string]$Path, [int64]$MaxBytes = 0)
  $sharing = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
  $stream = $null
  try {
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $sharing)
    if (($MaxBytes -gt 0 -and $stream.Length -gt $MaxBytes) -or $stream.Length -gt [int]::MaxValue) {
      throw "Managed text file exceeds the supported byte limit: $Path"
    }
    $bytes = New-Object byte[] ([int]$stream.Length)
    $read = 0
    while ($read -lt $bytes.Length) {
      $count = $stream.Read($bytes, $read, $bytes.Length - $read)
      if ($count -le 0) { throw "Managed text file ended unexpectedly: $Path" }
      $read += $count
    }
    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $offset = 3 }
    $text = $Utf8StrictNoBom.GetString($bytes, $offset, $bytes.Length - $offset)
    Assert-SafeUnicodeScalarText -Value $text -Context "Managed text file $Path"
    return $text
  } finally {
    if ($null -ne $stream) { $stream.Dispose() }
  }
}

function Read-StrictJsonFile {
  param([Parameter(Mandatory = $true)][string]$Path, [switch]$AllowEmpty)
  $text = Read-StrictUtf8Text -Path $Path
  if ($AllowEmpty -and [string]::IsNullOrWhiteSpace($text)) { return [pscustomobject]@{} }
  return ConvertFrom-StrictJsonText -Text $text
}

function Assert-ManagedInstallerInputs {
  param(
    [string]$HomePath,
    [string]$ClaudeSettingsPath = '',
    [string]$AudnSettingsPath = '',
    [string]$AudnGlobalPath = '',
    [string]$AudnMarkerPath = ''
  )

  foreach ($name in @('config.toml', 'notify-ntfy.ps1')) {
    $path = Join-Path $HomePath $name
    if (Test-Path -LiteralPath $path -PathType Leaf) { [void](Read-StrictUtf8Text -Path $path) }
  }
  foreach ($spec in @(
      @((Join-Path $HomePath 'ntfy-config.json'), $false),
      @((Join-Path $HomePath 'hooks.json'), $true),
      @($ClaudeSettingsPath, $true),
      @($AudnSettingsPath, $true),
      @($AudnGlobalPath, $true),
      @($AudnMarkerPath, $false)
    )) {
    $path = [string]$spec[0]
    if ([string]::IsNullOrWhiteSpace($path) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    [void](Read-StrictJsonFile -Path $path -AllowEmpty:([bool]$spec[1]))
  }
}

function Write-TextAtomic {
  param(
    [string]$Path,
    [string]$Content,
    [AllowNull()]
    [string]$ExpectedContent = $null
  )

  $validateScalar = {
    param([AllowNull()][AllowEmptyString()][string]$Value, [string]$Context)
    if ($null -eq $Value) { return }
    for ($scalarIndex = 0; $scalarIndex -lt $Value.Length; $scalarIndex++) {
      $unit = [int][char]$Value[$scalarIndex]
      if ($unit -eq 0xFFFD) { throw "$Context contains invalid Unicode scalar data" }
      if ($unit -ge 0xD800 -and $unit -le 0xDBFF) {
        if ($scalarIndex + 1 -ge $Value.Length) { throw "$Context contains invalid Unicode scalar data" }
        $low = [int][char]$Value[$scalarIndex + 1]
        if ($low -lt 0xDC00 -or $low -gt 0xDFFF) { throw "$Context contains invalid Unicode scalar data" }
        $scalarIndex++
      } elseif ($unit -ge 0xDC00 -and $unit -le 0xDFFF) {
        throw "$Context contains invalid Unicode scalar data"
      }
    }
  }
  & $validateScalar $Content 'Atomic text content'
  if ($PSBoundParameters.ContainsKey('ExpectedContent')) {
    & $validateScalar $ExpectedContent 'Atomic compare content'
  }
  # Encode before creating directories or temporary files so invalid text cannot
  # cause even a partial managed mutation.
  $strictAtomicUtf8 = New-Object Text.UTF8Encoding($false, $true)
  $bytes = $strictAtomicUtf8.GetBytes([string]$Content)
  $directory = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
  }
  if ((Test-Path -LiteralPath $Path) -and -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Atomic text destination is not a file: $Path"
  }

  $destinationExists = Test-Path -LiteralPath $Path -PathType Leaf
  $destinationAcl = if ($destinationExists) {
    try { [System.IO.File]::GetAccessControl($Path) } catch {
      try { Get-Acl -LiteralPath $Path -ErrorAction Stop } catch {
        throw "Could not read the destination ACL before atomically replacing ${Path}: $($_.Exception.Message)"
      }
    }
  } else { $null }
  $temp = Join-Path $directory ('.{0}.{1}.tmp' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
  $backup = Join-Path $directory ('.{0}.{1}.rollback' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
  $preserveBackup = $false
  $destinationLock = $null
  try {
    # Creating an empty file can briefly inherit the directory ACL; no private
    # content is written until that ACL has been replaced with the destination
    # ACL or the installer's private-file ACL.
    $empty = [System.IO.File]::Open(
      $temp,
      [System.IO.FileMode]::CreateNew,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::None
    )
    $empty.Dispose()
    if ($null -ne $destinationAcl) {
      try { [System.IO.File]::SetAccessControl($temp, $destinationAcl) } catch {
        try { Set-Acl -LiteralPath $temp -AclObject $destinationAcl -ErrorAction Stop } catch {
          throw "Could not protect the atomic replacement for ${Path}: $($_.Exception.Message)"
        }
      }
    } else {
      Protect-PrivatePath $temp
    }

    $stream = [System.IO.File]::Open(
      $temp,
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::None
    )
    try {
      $stream.SetLength(0)
      $stream.Write($bytes, 0, $bytes.Length)
      $stream.Flush($true)
    } finally {
      $stream.Dispose()
    }

    if ($PSBoundParameters.ContainsKey('ExpectedContent') -and $destinationExists) {
      # Hold a read handle which shares delete (so ReplaceFile remains legal)
      # but not write. The comparison and atomic swap therefore form a CAS for
      # writers that have not already opened the destination.
      $destinationLock = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        ([IO.FileShare]::Read -bor [IO.FileShare]::Delete)
      )
      $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
      $reader = New-Object IO.StreamReader($destinationLock, $strictUtf8, $true, 4096, $true)
      try { $lockedContent = $reader.ReadToEnd() } finally { $reader.Dispose() }
      & $validateScalar $lockedContent 'Atomic compare destination'
      if (-not [string]::Equals($lockedContent, $ExpectedContent, [StringComparison]::Ordinal)) {
        throw "Atomic text destination changed before replacement: $Path"
      }
    }

    if ($destinationExists) {
      # File.Replace is a same-volume atomic swap. Its private rollback copy is
      # retained only long enough to restore the prior file if the swap fails.
      [System.IO.File]::Replace($temp, $Path, $backup, $true)
    } else {
      # A same-directory move is atomic for a newly created destination and
      # fails closed if another writer creates the path first.
      [System.IO.File]::Move($temp, $Path)
    }
  } catch {
    $writeError = $_
    if ($destinationExists -and (Test-Path -LiteralPath $backup -PathType Leaf)) {
      try {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
          $failed = Join-Path $directory ('.{0}.{1}.failed' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
          try { [System.IO.File]::Replace($backup, $Path, $failed, $true) } finally {
            if (Test-Path -LiteralPath $failed) {
              Remove-Item -LiteralPath $failed -Force -ErrorAction SilentlyContinue
            }
          }
        } else {
          [System.IO.File]::Move($backup, $Path)
        }
      } catch {
        $preserveBackup = $true
        throw "Atomic write failed for ${Path}, and rollback also failed. The prior private file remains at ${backup}. $($writeError.Exception.Message) Rollback: $($_.Exception.Message)"
      }
    }
    throw $writeError
  } finally {
    if ($null -ne $destinationLock) { $destinationLock.Dispose() }
    if (Test-Path -LiteralPath $temp) {
      Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
    if (-not $preserveBackup -and (Test-Path -LiteralPath $backup)) {
      Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
  }
}

function Protect-PrivatePath {
  param([string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    return
  }
  $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $item = Get-Item -LiteralPath $Path
  $grants = if ($item.PSIsContainer) {
    @("${identity}:(OI)(CI)F", '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
  } else {
    @("${identity}:F", '*S-1-5-18:F', '*S-1-5-32-544:F')
  }
  try {
    & icacls.exe $Path /inheritance:r /grant:r $grants | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "icacls exited with $LASTEXITCODE"
    }
  } catch {
    throw "Could not tighten ACL for ${Path}: $($_.Exception.Message)"
  }
}

function Add-ConfigDefault {
  param(
    [object]$Config,
    [string]$Name,
    [object]$Value
  )

  if ($null -eq $Config.PSObject.Properties[$Name]) {
    Add-Member -InputObject $Config -MemberType NoteProperty -Name $Name -Value $Value
    return $true
  }
  return $false
}

function Get-LegacyConstant {
  param(
    [string]$Path,
    [string]$VariableName
  )

  if (-not (Test-Path -LiteralPath $Path)) {
    return $null
  }
  $tokens = $null
  $errors = $null
  $source = Read-StrictUtf8Text -Path $Path
  $ast = [System.Management.Automation.Language.Parser]::ParseInput($source, $Path, [ref]$tokens, [ref]$errors)
  if ($errors.Count -gt 0) {
    return $null
  }
  $assignments = $ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $node.Left.VariablePath.UserPath -eq $VariableName
    }, $true)
  foreach ($assignment in $assignments) {
    try {
      $right = $assignment.Right
      if ($right -is [System.Management.Automation.Language.CommandExpressionAst]) {
        $right = $right.Expression
      }
      if ($right -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
          $right -is [System.Management.Automation.Language.ConstantExpressionAst]) {
        return [string]$right.Value
      }
      return [string]$right.SafeGetValue()
    } catch {
      continue
    }
  }
  return $null
}

function New-PrivateConfigIfNeeded {
  param(
    [string]$Target,
    [string]$LegacyScript
  )

  $workerSqlitePath = if ([string]::IsNullOrWhiteSpace($env:CODEX_SQLITE_HOME)) {
    Split-Path -Parent $Target
  } else {
    [IO.Path]::GetFullPath($env:CODEX_SQLITE_HOME)
  }

  if (Test-Path -LiteralPath $Target) {
    $config = Read-StrictJsonFile -Path $Target
    $changed = $false
    $changed = (Add-ConfigDefault -Config $config -Name 'token' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'username' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'password' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_message' -Value $false) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_thread_title' -Value $false) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_task_link' -Value $false) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_task_link_action' -Value $false) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'allow_insecure_auth' -Value $false) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'priority' -Value 3) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'tags' -Value @('white_check_mark')) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'max_message_chars' -Value 180) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'markdown' -Value $false) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'subagent_classification_grace_seconds' -Value 8) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'idle_detection_mode' -Value 'strict') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'idle_grace_seconds' -Value 1.5) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'idle_probe_grace_seconds' -Value 30) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'unknown_retry_max_seconds' -Value 60) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'goal_aware' -Value $true) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'goal_poll_seconds' -Value 1) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'subagent_orphan_seconds' -Value 1800) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'suppress_technical_turns' -Value $true) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'watch_rollouts' -Value $true) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'watch_scan_seconds' -Value 2) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'watch_discovery_seconds' -Value 60) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'watch_cursor_batch_size' -Value 64) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'watch_remote_timeout_seconds' -Value 90) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'watch_initial_replay_seconds' -Value 15) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'watch_roots' -Value @()) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'worker_sqlite_path' -Value $workerSqlitePath) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'dead_retention_days' -Value 30) -or $changed
    $configuredTags = @($config.tags)
    if ($configuredTags.Count -eq 2 -and
        [string]$configuredTags[0] -eq 'computer' -and
        [string]$configuredTags[1] -eq 'white_check_mark') {
      $config.tags = @('white_check_mark')
      $changed = $true
    }
    if ($null -eq $config.PSObject.Properties['max_attempts'] -or [int]$config.max_attempts -eq 40) {
      if ($null -eq $config.PSObject.Properties['max_attempts']) {
        Add-Member -InputObject $config -MemberType NoteProperty -Name 'max_attempts' -Value 0
      } else {
        $config.max_attempts = 0
      }
      $changed = $true
    }
    $authValues = [ordered]@{
      token = [string]$env:CODEX_NTFY_TOKEN
      username = [string]$env:CODEX_NTFY_USER
      password = [string]$env:CODEX_NTFY_PASSWORD
    }
    foreach ($name in $authValues.Keys) {
      $value = [string]$authValues[$name]
      if (-not [string]::IsNullOrWhiteSpace($value) -and [string]::IsNullOrWhiteSpace([string]$config.$name)) {
        $config.$name = $value
        $changed = $true
      }
    }
    if ($changed) {
      Write-TextAtomic -Path $Target -Content ($config | ConvertTo-Json -Depth 8)
      Write-Status 'Updated private configuration defaults.'
    }
    Protect-PrivatePath $Target
    return
  }
  $server = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_SERVER)) {
    Get-LegacyConstant -Path $LegacyScript -VariableName 'DefaultServer'
  } else { $env:CODEX_NTFY_SERVER }
  $topic = if ([string]::IsNullOrWhiteSpace($env:CODEX_NTFY_TOPIC)) {
    Get-LegacyConstant -Path $LegacyScript -VariableName 'DefaultTopic'
  } else { $env:CODEX_NTFY_TOPIC }
  if ([string]::IsNullOrWhiteSpace($server)) {
    $server = 'https://ntfy.sh'
  }
  if ([string]::IsNullOrWhiteSpace($topic)) {
    try {
      $secureTopic = Read-Host 'Enter the private ntfy topic (input hidden)' -AsSecureString
      $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureTopic)
      try {
        $topic = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
      } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
      }
    } catch {
      throw 'No ntfy topic found. Set CODEX_NTFY_TOPIC for a non-interactive installation.'
    }
  }
  if ([string]::IsNullOrWhiteSpace($topic)) {
    throw 'The ntfy topic cannot be empty.'
  }
  $config = [ordered]@{
    server = $server
    topic = $topic
    token = [string]$env:CODEX_NTFY_TOKEN
    username = [string]$env:CODEX_NTFY_USER
    password = [string]$env:CODEX_NTFY_PASSWORD
    allow_insecure_auth = $false
    priority = 3
    tags = @('white_check_mark')
    max_message_chars = 180
    include_message = $false
    include_thread_title = $false
    include_task_link = $false
    include_task_link_action = $false
    markdown = $false
    include_full_path = $false
    suppress_subagents = $true
    subagent_classification_grace_seconds = 8
    idle_detection_mode = 'strict'
    idle_grace_seconds = 1.5
    idle_probe_grace_seconds = 30
    unknown_retry_max_seconds = 60
    goal_aware = $true
    goal_poll_seconds = 1
    subagent_orphan_seconds = 1800
    suppress_technical_turns = $true
    watch_rollouts = $true
    watch_scan_seconds = 2
    watch_discovery_seconds = 60
    watch_cursor_batch_size = 64
    watch_remote_timeout_seconds = 90
    watch_initial_replay_seconds = 15
    watch_roots = @()
    worker_sqlite_path = $workerSqlitePath
    timeout_seconds = 12
    max_attempts = 0
    retry_max_seconds = 900
    sent_retention_days = 14
    dead_retention_days = 30
  }
  Write-TextAtomic -Path $Target -Content ($config | ConvertTo-Json -Depth 5)
  Protect-PrivatePath $Target
  Write-Status 'Migrated the existing ntfy destination into a private config file.'
}

function Backup-CurrentInstallation {
  param([string]$HomePath)

  $backupRoot = Join-Path $HomePath 'ntfy-backups'
  if (-not (Test-Path -LiteralPath $backupRoot)) {
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    Protect-PrivatePath $backupRoot
  }
  $backup = Join-Path $backupRoot (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
  New-Item -ItemType Directory -Path $backup -Force | Out-Null
  Protect-PrivatePath $backup
  foreach ($name in @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'config.toml', 'hooks.json', 'ntfy-config.json')) {
    $source = Join-Path $HomePath $name
    if (Test-Path -LiteralPath $source) {
      Copy-Item -LiteralPath $source -Destination (Join-Path $backup $name) -Force
    }
  }
  try {
    $xml = & schtasks.exe /Query /TN $TaskName /XML 2>$null
    if ($LASTEXITCODE -eq 0 -and $xml) {
      $xmlText = ($xml -join [Environment]::NewLine) -replace '(?i)encoding="utf-16"', 'encoding="utf-8"'
      Write-TextAtomic -Path (Join-Path $backup 'CodexNtfyWatcher.xml') -Content $xmlText
    }
  } catch {
    # The task may not exist yet.
  }
  Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    Select-Object -Skip 10 |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  Write-Status "Rollback backup created at $backup"
  return $backup
}

function Ensure-TopLevelNotify {
  param(
    [string]$ConfigPath,
    [string]$NotifyLine,
    [string]$ExpectedMarker
  )

  $text = if (Test-Path -LiteralPath $ConfigPath) {
    Read-StrictUtf8Text -Path $ConfigPath
  } else { '' }
  $table = [regex]::Match($text, '(?m)^[ \t]*\[')
  $rootText = if ($table.Success) { $text.Substring(0, $table.Index) } else { $text }
  $match = [regex]::Match($rootText, '(?m)^[ \t]*notify[ \t]*=.*$')
  if ($match.Success) {
    if ($match.Value -notmatch [regex]::Escape($ExpectedMarker)) {
      throw "Existing notify command in $ConfigPath is unrelated; refusing to overwrite it."
    }
    if ($match.Value -ne $NotifyLine) {
      $updated = $text.Remove($match.Index, $match.Length).Insert($match.Index, $NotifyLine)
      Write-TextAtomic -Path $ConfigPath -Content $updated
    }
    return
  }
  if ($table.Success) {
    $updated = $text.Insert($table.Index, $NotifyLine + [Environment]::NewLine + [Environment]::NewLine)
  } else {
    $prefix = if ([string]::IsNullOrWhiteSpace($text)) { '' } else { $text.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine }
    $updated = $prefix + $NotifyLine + [Environment]::NewLine
  }
  Write-TextAtomic -Path $ConfigPath -Content $updated
}

function Test-ManagedHookHandler {
  param([object]$Handler)

  if ($null -eq $Handler -or $Handler -isnot [System.Management.Automation.PSCustomObject]) {
    return $false
  }
  foreach ($field in @('command', 'commandWindows', 'command_windows')) {
    $property = $Handler.PSObject.Properties[$field]
    if ($null -ne $property -and $property.Value -is [string] -and
        $property.Value -match '(?i)(?:^|[\\/])notify-ntfy(?:\.ps1|\.py|-wsl\.sh)(?=$|[\s''"])') {
      return $true
    }
  }
  return $false
}

function Test-ManagedClaudeHookHandler {
  param(
    [object]$Handler,
    [string]$ExpectedScriptPath,
    [string]$HookSwitch = '-ClaudeHook'
  )

  if ($null -eq $Handler -or $Handler -isnot [System.Management.Automation.PSCustomObject] -or
      [string](Get-ObjectValue -Object $Handler -Name 'type' -Default '') -ne 'command') {
    return $false
  }
  $command = [string](Get-ObjectValue -Object $Handler -Name 'command' -Default '')
  $argsProperty = $Handler.PSObject.Properties['args']
  if ($null -ne $argsProperty -and $argsProperty.Value -is [array]) {
    $args = @($argsProperty.Value | ForEach-Object { [string]$_ })
    $fileIndex = [Array]::IndexOf([string[]]$args, '-File')
    if ($args -notcontains $HookSwitch -or $fileIndex -lt 0 -or $fileIndex + 1 -ge $args.Count) {
      return $false
    }
    try {
      return [string]::Equals(
          [System.IO.Path]::GetFullPath([string]$args[$fileIndex + 1]),
          [System.IO.Path]::GetFullPath($ExpectedScriptPath),
          [System.StringComparison]::OrdinalIgnoreCase
        )
    } catch {
      return $false
    }
  }
  try { $expectedFullPath = [System.IO.Path]::GetFullPath($ExpectedScriptPath) } catch { return $false }
  $expectedLiteral = ConvertTo-PowerShellSingleQuotedLiteral $expectedFullPath
  if ($command.IndexOf($expectedLiteral, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
    $switchPattern = '(?i)(?:^|\s)' + [regex]::Escape($HookSwitch) + '(?:\s|$)'
    return $command -match $switchPattern
  }
  $scriptIndex = $command.IndexOf($expectedFullPath, [System.StringComparison]::OrdinalIgnoreCase)
  if ($scriptIndex -lt 0) { return $false }
  $beforeOk = $scriptIndex -eq 0 -or [char]::IsWhiteSpace($command[$scriptIndex - 1]) -or
    $command[$scriptIndex - 1] -in @([char]34, [char]39)
  $afterIndex = $scriptIndex + $expectedFullPath.Length
  $afterOk = $afterIndex -eq $command.Length -or [char]::IsWhiteSpace($command[$afterIndex]) -or
    $command[$afterIndex] -in @([char]34, [char]39)
  $switchPattern = '(?i)(?:^|\s)' + [regex]::Escape($HookSwitch) + '(?:\s|$)'
  return $beforeOk -and $afterOk -and $command -match $switchPattern
}

function Test-ManagedAudnCodeHookHandler {
  param([object]$Handler)

  if ($null -eq $Handler -or $Handler -isnot [System.Management.Automation.PSCustomObject] -or
      [string](Get-ObjectValue -Object $Handler -Name 'type' -Default '') -ne 'command') {
    return $false
  }
  $hookSwitch = '-AudnCodeHook'
  $argsProperty = $Handler.PSObject.Properties['args']
  if ($null -ne $argsProperty -and $argsProperty.Value -is [array]) {
    $args = @($argsProperty.Value | ForEach-Object { [string]$_ })
    $fileIndex = [Array]::IndexOf([string[]]$args, '-File')
    if ($args -notcontains $hookSwitch -or $fileIndex -lt 0 -or $fileIndex + 1 -ge $args.Count) {
      return $false
    }
    try {
      return [string]::Equals(
          [System.IO.Path]::GetFileName([string]$args[$fileIndex + 1]),
          'notify-ntfy.ps1',
          [System.StringComparison]::OrdinalIgnoreCase
        )
    } catch {
      return $false
    }
  }

  $command = [string](Get-ObjectValue -Object $Handler -Name 'command' -Default '')
  return $command -match '(?i)(?:^|[\\/''"\s])notify-ntfy\.ps1(?=$|[\s''"])' -and
    $command -match '(?i)(?:^|\s)-AudnCodeHook(?:\s|$)'
}

function Get-ObjectValue {
  param(
    [object]$Object,
    [string]$Name,
    [object]$Default = $null
  )

  if ($null -eq $Object) { return $Default }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property -or $null -eq $property.Value) { return $Default }
  return $property.Value
}

function ConvertTo-PosixShellArgument {
  param([string]$Value)

  $singleQuote = [string][char]39
  $doubleQuote = [string][char]34
  $escapedQuote = $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote
  return $singleQuote + $Value.Replace($singleQuote, $escapedQuote) + $singleQuote
}

function ConvertTo-PowerShellSingleQuotedLiteral {
  param([string]$Value)

  return "'" + $Value.Replace("'", "''") + "'"
}

function Ensure-StopHook {
  param(
    [string]$HooksPath,
    [string]$Command
  )

  $original = if (Test-Path -LiteralPath $HooksPath) {
    Read-StrictUtf8Text -Path $HooksPath
  } else { '' }
  try {
    $document = if ([string]::IsNullOrWhiteSpace($original)) {
      [pscustomobject][ordered]@{}
    } else {
      ConvertFrom-StrictJsonText -Text $original
    }
  } catch {
    throw "Invalid JSON in ${HooksPath}: $($_.Exception.Message)"
  }
  if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
    throw "$HooksPath must contain a JSON object."
  }

  $hooksProperty = $document.PSObject.Properties['hooks']
  if ($null -eq $hooksProperty) {
    Add-Member -InputObject $document -MemberType NoteProperty -Name 'hooks' -Value ([pscustomobject][ordered]@{})
    $hooksProperty = $document.PSObject.Properties['hooks']
  } elseif ($null -eq $hooksProperty.Value -or $hooksProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
    throw "hooks in $HooksPath must contain a JSON object."
  }
  $hookEvents = $hooksProperty.Value

  foreach ($eventProperty in @($hookEvents.PSObject.Properties)) {
    if ($eventProperty.Value -isnot [array]) {
      if ($eventProperty.Name -eq 'Stop') {
        throw "hooks.Stop in $HooksPath must contain a JSON array."
      }
      continue
    }
    $filteredGroups = New-Object 'System.Collections.Generic.List[object]'
    $removedFromEvent = $false
    foreach ($group in @($eventProperty.Value)) {
      if ($null -eq $group -or $group -isnot [System.Management.Automation.PSCustomObject]) {
        $filteredGroups.Add($group)
        continue
      }
      $handlersProperty = $group.PSObject.Properties['hooks']
      if ($null -eq $handlersProperty -or $handlersProperty.Value -isnot [array]) {
        $filteredGroups.Add($group)
        continue
      }
      $filteredHandlers = New-Object 'System.Collections.Generic.List[object]'
      $removedFromGroup = $false
      foreach ($handler in @($handlersProperty.Value)) {
        if (Test-ManagedHookHandler -Handler $handler) {
          $removedFromGroup = $true
          $removedFromEvent = $true
        } else {
          $filteredHandlers.Add($handler)
        }
      }
      if ($filteredHandlers.Count -gt 0) {
        $handlersProperty.Value = @($filteredHandlers.ToArray())
        $filteredGroups.Add($group)
      } elseif (-not $removedFromGroup) {
        $filteredGroups.Add($group)
      }
    }
    if ($filteredGroups.Count -gt 0 -or -not $removedFromEvent) {
      $eventProperty.Value = @($filteredGroups.ToArray())
    } else {
      $hookEvents.PSObject.Properties.Remove($eventProperty.Name)
    }
  }

  $stopProperty = $hookEvents.PSObject.Properties['Stop']
  if ($null -eq $stopProperty) {
    Add-Member -InputObject $hookEvents -MemberType NoteProperty -Name 'Stop' -Value @()
    $stopProperty = $hookEvents.PSObject.Properties['Stop']
  } elseif ($stopProperty.Value -isnot [array]) {
    throw "hooks.Stop in $HooksPath must contain a JSON array."
  }
  $managedGroup = [pscustomobject][ordered]@{
    hooks = @(
      [pscustomobject][ordered]@{
        type = 'command'
        command = $Command
        timeout = 30
      }
    )
  }
  $stopProperty.Value = @(@($stopProperty.Value) + $managedGroup)

  $rendered = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
  if ($rendered -ne $original) {
    Write-TextAtomic -Path $HooksPath -Content $rendered
  }
}

function Ensure-ClaudeCodeHooks {
  param(
    [string]$SettingsPath,
    [string]$PowerShellPath,
    [string]$ScriptPath
  )

  $settingsAcl = if (Test-Path -LiteralPath $SettingsPath) {
    try { Get-Acl -LiteralPath $SettingsPath } catch { $null }
  } else { $null }
  $original = if (Test-Path -LiteralPath $SettingsPath) {
    Read-StrictUtf8Text -Path $SettingsPath
  } else { '' }
  try {
    $document = if ([string]::IsNullOrWhiteSpace($original)) {
      [pscustomobject][ordered]@{}
    } else {
      ConvertFrom-StrictJsonText -Text $original
    }
  } catch {
    throw "Invalid JSON in ${SettingsPath}: $($_.Exception.Message)"
  }
  if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
    throw "$SettingsPath must contain a JSON object."
  }

  $hooksProperty = $document.PSObject.Properties['hooks']
  if ($null -eq $hooksProperty) {
    Add-Member -InputObject $document -MemberType NoteProperty -Name 'hooks' -Value ([pscustomobject][ordered]@{})
    $hooksProperty = $document.PSObject.Properties['hooks']
  } elseif ($null -eq $hooksProperty.Value -or $hooksProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
    throw "hooks in $SettingsPath must contain a JSON object."
  }
  $hookEvents = $hooksProperty.Value

  foreach ($eventProperty in @($hookEvents.PSObject.Properties)) {
    if ($eventProperty.Value -isnot [array]) {
      if ($eventProperty.Name -in @('Stop', 'StopFailure', 'UserPromptSubmit', 'UserPromptExpansion', 'Notification')) {
        throw "hooks.$($eventProperty.Name) in $SettingsPath must contain a JSON array."
      }
      continue
    }
    $filteredGroups = New-Object 'System.Collections.Generic.List[object]'
    $removedFromEvent = $false
    foreach ($group in @($eventProperty.Value)) {
      if ($null -eq $group -or $group -isnot [System.Management.Automation.PSCustomObject]) {
        $filteredGroups.Add($group)
        continue
      }
      $handlersProperty = $group.PSObject.Properties['hooks']
      if ($null -eq $handlersProperty -or $handlersProperty.Value -isnot [array]) {
        $filteredGroups.Add($group)
        continue
      }
      $filteredHandlers = New-Object 'System.Collections.Generic.List[object]'
      $removedFromGroup = $false
      foreach ($handler in @($handlersProperty.Value)) {
        if (Test-ManagedClaudeHookHandler -Handler $handler -ExpectedScriptPath $ScriptPath) {
          $removedFromGroup = $true
          $removedFromEvent = $true
        } else {
          $filteredHandlers.Add($handler)
        }
      }
      if ($filteredHandlers.Count -gt 0) {
        $handlersProperty.Value = @($filteredHandlers.ToArray())
        $filteredGroups.Add($group)
      } elseif (-not $removedFromGroup) {
        $filteredGroups.Add($group)
      }
    }
    if ($filteredGroups.Count -gt 0 -or -not $removedFromEvent) {
      $eventProperty.Value = @($filteredGroups.ToArray())
    } else {
      $hookEvents.PSObject.Properties.Remove($eventProperty.Name)
    }
  }

  foreach ($eventName in @('Stop', 'StopFailure', 'UserPromptSubmit', 'Notification')) {
    $eventProperty = $hookEvents.PSObject.Properties[$eventName]
    if ($null -eq $eventProperty) {
      Add-Member -InputObject $hookEvents -MemberType NoteProperty -Name $eventName -Value @()
      $eventProperty = $hookEvents.PSObject.Properties[$eventName]
    } elseif ($eventProperty.Value -isnot [array]) {
      throw "hooks.$eventName in $SettingsPath must contain a JSON array."
    }
    $handler = [pscustomobject][ordered]@{
      type = 'command'
      command = $PowerShellPath
      args = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $ScriptPath, '-ClaudeHook', '-ReadStdin', '-Origin', 'Claude Code'
      )
      timeout = 30
      # Prompt and terminal hook ordering is part of the correctness contract.
      # UserPromptSubmit must establish the epoch before Stop, and repeated Stop
      # events for one prompt must reach the queue in lifecycle order. Only the
      # optional Notification accelerators are safe to run asynchronously.
      async = $eventName -eq 'Notification'
    }
    $managedGroups = if ($eventName -eq 'Notification') {
      @(
        [pscustomobject][ordered]@{ matcher = 'idle_prompt'; hooks = @($handler) },
        [pscustomobject][ordered]@{ matcher = 'agent_completed'; hooks = @($handler) }
      )
    } else {
      @([pscustomobject][ordered]@{ hooks = @($handler) })
    }
    $eventProperty.Value = @(@($eventProperty.Value) + @($managedGroups))
  }

  $rendered = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
  if ($rendered -ne $original) {
    Write-TextAtomic -Path $SettingsPath -Content $rendered
    if ($null -ne $settingsAcl) {
      try { Set-Acl -LiteralPath $SettingsPath -AclObject $settingsAcl } catch {
        throw "Claude settings were updated, but their original ACL could not be restored: $($_.Exception.Message)"
      }
    }
  }
}

function Get-LiveAudnCodeSessionMarkers {
  param([string]$HomePath)

  $sessionsRoot = Join-Path $HomePath 'sessions'
  if (-not (Test-Path -LiteralPath $sessionsRoot -PathType Container)) { return @() }

  $items = @(Get-ChildItem -LiteralPath $sessionsRoot -Filter '*.json' -Force -ErrorAction Stop | Select-Object -First 4097)
  if ($items.Count -gt 4096) {
    throw "AudnCode has too many session markers to verify safely below $sessionsRoot."
  }
  $live = New-Object 'System.Collections.Generic.List[object]'
  $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
  foreach ($item in $items) {
    if ($item.PSIsContainer) { continue }
    $filePid = 0
    if (-not [int]::TryParse([string]$item.BaseName, [ref]$filePid) -or $filePid -le 0) { continue }

    $process = Get-Process -Id $filePid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
      # AudnCode removes markers on a clean exit. A leftover marker whose PID
      # no longer exists is stale and must not permanently block maintenance.
      continue
    }
    try {
      $processStartedUnixMs = ([DateTimeOffset]$process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()
    } catch {
      throw "A live process owns AudnCode session marker $($item.FullName), but its start time could not be verified. Close AudnCode and retry."
    }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw "A live PID owns reparse-point AudnCode session marker $($item.FullName); refusing to mutate settings.json."
    }

    $marker = $null
    $readError = $null
    for ($readAttempt = 1; $readAttempt -le 4; $readAttempt++) {
      try {
        $stream = [IO.File]::Open(
          $item.FullName,
          [IO.FileMode]::Open,
          [IO.FileAccess]::Read,
          ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        )
        try {
          if ($stream.Length -gt $AudnCodeSessionMarkerMaxBytes) {
            throw "marker exceeds the $AudnCodeSessionMarkerMaxBytes byte limit"
          }
          $reader = New-Object IO.StreamReader($stream, $strictUtf8, $true, 4096, $true)
          try { $rawMarker = $reader.ReadToEnd() } finally { $reader.Dispose() }
        } finally {
          $stream.Dispose()
        }
        $marker = ConvertFrom-StrictJsonText -Text $rawMarker
        if ($null -eq $marker -or $marker -isnot [System.Management.Automation.PSCustomObject]) {
          throw 'marker is not a JSON object'
        }
        $readError = $null
        break
      } catch {
        $readError = $_
        if ($readAttempt -lt 4) { Start-Sleep -Milliseconds 25 }
      }
    }
    if ($null -ne $readError) {
      throw "A live PID owns unreadable AudnCode session marker $($item.FullName). Close AudnCode and retry. $($readError.Exception.Message)"
    }

    $markerPid = 0
    $markerStartedUnixMs = [int64]0
    try {
      $markerPid = [int](Get-ObjectValue -Object $marker -Name 'pid' -Default 0)
      $markerStartedUnixMs = [int64](Get-ObjectValue -Object $marker -Name 'startedAt' -Default 0)
    } catch {
      throw "A live PID owns invalid AudnCode session marker $($item.FullName). Close AudnCode and retry."
    }
    if ($markerPid -ne $filePid -or $markerStartedUnixMs -le 0) {
      throw "A live PID owns mismatched AudnCode session marker $($item.FullName). Close AudnCode and retry."
    }
    if ([Math]::Abs($processStartedUnixMs - $markerStartedUnixMs) -gt 120000) {
      # The PID belongs to a newer process lifetime. Ignore this stale marker;
      # a real current AudnCode host will publish its own matching marker.
      continue
    }

    $kind = [string](Get-ObjectValue -Object $marker -Name 'kind' -Default '')
    $sessionId = [string](Get-ObjectValue -Object $marker -Name 'sessionId' -Default '')
    $parsedSessionId = [Guid]::Empty
    if ($kind -notin @('interactive', 'bg', 'daemon', 'daemon-worker') -or
        -not [Guid]::TryParse($sessionId, [ref]$parsedSessionId)) {
      throw "A live PID owns an unrecognized AudnCode session marker $($item.FullName). Close AudnCode and retry."
    }
    $live.Add([pscustomobject]@{
        pid = $filePid
        started_unix_ms = $markerStartedUnixMs
        session_id = $sessionId
        kind = $kind
      })
  }
  return @($live.ToArray())
}

function Assert-NoLiveAudnCodeSessions {
  param([string]$HomePath)

  $live = @(Get-LiveAudnCodeSessionMarkers -HomePath $HomePath)
  if ($live.Count -eq 0) { return }
  $pids = (@($live | ForEach-Object { [string]$_.pid } | Sort-Object -Unique) -join ', ')
  throw "Close every AudnCode process that uses $HomePath before changing managed hooks. Verified live PID(s): $pids. No AudnCode settings were changed."
}

function Assert-AudnCodeSettingsQuiet {
  param(
    [string]$SettingsPath,
    [string]$ExpectedContent,
    [string]$HomePath,
    [ValidateRange(100, 5000)]
    [int]$QuietMilliseconds = $AudnCodeSettingsQuietWindowMilliseconds
  )

  $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds($QuietMilliseconds)
  do {
    Assert-NoLiveAudnCodeSessions -HomePath $HomePath
    $observed = if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
      Read-StrictUtf8Text -Path $SettingsPath
    } else { '' }
    if (-not [string]::Equals($observed, $ExpectedContent, [StringComparison]::Ordinal)) {
      throw 'AudnCode settings changed during quiet verification after managed hooks were installed; refusing to report success.'
    }
    if ([DateTimeOffset]::UtcNow -ge $deadline) { return }
    Start-Sleep -Milliseconds 50
  } while ($true)
}

function Ensure-AudnCodeHooks {
  param(
    [string]$SettingsPath,
    [string]$PowerShellPath,
    [string]$ScriptPath,
    [string]$AudnCodeHomePath,
    [Parameter(Mandatory = $true)]
    [ref]$MutationState
  )

  $MutationState.Value = $null
  for ($attempt = 1; $attempt -le 8; $attempt++) {
    $filePreviouslyPresent = Test-Path -LiteralPath $SettingsPath -PathType Leaf
    $original = if ($filePreviouslyPresent) {
      Read-StrictUtf8Text -Path $SettingsPath
    } else { '' }
    try {
      $document = if ([string]::IsNullOrWhiteSpace($original)) {
        [pscustomobject][ordered]@{}
      } else {
        ConvertFrom-StrictJsonText -Text $original
      }
    } catch {
      throw "Invalid JSON in ${SettingsPath}: $($_.Exception.Message)"
    }
    if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
      throw "$SettingsPath must contain a JSON object."
    }

    $hooksProperty = $document.PSObject.Properties['hooks']
    $hooksPreviouslyPresent = $null -ne $hooksProperty
    if ($null -eq $hooksProperty) {
      Add-Member -InputObject $document -MemberType NoteProperty -Name 'hooks' -Value ([pscustomobject][ordered]@{})
      $hooksProperty = $document.PSObject.Properties['hooks']
    } elseif ($null -eq $hooksProperty.Value -or $hooksProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
      throw "hooks in $SettingsPath must contain a JSON object."
    }
    $hookEvents = $hooksProperty.Value
    $beforeEvents = @{}
    foreach ($eventProperty in @($hookEvents.PSObject.Properties)) {
      $beforeEvents[$eventProperty.Name] = ConvertTo-Json -InputObject $eventProperty.Value -Depth 100 -Compress
    }

    foreach ($eventProperty in @($hookEvents.PSObject.Properties)) {
      if ($eventProperty.Value -isnot [array]) {
        if ($eventProperty.Name -in @('SessionStart', 'Stop', 'StopFailure', 'UserPromptSubmit', 'Notification', 'PostToolUse', 'SubagentStart')) {
          throw "hooks.$($eventProperty.Name) in $SettingsPath must contain a JSON array."
        }
        continue
      }
      $filteredGroups = New-Object 'System.Collections.Generic.List[object]'
      $removedFromEvent = $false
      foreach ($group in @($eventProperty.Value)) {
        if ($null -eq $group -or $group -isnot [System.Management.Automation.PSCustomObject]) {
          $filteredGroups.Add($group)
          continue
        }
        $handlersProperty = $group.PSObject.Properties['hooks']
        if ($null -eq $handlersProperty -or $handlersProperty.Value -isnot [array]) {
          $filteredGroups.Add($group)
          continue
        }
        $filteredHandlers = New-Object 'System.Collections.Generic.List[object]'
        $removedFromGroup = $false
        foreach ($handler in @($handlersProperty.Value)) {
          # A previous installation can point at a different CodexHome. Treat
          # every notifier AudnCode hook as managed so upgrades never duplicate it.
          if (Test-ManagedAudnCodeHookHandler -Handler $handler) {
            $removedFromGroup = $true
            $removedFromEvent = $true
          } else {
            $filteredHandlers.Add($handler)
          }
        }
        if ($filteredHandlers.Count -gt 0) {
          $handlersProperty.Value = @($filteredHandlers.ToArray())
          $filteredGroups.Add($group)
        } elseif (-not $removedFromGroup) {
          $filteredGroups.Add($group)
        }
      }
      if ($filteredGroups.Count -gt 0 -or -not $removedFromEvent) {
        $eventProperty.Value = @($filteredGroups.ToArray())
      } else {
        $hookEvents.PSObject.Properties.Remove($eventProperty.Name)
      }
    }

    # Run inside AudnCode's existing `shell = powershell` process. Spawning a
    # second PowerShell would let that grandchild survive if the wrapper exits
    # before enforcing its timeout. The installed copy is unblocked below, and
    # process-scoped Bypass keeps local policy from rejecting it.
    $commandPrefix = 'Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; & ' +
      (ConvertTo-PowerShellSingleQuotedLiteral $ScriptPath) +
      " -AudnCodeHook -ReadStdin -Origin 'AudnCode' -AudnCodeHome " +
      (ConvertTo-PowerShellSingleQuotedLiteral $AudnCodeHomePath)
    foreach ($eventName in @('SessionStart', 'Stop', 'StopFailure', 'UserPromptSubmit', 'Notification', 'PostToolUse', 'SubagentStart')) {
      $eventProperty = $hookEvents.PSObject.Properties[$eventName]
      if ($null -eq $eventProperty) {
        Add-Member -InputObject $hookEvents -MemberType NoteProperty -Name $eventName -Value @()
        $eventProperty = $hookEvents.PSObject.Properties[$eventName]
      } elseif ($eventProperty.Value -isnot [array]) {
        throw "hooks.$eventName in $SettingsPath must contain a JSON array."
      }
      # The event name comes from this installer-controlled command line, not
      # from stdin. Prompt/tool hooks can therefore arm durable ingress state
      # before reading or parsing a potentially malformed payload.
      $command = $commandPrefix + ' -AudnCodeExpectedEvent ' +
        (ConvertTo-PowerShellSingleQuotedLiteral $eventName)
      $handler = [pscustomobject][ordered]@{
        type = 'command'
        command = $command
        shell = 'powershell'
        timeout = 60
        # Keep lifecycle events ordered so final-idle and background-tool state
        # cannot be overtaken by the next UserPromptSubmit.
        async = $false
      }
      $managedGroups = if ($eventName -eq 'SessionStart') {
        # AudnCode also emits SessionStart for compact, which is mid-query and
        # must never create a fresh root epoch. Anchor this pre-query fallback
        # only to the three lifecycle sources that precede direct initial work.
        @([pscustomobject][ordered]@{ matcher = '^(startup|resume|clear)$'; hooks = @($handler) })
      } elseif ($eventName -eq 'Notification') {
        @([pscustomobject][ordered]@{ matcher = 'idle_prompt'; hooks = @($handler) })
      } elseif ($eventName -eq 'PostToolUse') {
        @([pscustomobject][ordered]@{ matcher = 'Agent|Bash|PowerShell|Monitor|TaskStop|KillShell|CronCreate|CronDelete|SendMessage'; hooks = @($handler) })
      } else {
        @([pscustomobject][ordered]@{ hooks = @($handler) })
      }
      $eventProperty.Value = @(@($eventProperty.Value) + @($managedGroups))
    }

    $rendered = ($document | ConvertTo-Json -Depth 100) + [Environment]::NewLine
    if ($rendered -eq $original) { return }

    # AudnCode does not expose a shared lock for settings.json. A verified live
    # host is therefore a hard precondition failure whenever this run needs to
    # change hook content. Stale markers and reused PIDs are ignored above.
    Assert-NoLiveAudnCodeSessions -HomePath $AudnCodeHomePath
    $current = if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
      Read-StrictUtf8Text -Path $SettingsPath
    } else { '' }
    if (-not [string]::Equals($current, $original, [System.StringComparison]::Ordinal)) {
      Start-Sleep -Milliseconds (20 * $attempt)
      continue
    }
    # Close the remaining check/write race as far as the marker protocol permits:
    # a host that starts after the first check must be detected before the swap
    # or during the postcondition quiet window.
    Assert-NoLiveAudnCodeSessions -HomePath $AudnCodeHomePath

    $eventStates = New-Object 'System.Collections.Generic.List[object]'
    $allEventNames = @(@($beforeEvents.Keys) + @($hookEvents.PSObject.Properties | ForEach-Object { $_.Name }) | Sort-Object -Unique)
    foreach ($eventName in $allEventNames) {
      $afterProperty = $hookEvents.PSObject.Properties[$eventName]
      $beforePresent = $beforeEvents.ContainsKey($eventName)
      $afterPresent = $null -ne $afterProperty
      $beforeJson = if ($beforePresent) { [string]$beforeEvents[$eventName] } else { '' }
      $afterJson = if ($afterPresent) {
        ConvertTo-Json -InputObject $afterProperty.Value -Depth 100 -Compress
      } else { '' }
      if ($beforePresent -ne $afterPresent -or $beforeJson -ne $afterJson) {
        $eventStates.Add([pscustomobject]@{
            Name = $eventName
            BeforePresent = $beforePresent
            BeforeJson = $beforeJson
            InstalledPresent = $afterPresent
            InstalledJson = $afterJson
        })
      }
    }
    # Windows PowerShell and pwsh render equivalent JSON with different
    # whitespace. If every hook event is semantically unchanged, avoid a
    # formatting-only rewrite (and needless observation-generation rotation).
    if ($eventStates.Count -eq 0) { return }
    $MutationState.Value = [pscustomobject]@{
      Applied = $true
      FilePreviouslyPresent = [bool]$filePreviouslyPresent
      HooksPreviouslyPresent = [bool]$hooksPreviouslyPresent
      Events = @($eventStates.ToArray())
    }
    Write-TextAtomic -Path $SettingsPath -Content $rendered -ExpectedContent $original
    Assert-AudnCodeSettingsQuiet `
      -SettingsPath $SettingsPath `
      -ExpectedContent $rendered `
      -HomePath $AudnCodeHomePath
    # Write-TextAtomic applies the original descriptor to the empty temporary
    # file before writing content, or a private descriptor for a new file, and
    # preserves it through the atomic swap. Reapplying a Get-Acl descriptor here
    # is redundant and can spuriously require SeSecurityPrivilege on Windows.
    return
  }
  throw "AudnCode settings kept changing while hooks were being installed at $SettingsPath."
}

function Get-AudnCodeHookObservationMarker {
  param(
    [string]$HomePath,
    [string]$MarkerPath
  )

  if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) { return $null }
  try {
    $canonicalHome = [IO.Path]::GetFullPath($HomePath)
    $marker = ConvertFrom-StrictJsonText -Text (Read-StrictUtf8Text -Path $MarkerPath)
    $schemaProperty = $marker.PSObject.Properties['schema']
    $versionProperty = $marker.PSObject.Properties['notifier_version']
    $shapeProperty = $marker.PSObject.Properties['hook_shape_version']
    $homeProperty = $marker.PSObject.Properties['audncode_home']
    $generationProperty = $marker.PSObject.Properties['generation']
    $installedProperty = $marker.PSObject.Properties['installed_unix_ms']
    if ([string](Get-ObjectValue -Object $marker -Name 'kind' -Default '') -ne 'codex-ntfy-audncode-hooks' -or
        $null -eq $schemaProperty -or
        ($schemaProperty.Value -isnot [int] -and $schemaProperty.Value -isnot [long]) -or
        [int64]$schemaProperty.Value -ne 1 -or
        $null -eq $versionProperty -or $versionProperty.Value -isnot [string] -or [string]$versionProperty.Value -ne $NotifierVersion -or
        $null -eq $shapeProperty -or
        ($shapeProperty.Value -isnot [int] -and $shapeProperty.Value -isnot [long]) -or
        [int64]$shapeProperty.Value -ne $AudnCodeHookShapeVersion -or
        $null -eq $homeProperty -or $homeProperty.Value -isnot [string] -or
        -not [string]::Equals(
          [IO.Path]::GetFullPath([string]$homeProperty.Value),
          $canonicalHome,
          [StringComparison]::OrdinalIgnoreCase
        ) -or
        $null -eq $generationProperty -or $generationProperty.Value -isnot [string] -or
        [string]$generationProperty.Value -notmatch '^[a-f0-9]{32}$' -or
        $null -eq $installedProperty -or
        ($installedProperty.Value -isnot [int] -and $installedProperty.Value -isnot [long]) -or
        [int64]$installedProperty.Value -le 0) { return $null }
    return [pscustomobject]@{
      generation = [string]$generationProperty.Value
      installed_unix_ms = [int64]$installedProperty.Value
    }
  } catch {
    return $null
  }
}

function Get-AudnCodeHookMarkerMutexName {
  param([string]$MarkerPath)

  $canonical = [IO.Path]::GetFullPath($MarkerPath).ToLowerInvariant()
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))
    # Global keeps two interactive Windows sessions using the same profile from
    # running overlapping installers against one AudnCode home.
    return 'Global\codex-ntfy-audn-marker-' + ([BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
  }
}

function Get-InstallerTransactionMutexName {
  param([string]$ScheduledTaskName = $TaskName)

  # The scheduled task name is machine-global. Use one machine-global lease for
  # every installer transaction so two Codex homes cannot concurrently inspect,
  # replace, or roll back that shared task (or the files it launches).
  $key = ('codex-ntfy-installer|scheduled-task|' + $ScheduledTaskName).ToLowerInvariant()
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key))
    return 'Global\codex-ntfy-install-transaction-' +
      ([BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
  }
}

function Enter-InstallerTransactionLock {
  param(
    [string]$ScheduledTaskName = $TaskName,
    [ValidateRange(1, 1800)]
    [int]$TimeoutSeconds = 600
  )

  $mutex = New-Object Threading.Mutex(
    $false,
    (Get-InstallerTransactionMutexName -ScheduledTaskName $ScheduledTaskName)
  )
  $acquired = $false
  try {
    try {
      $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
    } catch [Threading.AbandonedMutexException] {
      $acquired = $true
    }
    if (-not $acquired) {
      throw "Timed out waiting for the installer transaction lock for scheduled task $ScheduledTaskName."
    }
    return $mutex
  } catch {
    if ($acquired) { try { $mutex.ReleaseMutex() } catch { } }
    $mutex.Dispose()
    throw
  }
}

function Exit-InstallerTransactionLock {
  param([object]$Mutex)

  if ($null -eq $Mutex) { return }
  try {
    $Mutex.ReleaseMutex()
  } finally {
    $Mutex.Dispose()
  }
}

function Enter-AudnCodeHookMarkerLock {
  param(
    [string]$MarkerPath,
    [ValidateRange(1, 1800)]
    [int]$TimeoutSeconds = 30
  )

  $mutex = New-Object Threading.Mutex($false, (Get-AudnCodeHookMarkerMutexName -MarkerPath $MarkerPath))
  $acquired = $false
  try {
    try {
      $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
    } catch [Threading.AbandonedMutexException] {
      $acquired = $true
    }
    if (-not $acquired) { throw "Timed out waiting for the AudnCode hook marker lock at $MarkerPath." }
    return $mutex
  } catch {
    if ($acquired) { try { $mutex.ReleaseMutex() } catch { } }
    $mutex.Dispose()
    throw
  }
}

function Exit-AudnCodeHookMarkerLock {
  param([object]$Mutex)

  if ($null -eq $Mutex) { return }
  try {
    $Mutex.ReleaseMutex()
  } finally {
    $Mutex.Dispose()
  }
}

function Invoke-WithAudnCodeHookMarkerLock {
  param(
    [string]$MarkerPath,
    [scriptblock]$Action,
    [object[]]$Arguments = @()
  )

  $mutex = Enter-AudnCodeHookMarkerLock -MarkerPath $MarkerPath
  try {
    return & $Action @Arguments
  } finally {
    Exit-AudnCodeHookMarkerLock -Mutex $mutex
  }
}

function Ensure-AudnCodeHookObservationMarker {
  param(
    [string]$HomePath,
    [string]$MarkerPath,
    [bool]$ManagedHookEventsChanged
  )

  return Invoke-WithAudnCodeHookMarkerLock -MarkerPath $MarkerPath -Action {
    param($lockedHomePath, $lockedMarkerPath, $lockedManagedHookEventsChanged)
    # Capture the rollback value inside the same lease and immediately before
    # mutation. The earlier private backup is manual-recovery evidence only;
    # using it here could erase a generation committed by another installer.
    $previousPresent = Test-Path -LiteralPath $lockedMarkerPath -PathType Leaf
    $previousContent = if ($previousPresent) { Read-StrictUtf8Text -Path $lockedMarkerPath } else { '' }
    $existing = Get-AudnCodeHookObservationMarker -HomePath $lockedHomePath -MarkerPath $lockedMarkerPath
    if (-not $lockedManagedHookEventsChanged -and $null -ne $existing) {
      Protect-PrivatePath $lockedMarkerPath
      return [pscustomobject]@{
        rotated = $false
        generation = [string]$existing.generation
        installed_unix_ms = [int64]$existing.installed_unix_ms
      }
    }

    # Commit this only after settings.json contains the exact seven managed event
    # shapes. A host that predates this instant cannot be known to have observed
    # every memory-only CronCreate event and therefore remains fail closed.
    $generation = [Guid]::NewGuid().ToString('N')
    $installedUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $payload = [ordered]@{
      schema = 1
      kind = 'codex-ntfy-audncode-hooks'
      notifier_version = $NotifierVersion
      hook_shape_version = $AudnCodeHookShapeVersion
      audncode_home = [IO.Path]::GetFullPath($lockedHomePath)
      generation = $generation
      installed_unix_ms = $installedUnixMs
    }
    $renderedMarker = ($payload | ConvertTo-Json -Depth 8) + [Environment]::NewLine
    $markerWritten = $false
    try {
      Write-TextAtomic -Path $lockedMarkerPath -Content $renderedMarker
      $markerWritten = $true
      Protect-PrivatePath $lockedMarkerPath
      $observed = Get-AudnCodeHookObservationMarker -HomePath $lockedHomePath -MarkerPath $lockedMarkerPath
      if ($null -eq $observed -or [string]$observed.generation -ne $generation -or
          [int64]$observed.installed_unix_ms -ne $installedUnixMs) {
        throw 'AudnCode hook observation marker could not be verified after installation.'
      }
    } catch {
      $markerError = $_
      if ($markerWritten) {
        try {
          if (Test-Path -LiteralPath $lockedMarkerPath -PathType Leaf) {
            $currentContent = Read-StrictUtf8Text -Path $lockedMarkerPath
            if ([string]::Equals($currentContent, $renderedMarker, [StringComparison]::Ordinal)) {
              if ($previousPresent) {
                # The exact installed payload is the CAS guard. Write-TextAtomic
                # retains the pre-existing destination ACL while restoring bytes.
                Write-TextAtomic `
                  -Path $lockedMarkerPath `
                  -Content $previousContent `
                  -ExpectedContent $renderedMarker
              } else {
                Remove-Item -LiteralPath $lockedMarkerPath -Force -ErrorAction Stop
              }
            } else {
              Write-Warning 'AudnCode hook observation marker changed during failed installation; internal rollback preserved that concurrent value.'
            }
          } elseif ($previousPresent) {
            Write-Warning 'AudnCode hook observation marker disappeared during failed installation; internal rollback preserved that concurrent deletion.'
          }
        } catch {
          throw "AudnCode hook observation marker failed after mutation, and its internal rollback also failed. $($markerError.Exception.Message) Rollback: $($_.Exception.Message)"
        }
      }
      throw $markerError
    }
    return [pscustomobject]@{
      rotated = $true
      generation = $generation
      installed_unix_ms = $installedUnixMs
      installed_content = $renderedMarker
      previous_present = [bool]$previousPresent
      previous_content = $previousContent
    }
  } -Arguments @($HomePath, $MarkerPath, $ManagedHookEventsChanged)
}

function Restore-AudnCodeHookObservationMarker {
  param(
    [string]$MarkerPath,
    [object]$MutationState
  )

  if ($null -eq $MutationState -or
      -not [bool](Get-ObjectValue -Object $MutationState -Name 'rotated' -Default $false)) { return }
  [void](Invoke-WithAudnCodeHookMarkerLock -MarkerPath $MarkerPath -Action {
      param($lockedMarkerPath, $lockedMutationState)
      if (-not (Test-Path -LiteralPath $lockedMarkerPath -PathType Leaf)) {
        Write-Warning 'AudnCode hook observation marker disappeared after installation; rollback preserved that concurrent change.'
        return
      }
      $current = Read-StrictUtf8Text -Path $lockedMarkerPath
      $installed = [string](Get-ObjectValue -Object $lockedMutationState -Name 'installed_content' -Default '')
      if ([string]::IsNullOrWhiteSpace($installed) -or
          -not [string]::Equals($current, $installed, [StringComparison]::Ordinal)) {
        Write-Warning 'AudnCode hook observation marker changed after installation; rollback preserved the newer value.'
        return
      }
      if ([bool](Get-ObjectValue -Object $lockedMutationState -Name 'previous_present' -Default $false)) {
        $previousContentProperty = $lockedMutationState.PSObject.Properties['previous_content']
        if ($null -eq $previousContentProperty -or $previousContentProperty.Value -isnot [string]) {
          throw 'AudnCode hook marker rollback state is missing its exact previous content.'
        }
        Write-TextAtomic -Path $lockedMarkerPath -Content ([string]$previousContentProperty.Value)
        Protect-PrivatePath $lockedMarkerPath
      } else {
        Remove-Item -LiteralPath $lockedMarkerPath -Force -ErrorAction Stop
      }
    } -Arguments @($MarkerPath, $MutationState))
}

function Restore-AudnCodeHooks {
  param(
    [string]$SettingsPath,
    [object]$MutationState
  )

  if ($null -eq $MutationState -or -not [bool](Get-ObjectValue -Object $MutationState -Name 'Applied' -Default $false)) {
    return
  }
  for ($attempt = 1; $attempt -le 8; $attempt++) {
    if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
      Write-Warning 'AudnCode settings disappeared after installation; rollback preserved that concurrent change.'
      return
    }
    $settingsAcl = try { Get-Acl -LiteralPath $SettingsPath } catch { $null }
    $original = Read-StrictUtf8Text -Path $SettingsPath
    try { $document = ConvertFrom-StrictJsonText -Text $original } catch {
      throw "Invalid JSON in ${SettingsPath} during rollback: $($_.Exception.Message)"
    }
    if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
      throw "$SettingsPath must contain a JSON object during rollback."
    }
    $hooksProperty = $document.PSObject.Properties['hooks']
    if ($null -eq $hooksProperty -or $null -eq $hooksProperty.Value -or
        $hooksProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
      Write-Warning 'AudnCode hooks changed after installation; rollback preserved the newer value.'
      return
    }
    $hookEvents = $hooksProperty.Value
    $changed = $false
    foreach ($eventState in @((Get-ObjectValue -Object $MutationState -Name 'Events' -Default @()))) {
      $eventName = [string](Get-ObjectValue -Object $eventState -Name 'Name' -Default '')
      if ([string]::IsNullOrWhiteSpace($eventName)) { continue }
      $currentProperty = $hookEvents.PSObject.Properties[$eventName]
      $currentPresent = $null -ne $currentProperty
      $installedPresent = [bool](Get-ObjectValue -Object $eventState -Name 'InstalledPresent' -Default $false)
      $currentJson = if ($currentPresent) {
        ConvertTo-Json -InputObject $currentProperty.Value -Depth 100 -Compress
      } else { '' }
      $installedJson = [string](Get-ObjectValue -Object $eventState -Name 'InstalledJson' -Default '')
      if ($currentPresent -ne $installedPresent -or $currentJson -ne $installedJson) {
        Write-Warning "AudnCode hooks.$eventName changed after installation; rollback preserved that event."
        continue
      }

      if ([bool](Get-ObjectValue -Object $eventState -Name 'BeforePresent' -Default $false)) {
        $beforeJson = [string](Get-ObjectValue -Object $eventState -Name 'BeforeJson' -Default '[]')
        # Windows PowerShell 5.1 emits a top-level JSON array as one pipeline
        # object. Assign first, then array-wrap, to avoid creating object[][].
        $parsedBeforeValue = ConvertFrom-StrictJsonText -Text $beforeJson
        $beforeValue = @($parsedBeforeValue)
        if ($null -eq $currentProperty) {
          Add-Member -InputObject $hookEvents -MemberType NoteProperty -Name $eventName -Value $beforeValue
        } else {
          $currentProperty.Value = $beforeValue
        }
      } else {
        $hookEvents.PSObject.Properties.Remove($eventName)
      }
      $changed = $true
    }
    if (-not $changed) { return }

    if (-not [bool](Get-ObjectValue -Object $MutationState -Name 'HooksPreviouslyPresent' -Default $true) -and
        @($hookEvents.PSObject.Properties).Count -eq 0) {
      $document.PSObject.Properties.Remove('hooks')
    }
    $filePreviouslyPresent = [bool](Get-ObjectValue -Object $MutationState -Name 'FilePreviouslyPresent' -Default $true)
    $removeFile = -not $filePreviouslyPresent -and @($document.PSObject.Properties).Count -eq 0
    $rendered = if ($removeFile) { '' } else { ($document | ConvertTo-Json -Depth 100) + [Environment]::NewLine }
    $current = if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
      Read-StrictUtf8Text -Path $SettingsPath
    } else { '' }
    if (-not [string]::Equals($current, $original, [System.StringComparison]::Ordinal)) {
      Start-Sleep -Milliseconds (20 * $attempt)
      continue
    }
    if ($removeFile) {
      Remove-Item -LiteralPath $SettingsPath -Force -ErrorAction Stop
    } else {
      Write-TextAtomic -Path $SettingsPath -Content $rendered -ExpectedContent $original
      if ($null -ne $settingsAcl) {
        try { Set-Acl -LiteralPath $SettingsPath -AclObject $settingsAcl } catch {
          throw "AudnCode settings were rolled back, but their ACL could not be restored: $($_.Exception.Message)"
        }
      }
    }
    return
  }
  throw "AudnCode settings kept changing while hooks were being rolled back at $SettingsPath."
}

function Try-RecoverStaleAudnCodeGlobalConfigLock {
  param(
    [string]$LockPath,
    [ValidateRange(2000, 60000)]
    [int]$StaleMs = 10000,
    [ValidateRange(50, 1000)]
    [int]$ConfirmationMs = 125
  )

  if (-not (Test-Path -LiteralPath $LockPath)) { return $true }
  if (-not (Test-Path -LiteralPath $LockPath -PathType Container)) {
    throw "AudnCode lock path $LockPath exists but is not a directory."
  }
  try { $first = Get-Item -LiteralPath $LockPath -Force -ErrorAction Stop } catch {
    if (-not (Test-Path -LiteralPath $LockPath)) { return $true }
    throw
  }
  if (($first.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "AudnCode lock path $LockPath is a reparse point; refusing stale-lock recovery."
  }
  if ($first.LastWriteTimeUtc -ge [DateTime]::UtcNow.AddMilliseconds(-$StaleMs)) {
    return $false
  }

  # A proper-lockfile owner refreshes mtime every stale/2 (5 s by default).
  # Re-stat after a confirmation window so a resumed/slow heartbeat wins.
  Start-Sleep -Milliseconds $ConfirmationMs
  try { $second = Get-Item -LiteralPath $LockPath -Force -ErrorAction Stop } catch {
    if (-not (Test-Path -LiteralPath $LockPath)) { return $true }
    throw
  }
  if (($second.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "AudnCode lock path $LockPath became a reparse point; refusing stale-lock recovery."
  }
  $sameSnapshot = $first.CreationTimeUtc.Ticks -eq $second.CreationTimeUtc.Ticks -and
    $first.LastWriteTimeUtc.Ticks -eq $second.LastWriteTimeUtc.Ticks
  if (-not $sameSnapshot -or
      $second.LastWriteTimeUtc -ge [DateTime]::UtcNow.AddMilliseconds(-$StaleMs)) {
    return $false
  }

  # Rename first, then verify the moved directory. This makes deletion target
  # the object we inspected rather than any replacement created at LockPath.
  $quarantinePath = "${LockPath}.recover-$([Guid]::NewGuid().ToString('N'))"
  try {
    [System.IO.Directory]::Move($LockPath, $quarantinePath)
  } catch {
    if (-not (Test-Path -LiteralPath $LockPath)) { return $true }
    return $false
  }

  $moved = Get-Item -LiteralPath $quarantinePath -Force -ErrorAction Stop
  $movedIsInspectedLock = $moved.CreationTimeUtc.Ticks -eq $second.CreationTimeUtc.Ticks -and
    $moved.LastWriteTimeUtc.Ticks -eq $second.LastWriteTimeUtc.Ticks -and
    ($moved.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0
  if (-not $movedIsInspectedLock) {
    if (-not (Test-Path -LiteralPath $LockPath)) {
      try { [System.IO.Directory]::Move($quarantinePath, $LockPath) } catch { }
    }
    throw "AudnCode lock ownership changed during stale-lock recovery at $LockPath; no replacement was deleted."
  }

  try {
    [System.IO.Directory]::Delete($quarantinePath, $false)
  } catch {
    if (-not (Test-Path -LiteralPath $LockPath) -and (Test-Path -LiteralPath $quarantinePath -PathType Container)) {
      try { [System.IO.Directory]::Move($quarantinePath, $LockPath) } catch { }
    }
    throw "Could not remove the confirmed stale AudnCode lock at ${LockPath}: $($_.Exception.Message)"
  }
  return $true
}

function Invoke-WithAudnCodeGlobalConfigLock {
  param(
    [string]$GlobalConfigPath,
    [scriptblock]$Action,
    [ValidateRange(1000, 60000)]
    [int]$TimeoutMs = 20000
  )

  if ($null -eq ('CodexNtfy.AudnCodeLockLease' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;

namespace CodexNtfy {
  public sealed class AudnCodeLockLease : IDisposable {
    private readonly string path;
    private readonly long creationTicks;
    private readonly Timer timer;
    private int compromised;
    private int disposed;

    public AudnCodeLockLease(string path, long creationTicks, int heartbeatMs) {
      this.path = path;
      this.creationTicks = creationTicks;
      Touch(null);
      this.timer = new Timer(Touch, null, heartbeatMs, heartbeatMs);
    }

    private void Touch(object state) {
      try {
        if (!Directory.Exists(path) || Directory.GetCreationTimeUtc(path).Ticks != creationTicks) {
          Interlocked.Exchange(ref compromised, 1);
          return;
        }
        Directory.SetLastWriteTimeUtc(path, DateTime.UtcNow);
      } catch {
        Interlocked.Exchange(ref compromised, 1);
      }
    }

    public void Dispose() {
      if (Interlocked.Exchange(ref disposed, 1) != 0) return;
      using (var stopped = new ManualResetEvent(false)) {
        if (timer.Dispose(stopped)) stopped.WaitOne();
      }
      if (Interlocked.CompareExchange(ref compromised, 0, 0) != 0 ||
          !Directory.Exists(path) || Directory.GetCreationTimeUtc(path).Ticks != creationTicks) {
        throw new IOException("The AudnCode configuration lock was replaced or lost while held.");
      }
      Directory.Delete(path, false);
    }
  }
}
'@
  }

  $parent = Split-Path -Parent $GlobalConfigPath
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
  # AudnCode uses proper-lockfile with this explicit lockfilePath. Its lock is
  # an atomic mkdir, not a lock file, so using the same directory serializes
  # this installer with live AudnCode configuration writes.
  $lockPath = "${GlobalConfigPath}.lock"
  $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
  $lease = $null
  $acquired = $false
  while (-not $acquired) {
    $createdThisAttempt = $false
    try {
      New-Item -ItemType Directory -Path $lockPath -ErrorAction Stop | Out-Null
      $createdThisAttempt = $true
      # proper-lockfile treats a lock as stale after 10 seconds and refreshes
      # its directory mtime periodically. Mirror that heartbeat. A randomized
      # creation time is an ownership token that lets release refuse to delete
      # a replacement lock without leaving marker files that break rmdir.
      $tokenTicks = [Convert]::ToInt64([Guid]::NewGuid().ToString('N').Substring(0, 12), 16)
      $requestedCreation = [DateTime]::SpecifyKind([DateTime]'2000-01-01', [DateTimeKind]::Utc).AddTicks($tokenTicks)
      [System.IO.Directory]::SetCreationTimeUtc($lockPath, $requestedCreation)
      $ownerCreation = [System.IO.Directory]::GetCreationTimeUtc($lockPath)
      $lease = New-Object CodexNtfy.AudnCodeLockLease($lockPath, $ownerCreation.Ticks, 2000)
      $acquired = $true
    } catch {
      $acquireError = $_
      if ($null -ne $lease) {
        try { $lease.Dispose() } catch { }
        $lease = $null
        $createdThisAttempt = $false
      } elseif ($createdThisAttempt -and (Test-Path -LiteralPath $lockPath -PathType Container)) {
        try { Remove-Item -LiteralPath $lockPath -Force -ErrorAction Stop } catch { }
        $createdThisAttempt = $false
      }
      if (Test-Path -LiteralPath $lockPath) {
        if (-not (Test-Path -LiteralPath $lockPath -PathType Container)) {
          throw "AudnCode lock path $lockPath exists but is not a directory."
        }
        if (Try-RecoverStaleAudnCodeGlobalConfigLock -LockPath $lockPath -StaleMs 10000) {
          continue
        }
      } elseif ($acquireError.FullyQualifiedErrorId -notmatch '(?i)(?:Directory|Resource|Item).*Exist') {
        throw "Could not acquire the AudnCode global configuration lock at ${lockPath}: $($acquireError.Exception.Message)"
      }
      if ([DateTime]::UtcNow -ge $deadline) {
        throw "Timed out waiting for AudnCode to release its global configuration lock at $lockPath."
      }
      Start-Sleep -Milliseconds 100
    }
  }

  $actionError = $null
  $result = $null
  try {
    $result = & $Action
  } catch {
    $actionError = $_
  }
  $releaseError = $null
  try {
    if ($null -ne $lease) { $lease.Dispose() }
  } catch {
    $releaseError = $_
  }
  if ($null -ne $actionError) {
    if ($null -ne $releaseError) {
      Write-Warning "AudnCode lock release also failed: $($releaseError.Exception.Message)"
    }
    throw $actionError
  }
  if ($null -ne $releaseError) { throw $releaseError }
  return $result
}

function Test-AudnCodeThresholdNumber {
  param(
    [object]$Value,
    [int]$Expected
  )

  if ($null -eq $Value) { return $false }
  $typeCode = [Type]::GetTypeCode($Value.GetType())
  if ($typeCode -notin @(
      [TypeCode]::SByte, [TypeCode]::Byte,
      [TypeCode]::Int16, [TypeCode]::UInt16,
      [TypeCode]::Int32, [TypeCode]::UInt32,
      [TypeCode]::Int64, [TypeCode]::UInt64,
      [TypeCode]::Single, [TypeCode]::Double, [TypeCode]::Decimal
    )) {
    return $false
  }
  try { return [decimal]$Value -eq [decimal]$Expected } catch { return $false }
}

function Ensure-AudnCodeIdleThreshold {
  param(
    [string]$GlobalConfigPath,
    [int]$ThresholdMs,
    [Parameter(Mandatory = $true)]
    [ref]$MutationState
  )

  $MutationState.Value = $null
  $operation = [pscustomobject]@{
    DidWrite = $false
    ConfigAcl = $null
  }
  $null = Invoke-WithAudnCodeGlobalConfigLock -GlobalConfigPath $GlobalConfigPath -Action {
    $filePreviouslyPresent = Test-Path -LiteralPath $GlobalConfigPath -PathType Leaf
    $operation.ConfigAcl = if ($filePreviouslyPresent) {
      try { Get-Acl -LiteralPath $GlobalConfigPath } catch { $null }
    } else { $null }
    $original = if ($filePreviouslyPresent) {
      Read-StrictUtf8Text -Path $GlobalConfigPath
    } else { '' }
    try {
      $document = if ([string]::IsNullOrWhiteSpace($original)) {
        [pscustomobject][ordered]@{}
      } else {
        ConvertFrom-StrictJsonText -Text $original
      }
    } catch {
      throw "Invalid JSON in ${GlobalConfigPath}: $($_.Exception.Message)"
    }
    if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
      throw "$GlobalConfigPath must contain a JSON object."
    }

    $property = $document.PSObject.Properties['messageIdleNotifThresholdMs']
    if ($null -ne $property -and
        (Test-AudnCodeThresholdNumber -Value $property.Value -Expected $ThresholdMs)) { return }

    $MutationState.Value = [pscustomobject]@{
      Applied = $true
      FilePreviouslyPresent = [bool]$filePreviouslyPresent
      HadProperty = $null -ne $property
      PreviousValue = if ($null -ne $property) { $property.Value } else { $null }
      InstalledValue = $ThresholdMs
    }
    if ($null -eq $property) {
      Add-Member -InputObject $document -MemberType NoteProperty -Name 'messageIdleNotifThresholdMs' -Value $ThresholdMs
    } else {
      $property.Value = $ThresholdMs
    }
    Write-TextAtomic -Path $GlobalConfigPath -Content (($document | ConvertTo-Json -Depth 100) + [Environment]::NewLine)
    $operation.DidWrite = $true
  }

  if ($operation.DidWrite -and $null -ne $operation.ConfigAcl) {
    try { Set-Acl -LiteralPath $GlobalConfigPath -AclObject $operation.ConfigAcl } catch {
      throw "AudnCode global configuration was updated, but its original ACL could not be restored: $($_.Exception.Message)"
    }
  } elseif ($operation.DidWrite) {
    Protect-PrivatePath $GlobalConfigPath
  }
}

function Restore-AudnCodeIdleThreshold {
  param(
    [string]$GlobalConfigPath,
    [object]$MutationState
  )

  if ($null -eq $MutationState -or -not [bool](Get-ObjectValue -Object $MutationState -Name 'Applied' -Default $false)) {
    return
  }
  $operation = [pscustomobject]@{
    DidWrite = $false
    ConfigAcl = $null
  }
  $null = Invoke-WithAudnCodeGlobalConfigLock -GlobalConfigPath $GlobalConfigPath -Action {
    if (-not (Test-Path -LiteralPath $GlobalConfigPath -PathType Leaf)) {
      Write-Warning 'AudnCode global configuration disappeared after installation; rollback left that concurrent change untouched.'
      return
    }
    $operation.ConfigAcl = try { Get-Acl -LiteralPath $GlobalConfigPath } catch { $null }
    $original = Read-StrictUtf8Text -Path $GlobalConfigPath
    try {
      $document = if ([string]::IsNullOrWhiteSpace($original)) {
        [pscustomobject][ordered]@{}
      } else {
        ConvertFrom-StrictJsonText -Text $original
      }
    } catch {
      throw "Invalid JSON in ${GlobalConfigPath} during rollback: $($_.Exception.Message)"
    }
    if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
      throw "$GlobalConfigPath must contain a JSON object during rollback."
    }

    $property = $document.PSObject.Properties['messageIdleNotifThresholdMs']
    $installedValue = [int](Get-ObjectValue -Object $MutationState -Name 'InstalledValue' -Default -1)
    if ($null -eq $property -or -not (Test-AudnCodeThresholdNumber -Value $property.Value -Expected $installedValue)) {
      Write-Warning 'AudnCode changed its idle threshold after installation; rollback preserved the newer value.'
      return
    }

    if ([bool](Get-ObjectValue -Object $MutationState -Name 'HadProperty' -Default $false)) {
      $property.Value = Get-ObjectValue -Object $MutationState -Name 'PreviousValue' -Default $null
    } else {
      $document.PSObject.Properties.Remove('messageIdleNotifThresholdMs')
    }
    $filePreviouslyPresent = [bool](Get-ObjectValue -Object $MutationState -Name 'FilePreviouslyPresent' -Default $true)
    if (-not $filePreviouslyPresent -and @($document.PSObject.Properties).Count -eq 0) {
      Remove-Item -LiteralPath $GlobalConfigPath -Force -ErrorAction Stop
      return
    }
    Write-TextAtomic -Path $GlobalConfigPath -Content (($document | ConvertTo-Json -Depth 100) + [Environment]::NewLine)
    $operation.DidWrite = $true
  }

  if ($operation.DidWrite -and $null -ne $operation.ConfigAcl) {
    try { Set-Acl -LiteralPath $GlobalConfigPath -AclObject $operation.ConfigAcl } catch {
      throw "AudnCode global configuration was rolled back, but its ACL could not be restored: $($_.Exception.Message)"
    }
  }
}

function Assert-AudnCodeIdleThreshold {
  param(
    [string]$GlobalConfigPath,
    [int]$ThresholdMs
  )

  $null = Invoke-WithAudnCodeGlobalConfigLock -GlobalConfigPath $GlobalConfigPath -Action {
    if (-not (Test-Path -LiteralPath $GlobalConfigPath -PathType Leaf)) {
      throw "AudnCode global configuration is missing at $GlobalConfigPath."
    }
    $original = Read-StrictUtf8Text -Path $GlobalConfigPath
    try { $document = ConvertFrom-StrictJsonText -Text $original } catch {
      throw "Invalid JSON in ${GlobalConfigPath}: $($_.Exception.Message)"
    }
    if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
      throw "$GlobalConfigPath must contain a JSON object."
    }
    $property = $document.PSObject.Properties['messageIdleNotifThresholdMs']
    if ($null -eq $property -or
        -not (Test-AudnCodeThresholdNumber -Value $property.Value -Expected $ThresholdMs)) {
      throw 'AudnCode idle notification threshold did not persist.'
    }
  }
}

function Test-AudnCodeEnvironmentTruthy {
  param([string]$Value)

  if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
  return $Value.Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'on')
}

function Resolve-AudnCodeGlobalConfigPath {
  param([string]$HomePath)

  $configJson = Join-Path $HomePath '.config.json'
  if (Test-Path -LiteralPath $configJson -PathType Leaf) { return $configJson }

  # Mirror AudnCode's fileSuffixForOauthConfig(): each OAuth environment has
  # a separate effective global-config file.
  $oauthSuffix = if (-not [string]::IsNullOrEmpty($env:CLAUDE_CODE_CUSTOM_OAUTH_URL)) {
    '-custom-oauth'
  } elseif ($env:USER_TYPE -ceq 'ant' -and
      (Test-AudnCodeEnvironmentTruthy -Value $env:USE_LOCAL_OAUTH)) {
    '-local-oauth'
  } elseif ($env:USER_TYPE -ceq 'ant' -and
      (Test-AudnCodeEnvironmentTruthy -Value $env:USE_STAGING_OAUTH)) {
    '-staging-oauth'
  } else { '' }
  $openClaudeJson = Join-Path $HomePath ".openclaude${oauthSuffix}.json"
  $legacyClaudeJson = Join-Path $HomePath ".claude${oauthSuffix}.json"
  if (-not (Test-Path -LiteralPath $openClaudeJson) -and
      (Test-Path -LiteralPath $legacyClaudeJson -PathType Leaf)) {
    return $legacyClaudeJson
  }
  return $openClaudeJson
}

function Get-InstalledClaudeCodeVersions {
  $candidates = New-Object 'System.Collections.Generic.List[object]'
  $command = Get-Command claude -ErrorAction SilentlyContinue
  if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
    $candidates.Add([pscustomobject]@{ surface = 'PATH'; path = [string]$command.Source })
  }
  foreach ($location in @(
      [pscustomobject]@{ surface = 'Claude Desktop'; root = (Join-Path $env:APPDATA 'Claude\claude-code') },
      [pscustomobject]@{ surface = 'Claude Desktop (Store)'; root = (Join-Path $env:LOCALAPPDATA 'Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code') }
    )) {
    $root = $location.root
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
    Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
      Sort-Object { try { [version]$_.Name } catch { [version]'0.0' } } -Descending |
      ForEach-Object {
        $candidate = Join-Path $_.FullName 'claude.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
          $candidates.Add([pscustomobject]@{ surface = $location.surface; path = $candidate })
        }
      }
  }
  foreach ($location in @(
      [pscustomobject]@{ surface = 'VS Code'; root = (Join-Path $env:USERPROFILE '.vscode\extensions') },
      [pscustomobject]@{ surface = 'VS Code Insiders'; root = (Join-Path $env:USERPROFILE '.vscode-insiders\extensions') },
      [pscustomobject]@{ surface = 'Cursor'; root = (Join-Path $env:USERPROFILE '.cursor\extensions') }
    )) {
    $extensionsRoot = $location.root
    if (-not (Test-Path -LiteralPath $extensionsRoot -PathType Container)) { continue }
    Get-ChildItem -LiteralPath $extensionsRoot -Directory -Filter 'anthropic.claude-code-*' -ErrorAction SilentlyContinue |
      ForEach-Object {
        $candidate = Join-Path $_.FullName 'resources\native-binary\claude.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
          $candidates.Add([pscustomobject]@{ surface = $location.surface; path = $candidate })
        }
      }
  }
  $selected = New-Object 'System.Collections.Generic.List[object]'
  foreach ($surfaceGroup in @($candidates | Sort-Object surface, path -Unique | Group-Object surface)) {
    $detected = New-Object 'System.Collections.Generic.List[object]'
    foreach ($candidate in @($surfaceGroup.Group)) {
      try {
        $versionText = (& $candidate.path --version 2>$null | Select-Object -First 1)
        if ([string]$versionText -match '(?<!\d)(\d+\.\d+\.\d+)(?!\d)') {
          $detected.Add([pscustomobject]@{
              surface = [string]$candidate.surface
              version = [version]$Matches[1]
              path = [string]$candidate.path
            })
        }
      } catch { }
    }
    if ($detected.Count -gt 0) {
      $selected.Add(@($detected | Sort-Object version -Descending | Select-Object -First 1)[0])
    } else {
      $firstCandidate = @($surfaceGroup.Group | Select-Object -First 1)[0]
      $selected.Add([pscustomobject]@{
          surface = [string]$firstCandidate.surface
          version = $null
          path = [string]$firstCandidate.path
        })
    }
  }
  return @($selected | Sort-Object surface)
}

function Assert-ClaudeCodeVersions {
  param([object[]]$Installations)

  $minimum = [version]'2.1.198'
  $unknown = @($Installations | Where-Object { $null -eq $_.version })
  if ($unknown.Count -gt 0) {
    $details = @($unknown | ForEach-Object { "$($_.surface) at $($_.path)" }) -join '; '
    throw "Could not determine the Claude Code version for: $details. Version $minimum or newer is required."
  }
  $unsupported = @($Installations | Where-Object { $_.version -lt $minimum })
  if ($unsupported.Count -gt 0) {
    $details = @($unsupported | ForEach-Object { "$($_.surface) $($_.version) at $($_.path)" }) -join '; '
    throw "Claude Code version $minimum or newer is required on every detected surface. Upgrade: $details"
  }
  foreach ($installation in $Installations) {
    Write-Status "Detected Claude Code $($installation.version) for $($installation.surface)."
  }
}

function Restore-ClaudeCodeSettings {
  param(
    [string]$SettingsPath,
    [string]$BackupPath,
    [bool]$PreviouslyPresent
  )

  $saved = Join-Path $BackupPath 'claude-settings.json'
  if (Test-Path -LiteralPath $saved) {
    $stage = "$SettingsPath.rollback"
    Copy-Item -LiteralPath $saved -Destination $stage -Force
    Move-Item -LiteralPath $stage -Destination $SettingsPath -Force
  } elseif (-not $PreviouslyPresent) {
    Remove-Item -LiteralPath $SettingsPath -Force -ErrorAction SilentlyContinue
  }
}

function Restore-AudnCodeConfiguration {
  param(
    [string]$SettingsPath,
    [string]$GlobalConfigPath,
    [object]$SettingsMutation,
    [object]$GlobalConfigMutation,
    [string]$HookMarkerPath,
    [object]$HookMarkerMutation
  )

  # These files are independent live configuration surfaces. Always attempt
  # both field-level rollbacks so a failure on one cannot strand the other.
  $rollbackErrors = New-Object 'System.Collections.Generic.List[string]'
  try {
    Restore-AudnCodeHooks -SettingsPath $SettingsPath -MutationState $SettingsMutation
  } catch {
    $rollbackErrors.Add("settings: $($_.Exception.Message)")
  }
  try {
    Restore-AudnCodeIdleThreshold `
      -GlobalConfigPath $GlobalConfigPath `
      -MutationState $GlobalConfigMutation
  } catch {
    $rollbackErrors.Add("global config: $($_.Exception.Message)")
  }
  try {
    Restore-AudnCodeHookObservationMarker `
      -MarkerPath $HookMarkerPath `
      -MutationState $HookMarkerMutation
  } catch {
    $rollbackErrors.Add("hook observation marker: $($_.Exception.Message)")
  }
  if ($rollbackErrors.Count -gt 0) {
    throw ($rollbackErrors -join '; ')
  }
}

function Restore-WindowsInstallation {
  param(
    [string]$HomePath,
    [string]$BackupPath,
    [string[]]$PreviouslyPresent,
    [bool]$TaskPreviouslyPresent,
    [bool]$TaskWasRunning
  )

  if (-not $SkipScheduledTask) {
    Stop-LegacyTask -HomePath $HomePath
  }
  foreach ($name in @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'config.toml', 'hooks.json', 'ntfy-config.json')) {
    $saved = Join-Path $BackupPath $name
    $target = Join-Path $HomePath $name
    if (Test-Path -LiteralPath $saved) {
      $stage = "$target.rollback"
      Copy-Item -LiteralPath $saved -Destination $stage -Force
      Move-Item -LiteralPath $stage -Destination $target -Force
    } elseif ($name -notin $PreviouslyPresent) {
      Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
    }
  }
  foreach ($privateName in @('config.toml', 'hooks.json', 'ntfy-config.json')) {
    Protect-PrivatePath (Join-Path $HomePath $privateName)
  }

  if (-not $SkipScheduledTask) {
    $taskXml = Join-Path $BackupPath 'CodexNtfyWatcher.xml'
    if ($TaskPreviouslyPresent -and (Test-Path -LiteralPath $taskXml)) {
      $taskXmlContent = (Read-StrictUtf8Text -Path $taskXml) -replace '^\s*<\?xml[^?]*\?>', ''
      Register-ScheduledTask -TaskName $TaskName -Xml $taskXmlContent -Force | Out-Null
      if ($TaskWasRunning) { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
    } elseif (-not $TaskPreviouslyPresent) {
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
  }
}

function Stop-LegacyTask {
  param(
    [string]$HomePath,
    [ValidateRange(0, 120000)]
    [int]$GraceMilliseconds = 10000,
    [ValidateRange(0, 300000)]
    [int]$ExitWaitMilliseconds = 120000
  )

  $canonicalHome = [IO.Path]::GetFullPath($HomePath)
  try {
    $scheduledTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $scheduledTask -and
        (Test-OwnedScheduledTask -Task $scheduledTask -HomePath $canonicalHome)) {
      & schtasks.exe /End /TN $TaskName 2>$null | Out-Null
    }
  } catch {
    # It may not exist yet.
  }
  $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds($GraceMilliseconds)
  $watchers = @()
  do {
    try {
      $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object -First 16385)
    } catch {
      Write-Warning "Could not inspect existing notifier processes safely; close the worker for $canonicalHome manually. $($_.Exception.Message)"
      return
    }
    if ($snapshot.Count -gt 16384) {
      Write-Warning "Too many processes exist to verify notifier ownership safely; close the worker for $canonicalHome manually."
      return
    }
    $watchers = @($snapshot | Where-Object {
        Test-LegacyNotifierProcessShape -Process $_ -HomePath $canonicalHome
      })
    if ($watchers.Count -eq 0) {
      return
    }
    if ([DateTimeOffset]::UtcNow -ge $deadline) { break }
    Start-Sleep -Milliseconds 250
  } while ($true)

  $ownedProcesses = New-Object 'System.Collections.Generic.List[object]'
  foreach ($process in $watchers) {
    $createdUtc = Get-CimProcessCreationUtc -Process $process
    if ($null -eq $createdUtc) { continue }
    $ownedProcesses.Add([pscustomobject]@{
        pid = [int]$process.ProcessId
        creation_ticks = [int64]$createdUtc.Ticks
        command_line = [string]$process.CommandLine
      })
  }

  foreach ($ownedProcess in $ownedProcesses) {
    try {
      # Requery immediately before Stop-Process. A recycled PID, changed command
      # line, or different installed path is never treated as our worker.
      $currentRows = @(Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ownedProcess.pid) -ErrorAction Stop)
      if ($currentRows.Count -ne 1) { continue }
      $current = $currentRows[0]
      $currentCreatedUtc = Get-CimProcessCreationUtc -Process $current
      if ($null -eq $currentCreatedUtc -or
          [int64]$currentCreatedUtc.Ticks -ne [int64]$ownedProcess.creation_ticks -or
          -not [string]::Equals(
            [string]$current.CommandLine,
            [string]$ownedProcess.command_line,
            [StringComparison]::Ordinal
          ) -or
          -not (Test-LegacyNotifierProcessShape -Process $current -HomePath $canonicalHome)) {
        continue
      }
      Stop-Process -Id ([int]$ownedProcess.pid) -Force -ErrorAction Stop
    } catch {
      # A process that exits between the final identity query and Stop-Process is
      # already in the desired state. The lifetime-aware wait below decides.
    }
  }

  $forcedDeadline = [DateTimeOffset]::UtcNow.AddMilliseconds($ExitWaitMilliseconds)
  do {
    $alive = New-Object 'System.Collections.Generic.List[object]'
    foreach ($ownedProcess in $ownedProcesses) {
      try {
        $currentRows = @(Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ownedProcess.pid) -ErrorAction Stop)
        if ($currentRows.Count -ne 1) { continue }
        $currentCreatedUtc = Get-CimProcessCreationUtc -Process $currentRows[0]
        if ($null -ne $currentCreatedUtc -and
            [int64]$currentCreatedUtc.Ticks -eq [int64]$ownedProcess.creation_ticks) {
          $alive.Add($ownedProcess)
        }
      } catch {
      }
    }
    if ($alive.Count -eq 0) { return }
    if ([DateTimeOffset]::UtcNow -ge $forcedDeadline) { break }
    Start-Sleep -Milliseconds 250
  } while ($true)
  $aliveIds = @($alive | ForEach-Object { $_.pid })
  throw "Could not stop existing notifier process(es) for ${canonicalHome}: $($aliveIds -join ', ')."
}

function Get-CimProcessCreationUtc {
  param([object]$Process)

  try {
    $value = $Process.CreationDate
    if ($value -is [DateTime]) { return ([DateTime]$value).ToUniversalTime() }
    if ($value -is [DateTimeOffset]) { return ([DateTimeOffset]$value).UtcDateTime }
    if ($value -is [string] -and -not [string]::IsNullOrWhiteSpace($value)) {
      return [Management.ManagementDateTimeConverter]::ToDateTime($value).ToUniversalTime()
    }
  } catch {
  }
  return $null
}

function Get-PowerShellCommandTokenPattern {
  param([string]$Value)

  $raw = [regex]::Escape($Value)
  $singleQuoted = [regex]::Escape($Value.Replace("'", "''"))
  return "(?:'$singleQuoted'|`"$raw`"|$raw)"
}

function Test-LegacyNotifierProcessShape {
  param(
    [object]$Process,
    [string]$HomePath
  )

  if ($null -eq $Process) { return $false }
  $name = [string]$Process.Name
  if ($name -notin @('powershell.exe', 'pwsh.exe', 'wscript.exe')) { return $false }
  $commandLine = [string]$Process.CommandLine
  if ([string]::IsNullOrWhiteSpace($commandLine) -or $commandLine.Length -gt 131072) {
    return $false
  }

  try { $canonicalHome = [IO.Path]::GetFullPath($HomePath) } catch { return $false }
  $hiddenVbs = Join-Path $canonicalHome 'watch-codex-ntfy-hidden.vbs'
  $watcherScript = Join-Path $canonicalHome 'watch-codex-ntfy.ps1'
  $notifierScript = Join-Path $canonicalHome 'notify-ntfy.ps1'
  if ($name -eq 'wscript.exe') {
    $wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
    $executablePath = [string](Get-ObjectValue -Object $Process -Name 'ExecutablePath' -Default '')
    if (-not [string]::IsNullOrWhiteSpace($executablePath)) {
      try {
        if (-not [string]::Equals(
            [IO.Path]::GetFullPath($executablePath),
            [IO.Path]::GetFullPath($wscript),
            [StringComparison]::OrdinalIgnoreCase
          )) { return $false }
      } catch { return $false }
    }
    $wscriptPattern = Get-PowerShellCommandTokenPattern -Value ([IO.Path]::GetFullPath($wscript))
    $hiddenPattern = Get-PowerShellCommandTokenPattern -Value ([IO.Path]::GetFullPath($hiddenVbs))
    return $commandLine -match "(?i)^\s*$wscriptPattern\s+//B\s+//Nologo\s+$hiddenPattern\s*$"
  }

  $watcherPattern = Get-PowerShellCommandTokenPattern -Value ([IO.Path]::GetFullPath($watcherScript))
  $notifierPattern = Get-PowerShellCommandTokenPattern -Value ([IO.Path]::GetFullPath($notifierScript))
  $runsWatcher = $commandLine -match "(?i)(?<!\S)-File\s+$watcherPattern(?=$|\s)"
  $runsWorker = $commandLine -match "(?i)(?<!\S)-File\s+$notifierPattern(?=$|\s)" -and
    $commandLine -match '(?i)(?<!\S)-(?:Worker|Continuous|ScanRollouts|Maintenance)(?=$|\s)'
  return $runsWatcher -or $runsWorker
}

function Test-LegacyAudnCodeHookProcessShape {
  param(
    [object]$Process,
    [string]$ScriptPath,
    [string]$AudnCodeHomePath
  )

  if ($null -eq $Process -or
      [string]$Process.Name -notin @('powershell.exe', 'pwsh.exe')) {
    return $false
  }
  $commandLine = [string]$Process.CommandLine
  if ([string]::IsNullOrWhiteSpace($commandLine) -or $commandLine.Length -gt 131072) {
    return $false
  }
  $scriptPattern = Get-PowerShellCommandTokenPattern -Value ([IO.Path]::GetFullPath($ScriptPath))
  $homePattern = Get-PowerShellCommandTokenPattern -Value ([IO.Path]::GetFullPath($AudnCodeHomePath))
  $originPattern = Get-PowerShellCommandTokenPattern -Value 'AudnCode'
  return $commandLine -match "(?i)(?<!\S)$scriptPattern(?=$|\s)" -and
    $commandLine -match '(?i)(?<!\S)-AudnCodeHook(?=$|\s)' -and
    $commandLine -match '(?i)(?<!\S)-ReadStdin(?=$|\s)' -and
    $commandLine -match "(?i)(?<!\S)-Origin\s+$originPattern(?=$|\s)" -and
    $commandLine -match "(?i)(?<!\S)-AudnCodeHome\s+$homePattern(?=$|\s)" -and
    $commandLine -notmatch '(?i)(?<!\S)-AudnCodeExpectedEvent(?=$|\s)'
}

function Remove-LegacyAudnCodeHookOrphans {
  param(
    [string]$ScriptPath,
    [string]$AudnCodeHomePath
  )

  $minimumAgeSeconds = 30
  $testMinimumAgeSeconds = 0
  if ($env:CODEX_NTFY_TEST_MODE -eq '1' -and
      [int]::TryParse(
        [string]$env:CODEX_NTFY_TEST_LEGACY_ORPHAN_MIN_AGE_SECONDS,
        [ref]$testMinimumAgeSeconds
      ) -and
      $testMinimumAgeSeconds -ge 1 -and $testMinimumAgeSeconds -lt $minimumAgeSeconds) {
    $minimumAgeSeconds = $testMinimumAgeSeconds
  }

  $removed = 0
  for ($pass = 1; $pass -le 3; $pass++) {
    try {
      $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object -First 16385)
    } catch {
      Write-Warning "Could not inspect legacy AudnCode hook processes safely; close any old notify-ntfy.ps1 PowerShell process manually. $($_.Exception.Message)"
      return $removed
    }
    if ($snapshot.Count -gt 16384) {
      Write-Warning 'Too many processes exist to verify legacy AudnCode hook ownership safely; close old notify-ntfy.ps1 PowerShell processes manually.'
      return $removed
    }
    $snapshotByPid = @{}
    foreach ($process in $snapshot) {
      try { $snapshotByPid[[int]$process.ProcessId] = $process } catch { }
    }
    $candidates = New-Object 'System.Collections.Generic.List[object]'
    foreach ($process in $snapshot) {
      if (-not (Test-LegacyAudnCodeHookProcessShape `
          -Process $process `
          -ScriptPath $ScriptPath `
          -AudnCodeHomePath $AudnCodeHomePath)) { continue }
      $createdUtc = Get-CimProcessCreationUtc -Process $process
      if ($null -eq $createdUtc -or
          ([DateTime]::UtcNow - $createdUtc).TotalSeconds -le $minimumAgeSeconds) { continue }
      $parentPid = [int]$process.ParentProcessId
      if ($parentPid -gt 0 -and $snapshotByPid.ContainsKey($parentPid)) { continue }
      $candidates.Add([pscustomobject]@{
          pid = [int]$process.ProcessId
          parent_pid = $parentPid
          creation_ticks = [int64]$createdUtc.Ticks
          command_line = [string]$process.CommandLine
        })
    }
    if ($candidates.Count -eq 0) { break }

    $removedThisPass = 0
    foreach ($candidate in $candidates) {
      try {
        $currentRows = @(Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $candidate.pid) -ErrorAction Stop)
        if ($currentRows.Count -ne 1) { continue }
        $current = $currentRows[0]
        $currentCreatedUtc = Get-CimProcessCreationUtc -Process $current
        if ($null -eq $currentCreatedUtc -or
            [int64]$currentCreatedUtc.Ticks -ne [int64]$candidate.creation_ticks -or
            [int]$current.ParentProcessId -ne [int]$candidate.parent_pid -or
            -not [string]::Equals(
              [string]$current.CommandLine,
              [string]$candidate.command_line,
              [StringComparison]::Ordinal
            ) -or
            -not (Test-LegacyAudnCodeHookProcessShape `
              -Process $current `
              -ScriptPath $ScriptPath `
              -AudnCodeHomePath $AudnCodeHomePath)) {
          continue
        }
        if ([int]$candidate.parent_pid -gt 0) {
          $currentParent = @(Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $candidate.parent_pid) -ErrorAction Stop)
          if ($currentParent.Count -ne 0) { continue }
        }
        Stop-Process -Id ([int]$candidate.pid) -Force -ErrorAction Stop
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
        do {
          if ($null -eq (Get-Process -Id ([int]$candidate.pid) -ErrorAction SilentlyContinue)) { break }
          Start-Sleep -Milliseconds 50
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        if ($null -ne (Get-Process -Id ([int]$candidate.pid) -ErrorAction SilentlyContinue)) {
          Write-Warning "Legacy AudnCode hook PID $($candidate.pid) did not exit; close it manually."
          continue
        }
        $removed++
        $removedThisPass++
      } catch {
        Write-Warning "Skipped legacy AudnCode hook PID $($candidate.pid) because its lifetime could not be reverified safely. Close it manually if it remains. $($_.Exception.Message)"
      }
    }
    if ($removedThisPass -eq 0) { break }
  }
  if ($removed -gt 0) {
    Write-Status "Stopped $removed verified orphaned legacy AudnCode hook process(es)."
  }
  return $removed
}

function Install-WindowsFiles {
  param([string]$HomePath)

  foreach ($name in @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs')) {
    $source = Join-Path $SourceRoot $name
    $target = Join-Path $HomePath $name
    $stage = "$target.new"
    Copy-Item -LiteralPath $source -Destination $stage -Force
    Unblock-File -LiteralPath $stage -ErrorAction Stop
    Move-Item -LiteralPath $stage -Destination $target -Force
  }
  $state = Join-Path $HomePath 'ntfy-state'
  New-Item -ItemType Directory -Path $state -Force | Out-Null
  # 2.4.2 and earlier left one zero-byte lock per completion. The worker is
  # stopped while installing, so these obsolete lock names are safe to remove.
  Get-ChildItem -LiteralPath $state -Filter 'mutation-*.lock' -File -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
  foreach ($healthName in @('worker-health.json', 'delivery-health.json', 'watch-health.json', 'remote-watch-health.json')) {
    Remove-Item -LiteralPath (Join-Path $state $healthName) -Force -ErrorAction SilentlyContinue
  }
  Protect-PrivatePath $state

  $scriptPath = Join-Path $HomePath 'notify-ntfy.ps1'
  $windowsPowerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $escapedScript = $scriptPath.Replace('\', '\\').Replace('"', '\"')
  $windowsPowerShell = $windowsPowerShellPath.Replace('\', '\\').Replace('"', '\"')
  $notifyLine = 'notify = ["' + $windowsPowerShell + '", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "' + $escapedScript + '"]'
  $configPath = Join-Path $HomePath 'config.toml'
  Ensure-TopLevelNotify -ConfigPath $configPath -NotifyLine $notifyLine -ExpectedMarker 'notify-ntfy.ps1'
  Protect-PrivatePath $configPath
  $hookCommand = '"{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}" -HookEvent' -f $windowsPowerShellPath, $scriptPath
  $hooksPath = Join-Path $HomePath 'hooks.json'
  Ensure-StopHook -HooksPath $hooksPath -Command $hookCommand
  Protect-PrivatePath $hooksPath
}

function Ensure-ScheduledWorker {
  param([string]$HomePath)

  if ($SkipScheduledTask) {
    return
  }
  $wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
  $vbs = Join-Path $HomePath 'watch-codex-ntfy-hidden.vbs'
  $action = New-ScheduledTaskAction -Execute $wscript -Argument ('//B //Nologo "{0}"' -f $vbs) -WorkingDirectory $HomePath
  $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
  $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Durable ntfy worker for Codex completion notifications.' -Force | Out-Null
  $registeredTask = Get-ScheduledTask -TaskName $TaskName
  if (-not (Test-OwnedScheduledTask -Task $registeredTask -HomePath $HomePath)) {
    throw 'Scheduled worker action does not match the installed notifier.'
  }
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 1
  $state = (Get-ScheduledTask -TaskName $TaskName).State
  if ($state -ne 'Running') {
    throw "Scheduled worker did not start (state: $state)."
  }
  $workerHealthPath = Join-Path (Join-Path $HomePath 'ntfy-state') 'worker-health.json'
  $workerReady = $false
  # PowerShell cold starts can be delayed substantially by AMSI/Defender on
  # slower Windows hosts. The launcher now skips one PowerShell hop, but keep a
  # generous verification window so installation does not roll back a healthy
  # worker merely because process creation was temporarily slow.
  $workerDeadline = [DateTimeOffset]::UtcNow.AddSeconds(240)
  do {
    try {
      $health = Read-StrictJsonFile -Path $workerHealthPath
      $healthProcess = [System.Diagnostics.Process]::GetProcessById([int]$health.pid)
      if ($null -ne $healthProcess) {
        $workerReady = $true
        break
      }
    } catch {
    }
    Start-Sleep -Milliseconds 250
  } while ([DateTimeOffset]::UtcNow -lt $workerDeadline)
  if (-not $workerReady) {
    throw 'Scheduled worker task started but its notifier process did not become healthy.'
  }
  Write-Status 'Windows durable worker is running.'
}

function Test-OwnedScheduledTask {
  param(
    [object]$Task,
    [string]$HomePath
  )

  if ($null -eq $Task) { return $false }
  $actions = @($Task.Actions)
  if ($actions.Count -ne 1) { return $false }
  $action = $actions[0]
  try {
    $canonicalHome = [IO.Path]::GetFullPath($HomePath)
    $expectedExecutable = [IO.Path]::GetFullPath(
      (Join-Path $env:WINDIR 'System32\wscript.exe')
    )
    $actualExecutable = [IO.Path]::GetFullPath([string]$action.Execute)
    $actualWorkingDirectory = [IO.Path]::GetFullPath([string]$action.WorkingDirectory)
    $expectedArguments = '//B //Nologo "{0}"' -f (Join-Path $canonicalHome 'watch-codex-ntfy-hidden.vbs')
    return [string]::Equals(
        $actualExecutable,
        $expectedExecutable,
        [StringComparison]::OrdinalIgnoreCase
      ) -and
      [string]::Equals(
        [string]$action.Arguments,
        $expectedArguments,
        [StringComparison]::OrdinalIgnoreCase
      ) -and
      [string]::Equals(
        $actualWorkingDirectory,
        $canonicalHome,
        [StringComparison]::OrdinalIgnoreCase
      )
  } catch {
    return $false
  }
}

function Convert-WslHomeToUnc {
  param(
    [string]$Distro,
    [string]$LinuxHome
  )
  $relative = $LinuxHome.Trim().TrimStart('/').Replace('/', '\')
  return "\\wsl.localhost\$Distro\$relative"
}

function Register-WslWatchRoot {
  param(
    [string]$ConfigPath,
    [string]$Distro,
    [string]$Root,
    [string]$SqliteRoot
  )

  $config = Read-StrictJsonFile -Path $ConfigPath
  $entries = @()
  $property = $config.PSObject.Properties['watch_roots']
  if ($null -ne $property) {
    foreach ($entry in @($property.Value)) {
      if ($entry -is [string]) {
        $entries += [pscustomobject][ordered]@{ path = [string]$entry; sqlite_path = [string]$entry; origin = '' }
      } elseif ($null -ne $entry) {
        $path = [string]$entry.path
        if (-not [string]::IsNullOrWhiteSpace($path)) {
          $sqlitePath = [string]$entry.sqlite_path
          if ([string]::IsNullOrWhiteSpace($sqlitePath)) { $sqlitePath = $path }
          $entries += [pscustomobject][ordered]@{ path = $path; sqlite_path = $sqlitePath; origin = [string]$entry.origin }
        }
      }
    }
  }
  $managedOrigin = "WSL:$Distro"
  $entries = @($entries | Where-Object {
      -not [string]::Equals($_.path, $Root, [StringComparison]::OrdinalIgnoreCase) -and
      -not [string]::Equals($_.origin, $managedOrigin, [StringComparison]::OrdinalIgnoreCase)
    })
  $entries += [pscustomobject][ordered]@{ path = $Root; sqlite_path = $SqliteRoot; origin = $managedOrigin }
  if ($null -eq $property) {
    Add-Member -InputObject $config -MemberType NoteProperty -Name 'watch_roots' -Value @($entries)
  } else {
    $property.Value = @($entries)
  }
  Write-TextAtomic -Path $ConfigPath -Content ($config | ConvertTo-Json -Depth 8)
  Protect-PrivatePath $ConfigPath
}

function Restore-WslInstallation {
  param([object]$State)

  if ($null -eq $State) { return }
  foreach ($name in @($State.Managed)) {
    $saved = Join-Path $State.Backup $name
    $target = Join-Path $State.UncCodex $name
    if (Test-Path -LiteralPath $saved) {
      Copy-Item -LiteralPath $saved -Destination $target -Force
    } elseif ($name -notin @($State.PreviouslyPresent)) {
      Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
    }
  }
  foreach ($name in @('notify-ntfy.py', 'notify-ntfy-wsl.sh')) {
    if (Test-Path -LiteralPath (Join-Path $State.UncCodex $name)) {
      & wsl.exe -d $State.Distro -- chmod 700 "$($State.LinuxCodex)/$name"
      if ($LASTEXITCODE -ne 0) { throw "Could not restore WSL executable permissions in $($State.Distro)." }
    }
  }
  foreach ($name in @('ntfy-config.json', 'config.toml', 'hooks.json')) {
    if (Test-Path -LiteralPath (Join-Path $State.UncCodex $name)) {
      & wsl.exe -d $State.Distro -- chmod 600 "$($State.LinuxCodex)/$name"
      if ($LASTEXITCODE -ne 0) { throw "Could not restore WSL private permissions in $($State.Distro)." }
    }
  }
}

function Install-WslNotifier {
  param(
    [string]$Distro,
    [string]$PrivateConfig,
    [string]$WindowsScriptPath
  )

  & wsl.exe -d $Distro -- true 2>$null
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "WSL distro '$Distro' is unavailable; skipped."
    return
  }
  $linuxCodex = (& wsl.exe -d $Distro -- sh -lc 'printf %s "${CODEX_HOME:-$HOME/.codex}"').Trim()
  if ([string]::IsNullOrWhiteSpace($linuxCodex) -or -not $linuxCodex.StartsWith('/')) {
    throw "Could not resolve CODEX_HOME in WSL distro $Distro."
  }
  $linuxSqlite = (& wsl.exe -d $Distro -- sh -lc 'printf %s "${CODEX_SQLITE_HOME:-${CODEX_HOME:-$HOME/.codex}}"').Trim()
  if ([string]::IsNullOrWhiteSpace($linuxSqlite) -or -not $linuxSqlite.StartsWith('/')) {
    throw "Could not resolve CODEX_SQLITE_HOME in WSL distro $Distro."
  }
  $uncCodex = Convert-WslHomeToUnc -Distro $Distro -LinuxHome $linuxCodex
  $uncSqlite = Convert-WslHomeToUnc -Distro $Distro -LinuxHome $linuxSqlite
  Assert-ManagedInstallerInputs -HomePath $uncCodex
  foreach ($existingScriptName in @('notify-ntfy.py', 'notify-ntfy-wsl.sh')) {
    $existingScriptPath = Join-Path $uncCodex $existingScriptName
    if (Test-Path -LiteralPath $existingScriptPath -PathType Leaf) {
      [void](Read-StrictUtf8Text -Path $existingScriptPath)
    }
  }
  Register-WslWatchRoot -ConfigPath $PrivateConfig -Distro $Distro -Root $uncCodex -SqliteRoot $uncSqlite
  New-Item -ItemType Directory -Path $uncCodex -Force | Out-Null
  & wsl.exe -d $Distro -- chmod 700 $linuxCodex
  if ($LASTEXITCODE -ne 0) { throw "Could not protect the WSL Codex directory in $Distro." }
  $backupRoot = Join-Path $uncCodex 'ntfy-backups'
  $backup = Join-Path $backupRoot (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
  New-Item -ItemType Directory -Path $backup -Force | Out-Null
  $linuxBackupRoot = "$linuxCodex/ntfy-backups"
  $linuxBackup = "$linuxBackupRoot/$(Split-Path -Leaf $backup)"
  & wsl.exe -d $Distro -- chmod 700 $linuxBackupRoot $linuxBackup
  if ($LASTEXITCODE -ne 0) { throw "Could not protect the WSL backup in $Distro." }
  $managed = @('notify-ntfy.py', 'notify-ntfy-wsl.sh', 'ntfy-config.json', 'config.toml', 'hooks.json')
  $previouslyPresent = @()
  foreach ($name in $managed) {
    $source = Join-Path $uncCodex $name
    if (Test-Path -LiteralPath $source) {
      $previouslyPresent += $name
      Copy-Item -LiteralPath $source -Destination (Join-Path $backup $name) -Force
      & wsl.exe -d $Distro -- chmod 600 "$linuxBackup/$name"
      if ($LASTEXITCODE -ne 0) { throw "Could not protect a WSL backup file in $Distro." }
    }
  }
  Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    Select-Object -Skip 10 |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  $rollbackState = [pscustomobject][ordered]@{
    Distro = $Distro
    UncCodex = $uncCodex
    LinuxCodex = $linuxCodex
    Backup = $backup
    PreviouslyPresent = @($previouslyPresent)
    Managed = @($managed)
  }

  try {
    foreach ($copy in @(
        @((Join-Path $SourceRoot 'notify-ntfy.py'), 'notify-ntfy.py'),
        @((Join-Path $SourceRoot 'notify-ntfy-wsl.sh'), 'notify-ntfy-wsl.sh'),
        @($PrivateConfig, 'ntfy-config.json')
      )) {
      $target = Join-Path $uncCodex $copy[1]
      $stage = "$target.new"
      Copy-Item -LiteralPath $copy[0] -Destination $stage -Force
      Move-Item -LiteralPath $stage -Destination $target -Force
    }

    $escapedLinuxScript = ($linuxCodex + '/notify-ntfy-wsl.sh').Replace('\', '\\').Replace('"', '\"')
    $escapedWindowsScript = $WindowsScriptPath.Replace('\', '\\').Replace('"', '\"')
    $notifyLine = 'notify = ["' + $escapedLinuxScript + '", "--windows-script", "' + $escapedWindowsScript + '"]'
    Ensure-TopLevelNotify -ConfigPath (Join-Path $uncCodex 'config.toml') -NotifyLine $notifyLine -ExpectedMarker 'notify-ntfy-wsl.sh'
    $hookCommand = (ConvertTo-PosixShellArgument ($linuxCodex + '/notify-ntfy-wsl.sh')) +
      ' --hook-event --windows-script ' + (ConvertTo-PosixShellArgument $WindowsScriptPath)
    Ensure-StopHook -HooksPath (Join-Path $uncCodex 'hooks.json') -Command $hookCommand

    & wsl.exe -d $Distro -- chmod 700 "$linuxCodex/notify-ntfy.py" "$linuxCodex/notify-ntfy-wsl.sh"
    if ($LASTEXITCODE -ne 0) { throw "Could not protect WSL executables in $Distro." }
    & wsl.exe -d $Distro -- chmod 600 "$linuxCodex/ntfy-config.json" "$linuxCodex/config.toml" "$linuxCodex/hooks.json"
    if ($LASTEXITCODE -ne 0) { throw "Could not protect WSL private configuration in $Distro." }
    & wsl.exe -d $Distro -- python3 "$linuxCodex/notify-ntfy.py" --cleanup-test-state | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not clean synthetic test state in $Distro." }
    & wsl.exe -d $Distro -- python3 "$linuxCodex/notify-ntfy.py" --doctor | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "WSL doctor failed for $Distro." }
    Write-Status "Installed WSL bridge and native fallback in $Distro."
    return $rollbackState
  } catch {
    $installationError = $_
    try {
      Restore-WslInstallation -State $rollbackState
    } catch {
      Write-Warning "Automatic WSL rollback failed; use the private backup at $linuxBackup."
    }
    throw $installationError
  }
}

foreach ($required in @('notify-ntfy.ps1', 'notify-ntfy.py', 'notify-ntfy-wsl.sh', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs')) {
  if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $required))) {
    throw "Missing source file: $required"
  }
}

$preflightClaudeSettingsPath = if ($EnableClaudeCode) { Join-Path $ClaudeHome 'settings.json' } else { '' }
$preflightAudnSettingsPath = if ($EnableAudnCode) { Join-Path $AudnCodeHome 'settings.json' } else { '' }
$preflightAudnGlobalPath = if ($EnableAudnCode) { Resolve-AudnCodeGlobalConfigPath -HomePath $AudnCodeHome } else { '' }
$preflightAudnMarkerPath = if ($EnableAudnCode) { Join-Path $AudnCodeHome $AudnCodeHookObservationMarkerName } else { '' }
# Validate every existing managed input before a mutex, backup directory, ACL,
# scheduled task, or configuration file can be changed.
Assert-ManagedInstallerInputs `
  -HomePath $CodexHome `
  -ClaudeSettingsPath $preflightClaudeSettingsPath `
  -AudnSettingsPath $preflightAudnSettingsPath `
  -AudnGlobalPath $preflightAudnGlobalPath `
  -AudnMarkerPath $preflightAudnMarkerPath

$installerTransactionMutex = $null
$audnCodeTransactionMutex = $null
try {
$installerTransactionMutex = Enter-InstallerTransactionLock `
  -ScheduledTaskName $TaskName `
  -TimeoutSeconds 600
if ($EnableAudnCode) {
  # The fixed acquisition order is global installer/task transaction first,
  # then the per-AudnCode-home marker mutex. Windows mutexes are recursive on
  # the owning thread, so marker helpers can safely re-enter the second lock.
  # Both locks remain held across backup, mutation, validation, and rollback.
  $audnCodeTransactionMarkerPath = Join-Path $AudnCodeHome $AudnCodeHookObservationMarkerName
  $audnCodeTransactionMutex = Enter-AudnCodeHookMarkerLock `
    -MarkerPath $audnCodeTransactionMarkerPath `
    -TimeoutSeconds 600
}

New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null
$legacyScript = Join-Path $CodexHome 'notify-ntfy.ps1'
$privateConfig = Join-Path $CodexHome 'ntfy-config.json'
$managedNames = @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'config.toml', 'hooks.json', 'ntfy-config.json')
$previouslyPresent = @($managedNames | Where-Object { Test-Path -LiteralPath (Join-Path $CodexHome $_) })
$previousTask = if ($SkipScheduledTask) { $null } else { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
$taskPreviouslyPresent = $null -ne $previousTask
$taskWasRunning = $taskPreviouslyPresent -and [string]$previousTask.State -eq 'Running'
if ($taskPreviouslyPresent -and -not (Test-OwnedScheduledTask -Task $previousTask -HomePath $CodexHome)) {
  throw "Scheduled task '$TaskName' already exists but is unrelated; refusing to overwrite it."
}
$backup = Backup-CurrentInstallation -HomePath $CodexHome
$claudeSettingsPath = $preflightClaudeSettingsPath
$claudeSettingsPreviouslyPresent = $EnableClaudeCode -and (Test-Path -LiteralPath $claudeSettingsPath)
if ($claudeSettingsPreviouslyPresent) {
  $claudeBackupPath = Join-Path $backup 'claude-settings.json'
  Copy-Item -LiteralPath $claudeSettingsPath -Destination $claudeBackupPath -Force
  Protect-PrivatePath $claudeBackupPath
}
$audnCodeSettingsPath = $preflightAudnSettingsPath
$audnCodeGlobalConfigPath = $preflightAudnGlobalPath
$audnCodeHookMarkerPath = $preflightAudnMarkerPath
$audnCodeSettingsPreviouslyPresent = $EnableAudnCode -and (Test-Path -LiteralPath $audnCodeSettingsPath)
$audnCodeGlobalConfigPreviouslyPresent = $EnableAudnCode -and (Test-Path -LiteralPath $audnCodeGlobalConfigPath)
$audnCodeHookMarkerPreviouslyPresent = $EnableAudnCode -and (Test-Path -LiteralPath $audnCodeHookMarkerPath -PathType Leaf)
if ($audnCodeSettingsPreviouslyPresent) {
  $audnCodeSettingsBackupPath = Join-Path $backup 'audncode-settings.json'
  Copy-Item -LiteralPath $audnCodeSettingsPath -Destination $audnCodeSettingsBackupPath -Force
  Protect-PrivatePath $audnCodeSettingsBackupPath
}
if ($audnCodeGlobalConfigPreviouslyPresent) {
  $audnCodeGlobalBackupPath = Join-Path $backup 'audncode-global.json'
  Copy-Item -LiteralPath $audnCodeGlobalConfigPath -Destination $audnCodeGlobalBackupPath -Force
  Protect-PrivatePath $audnCodeGlobalBackupPath
}
$audnCodeHookMarkerBackupPath = Join-Path $backup 'audncode-hooks-marker.json'
if ($audnCodeHookMarkerPreviouslyPresent) {
  Copy-Item -LiteralPath $audnCodeHookMarkerPath -Destination $audnCodeHookMarkerBackupPath -Force
  Protect-PrivatePath $audnCodeHookMarkerBackupPath
}
$wslInstallations = @()
$audnCodeSettingsMutation = $null
$audnCodeGlobalConfigMutation = $null
$audnCodeHookMarkerState = $null

try {
  New-PrivateConfigIfNeeded -Target $privateConfig -LegacyScript $legacyScript
  if (-not $SkipScheduledTask) {
    Stop-LegacyTask -HomePath $CodexHome
  }
  if ($EnableAudnCode) {
    # The outer AudnCode marker mutex is already held. Only exact, parentless
    # pre-shape-6 hooks are eligible; current managed hooks and live wrappers
    # are deliberately preserved.
    [void](Remove-LegacyAudnCodeHookOrphans `
        -ScriptPath (Join-Path $CodexHome 'notify-ntfy.ps1') `
        -AudnCodeHomePath $AudnCodeHome)
  }
  Install-WindowsFiles -HomePath $CodexHome
  if ($EnableClaudeCode) {
    $claudeCodeInstallations = @(Get-InstalledClaudeCodeVersions)
    if ($claudeCodeInstallations.Count -gt 0) {
      Assert-ClaudeCodeVersions -Installations $claudeCodeInstallations
    } else {
      Write-Warning 'Claude Code executable was not found. Hooks will be installed, but Claude Code 2.1.198 or newer is required.'
    }
    New-Item -ItemType Directory -Path $ClaudeHome -Force | Out-Null
    $windowsPowerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Ensure-ClaudeCodeHooks `
      -SettingsPath $claudeSettingsPath `
      -PowerShellPath $windowsPowerShellPath `
      -ScriptPath (Join-Path $CodexHome 'notify-ntfy.ps1')
    Write-Status 'Claude Code completion hooks installed without changing unrelated Claude settings.'
  }
  if ($EnableAudnCode) {
    New-Item -ItemType Directory -Path $AudnCodeHome -Force | Out-Null
    $windowsPowerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Ensure-AudnCodeHooks `
      -SettingsPath $audnCodeSettingsPath `
      -PowerShellPath $windowsPowerShellPath `
      -ScriptPath (Join-Path $CodexHome 'notify-ntfy.ps1') `
      -AudnCodeHomePath $AudnCodeHome `
      -MutationState ([ref]$audnCodeSettingsMutation)
    Ensure-AudnCodeIdleThreshold `
      -GlobalConfigPath $audnCodeGlobalConfigPath `
      -ThresholdMs $AudnCodeIdleThresholdMs `
      -MutationState ([ref]$audnCodeGlobalConfigMutation)
    $audnCodeHookEventsChanged = $null -ne $audnCodeSettingsMutation -and
      @((Get-ObjectValue -Object $audnCodeSettingsMutation -Name 'Events' -Default @())).Count -gt 0
    $audnCodeHookMarkerState = Ensure-AudnCodeHookObservationMarker `
      -HomePath $AudnCodeHome `
      -MarkerPath $audnCodeHookMarkerPath `
      -ManagedHookEventsChanged ([bool]$audnCodeHookEventsChanged)
    Write-Status "AudnCode final-idle hooks installed; completion delay is $AudnCodeIdleThresholdMs ms."
  }
  $removedSyntheticTests = & (Join-Path $CodexHome 'notify-ntfy.ps1') -CleanupTestState
  if ($LASTEXITCODE -ne 0) { throw 'Could not clean synthetic Windows test state.' }
  if ([int]$removedSyntheticTests -gt 0) {
    Write-Status "Removed $removedSyntheticTests synthetic test receipt(s) from local notifier state."
  }
  Ensure-ScheduledWorker -HomePath $CodexHome
  if (-not $NoWsl) {
    foreach ($distro in $WslDistro) {
      $installedWsl = Install-WslNotifier -Distro $distro -PrivateConfig $privateConfig -WindowsScriptPath (Join-Path $CodexHome 'notify-ntfy.ps1')
      if ($null -ne $installedWsl) { $wslInstallations += $installedWsl }
    }
  }
  & (Join-Path $CodexHome 'notify-ntfy.ps1') -Doctor | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw 'Windows notifier doctor failed.'
  }
  Write-Status 'Installation completed without exposing the ntfy destination.'
  Write-Warning 'Codex will skip the new Stop hook until you review and trust it with /hooks in every installed Codex environment.'
  if ($EnableClaudeCode) {
    $claudeSettings = Read-StrictJsonFile -Path $claudeSettingsPath
    if ([bool](Get-ObjectValue -Object $claudeSettings -Name 'disableAllHooks' -Default $false)) {
      Write-Warning 'Claude Code hooks are installed, but disableAllHooks is true in Claude settings.'
    }
    Write-Status 'Claude Code Desktop/CLI/VS Code will pick up the managed lifecycle hooks through its settings watcher.'
  }
  if ($EnableAudnCode) {
    $audnCodeSettings = Read-StrictJsonFile -Path $audnCodeSettingsPath
    if ([bool](Get-ObjectValue -Object $audnCodeSettings -Name 'disableAllHooks' -Default $false)) {
      Write-Warning 'AudnCode hooks are installed, but disableAllHooks is true in AudnCode settings.'
    }
    Assert-AudnCodeIdleThreshold `
      -GlobalConfigPath $audnCodeGlobalConfigPath `
      -ThresholdMs $AudnCodeIdleThresholdMs
    if ([bool](Get-ObjectValue -Object $audnCodeHookMarkerState -Name 'rotated' -Default $true)) {
      Write-Warning 'Close and reopen every running AudnCode window once. Existing processes may own memory-only cron jobs created before the managed hooks became observable, so notifications stay fail closed until restart.'
    } else {
      Write-Status 'AudnCode managed hooks were already exact; their observation generation was preserved and no restart is required.'
    }
    Write-Warning 'AudnCode executes hooks only in trusted workspaces. Review trust in AudnCode if a workspace has never been approved.'
  }
  Write-Status 'Reload existing VS Code windows so their Codex app-server reads the new WSL notify command.'
} catch {
  $installationError = $_
  for ($index = $wslInstallations.Count - 1; $index -ge 0; $index--) {
    try {
      Restore-WslInstallation -State $wslInstallations[$index]
      Write-Warning "WSL installation $($wslInstallations[$index].Distro) was restored automatically."
    } catch {
      Write-Warning "Automatic WSL rollback failed for $($wslInstallations[$index].Distro): $($_.Exception.Message)"
    }
  }
  if ($EnableClaudeCode) {
    try {
      Restore-ClaudeCodeSettings `
        -SettingsPath $claudeSettingsPath `
        -BackupPath $backup `
        -PreviouslyPresent $claudeSettingsPreviouslyPresent
      Write-Warning 'The Claude Code settings file was restored automatically.'
    } catch {
      Write-Warning "Automatic Claude Code rollback failed; use the private backup at $backup. $($_.Exception.Message)"
    }
  }
  if ($EnableAudnCode) {
    try {
      Restore-AudnCodeConfiguration `
        -SettingsPath $audnCodeSettingsPath `
        -GlobalConfigPath $audnCodeGlobalConfigPath `
        -SettingsMutation $audnCodeSettingsMutation `
        -GlobalConfigMutation $audnCodeGlobalConfigMutation `
        -HookMarkerPath $audnCodeHookMarkerPath `
        -HookMarkerMutation $audnCodeHookMarkerState
      Write-Warning 'The AudnCode settings, global configuration, and hook observation marker were restored automatically.'
    } catch {
      Write-Warning "Automatic AudnCode rollback failed; use the private backup at $backup. $($_.Exception.Message)"
    }
  }
  try {
    Restore-WindowsInstallation `
      -HomePath $CodexHome `
      -BackupPath $backup `
      -PreviouslyPresent $previouslyPresent `
      -TaskPreviouslyPresent $taskPreviouslyPresent `
      -TaskWasRunning $taskWasRunning
    Write-Warning 'The Windows installation was restored automatically.'
  } catch {
    Write-Warning "Automatic Windows rollback failed; use the private backup at $backup. $($_.Exception.Message)"
  }
  Write-Error "Installation failed. Rollback files are in $backup. $($installationError.Exception.Message)" -ErrorAction Continue
  throw $installationError
}
} finally {
  try {
    if ($null -ne $audnCodeTransactionMutex) {
      Exit-AudnCodeHookMarkerLock -Mutex $audnCodeTransactionMutex
    }
  } finally {
    if ($null -ne $installerTransactionMutex) {
      Exit-InstallerTransactionLock -Mutex $installerTransactionMutex
    }
  }
}
