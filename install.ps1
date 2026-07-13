[CmdletBinding()]
param(
  [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex'),
  [string[]]$WslDistro = @('Ubuntu'),
  [switch]$NoWsl,
  [switch]$SkipScheduledTask
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$SourceRoot = Join-Path $PSScriptRoot 'src'
$TaskName = 'CodexNtfyWatcher'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$CodexHome = [IO.Path]::GetFullPath($CodexHome)

function Write-Status {
  param([string]$Message)
  Write-Host "[codex-ntfy] $Message"
}

function Write-TextAtomic {
  param(
    [string]$Path,
    [string]$Content
  )

  $directory = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
  }
  $temp = Join-Path $directory ('.{0}.{1}.tmp' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
  [System.IO.File]::WriteAllText($temp, $Content, $Utf8NoBom)
  try {
    Move-Item -LiteralPath $temp -Destination $Path -Force
  } finally {
    if (Test-Path -LiteralPath $temp) {
      Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
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
  $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
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
    $config = Get-Content -LiteralPath $Target -Raw -Encoding UTF8 | ConvertFrom-Json
    $changed = $false
    $changed = (Add-ConfigDefault -Config $config -Name 'token' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'username' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'password' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_message' -Value $true) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_thread_title' -Value $true) -or $changed
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
    [System.IO.File]::ReadAllText($ConfigPath)
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

function ConvertTo-PosixShellArgument {
  param([string]$Value)

  $singleQuote = [string][char]39
  $doubleQuote = [string][char]34
  $escapedQuote = $singleQuote + $doubleQuote + $singleQuote + $doubleQuote + $singleQuote
  return $singleQuote + $Value.Replace($singleQuote, $escapedQuote) + $singleQuote
}

function Ensure-StopHook {
  param(
    [string]$HooksPath,
    [string]$Command
  )

  $original = if (Test-Path -LiteralPath $HooksPath) {
    [System.IO.File]::ReadAllText($HooksPath)
  } else { '' }
  try {
    $document = if ([string]::IsNullOrWhiteSpace($original)) {
      [pscustomobject][ordered]@{}
    } else {
      $original | ConvertFrom-Json
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

function Restore-WindowsInstallation {
  param(
    [string]$HomePath,
    [string]$BackupPath,
    [string[]]$PreviouslyPresent,
    [bool]$TaskPreviouslyPresent,
    [bool]$TaskWasRunning
  )

  if (-not $SkipScheduledTask) {
    Stop-LegacyTask
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
      $taskXmlContent = [IO.File]::ReadAllText($taskXml) -replace '^\s*<\?xml[^?]*\?>', ''
      Register-ScheduledTask -TaskName $TaskName -Xml $taskXmlContent -Force | Out-Null
      if ($TaskWasRunning) { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
    } elseif (-not $TaskPreviouslyPresent) {
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
  }
}

function Stop-LegacyTask {
  try {
    & schtasks.exe /End /TN $TaskName 2>$null | Out-Null
  } catch {
    # It may not exist yet.
  }
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
  do {
    $watchers = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('powershell.exe', 'pwsh.exe', 'wscript.exe') -and
        ($_.CommandLine -match '(?i)watch-codex-ntfy(?:-hidden)?\.(?:ps1|vbs)' -or
         $_.CommandLine -match '(?i)notify-ntfy\.ps1.*-(?:Worker|Continuous|ScanRollouts|Maintenance)(?:\s|$)')
      })
    if ($watchers.Count -eq 0) {
      return
    }
    Start-Sleep -Milliseconds 250
  } while ([DateTimeOffset]::UtcNow -lt $deadline)
  foreach ($process in $watchers) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
  $ownedIds = @($watchers.ProcessId | Sort-Object -Unique)
  $forcedDeadline = [DateTimeOffset]::UtcNow.AddSeconds(120)
  do {
    $aliveIds = @()
    foreach ($ownedId in $ownedIds) {
      try {
        [System.Diagnostics.Process]::GetProcessById([int]$ownedId) | Out-Null
        $aliveIds += $ownedId
      } catch {
      }
    }
    if ($aliveIds.Count -eq 0) { return }
    Start-Sleep -Milliseconds 250
  } while ([DateTimeOffset]::UtcNow -lt $forcedDeadline)
  throw "Could not stop existing notifier process(es): $($aliveIds -join ', ')."
}

function Install-WindowsFiles {
  param([string]$HomePath)

  foreach ($name in @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs')) {
    $source = Join-Path $SourceRoot $name
    $target = Join-Path $HomePath $name
    $stage = "$target.new"
    Copy-Item -LiteralPath $source -Destination $stage -Force
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
  $registeredAction = (Get-ScheduledTask -TaskName $TaskName).Actions | Select-Object -First 1
  if ([string]$registeredAction.Execute -ne $wscript -or [string]$registeredAction.Arguments -notlike "*$vbs*") {
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
      $health = Get-Content -LiteralPath $workerHealthPath -Raw -Encoding UTF8 | ConvertFrom-Json
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
  param([object]$Task)

  if ($null -eq $Task) { return $false }
  foreach ($action in @($Task.Actions)) {
    $description = '{0} {1} {2}' -f $action.Execute, $action.Arguments, $action.WorkingDirectory
    if ($description -match '(?i)(?:watch-codex-ntfy|notify-ntfy)') { return $true }
  }
  return $false
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

  $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
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

New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null
$legacyScript = Join-Path $CodexHome 'notify-ntfy.ps1'
$privateConfig = Join-Path $CodexHome 'ntfy-config.json'
$managedNames = @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'config.toml', 'hooks.json', 'ntfy-config.json')
$previouslyPresent = @($managedNames | Where-Object { Test-Path -LiteralPath (Join-Path $CodexHome $_) })
$previousTask = if ($SkipScheduledTask) { $null } else { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
$taskPreviouslyPresent = $null -ne $previousTask
$taskWasRunning = $taskPreviouslyPresent -and [string]$previousTask.State -eq 'Running'
if ($taskPreviouslyPresent -and -not (Test-OwnedScheduledTask -Task $previousTask)) {
  throw "Scheduled task '$TaskName' already exists but is unrelated; refusing to overwrite it."
}
$backup = Backup-CurrentInstallation -HomePath $CodexHome
$wslInstallations = @()

try {
  New-PrivateConfigIfNeeded -Target $privateConfig -LegacyScript $legacyScript
  if (-not $SkipScheduledTask) {
    Stop-LegacyTask
  }
  Install-WindowsFiles -HomePath $CodexHome
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
