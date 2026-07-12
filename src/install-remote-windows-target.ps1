[CmdletBinding()]
param(
  [string]$Origin = 'SSH:Windows',
  [switch]$SkipScheduledTask
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$HomePath = $PSScriptRoot
$ConfigPath = Join-Path $HomePath 'config.toml'
$HooksPath = Join-Path $HomePath 'hooks.json'
$ScriptPath = Join-Path $HomePath 'notify-ntfy.ps1'
$PrivateConfig = Join-Path $HomePath 'ntfy-config.json'
$StatePath = Join-Path $HomePath 'ntfy-state'
$TaskName = 'CodexNtfyWatcher'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-TextAtomic {
  param(
    [string]$Path,
    [string]$Content
  )

  $directory = Split-Path -Parent $Path
  $temp = Join-Path $directory ('.{0}.{1}.tmp' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
  [System.IO.File]::WriteAllText($temp, $Content, $Utf8NoBom)
  try {
    Move-Item -LiteralPath $temp -Destination $Path -Force
  } finally {
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
  }
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

function Ensure-StopHook {
  param(
    [string]$Path,
    [string]$Command
  )

  $original = if (Test-Path -LiteralPath $Path) { [System.IO.File]::ReadAllText($Path) } else { '' }
  try {
    $document = if ([string]::IsNullOrWhiteSpace($original)) {
      [pscustomobject][ordered]@{}
    } else {
      $original | ConvertFrom-Json
    }
  } catch {
    throw "Invalid JSON in ${Path}: $($_.Exception.Message)"
  }
  if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
    throw "$Path must contain a JSON object."
  }
  $hooksProperty = $document.PSObject.Properties['hooks']
  if ($null -eq $hooksProperty) {
    Add-Member -InputObject $document -MemberType NoteProperty -Name 'hooks' -Value ([pscustomobject][ordered]@{})
    $hooksProperty = $document.PSObject.Properties['hooks']
  } elseif ($null -eq $hooksProperty.Value -or $hooksProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
    throw "hooks in $Path must contain a JSON object."
  }
  $hookEvents = $hooksProperty.Value

  foreach ($eventProperty in @($hookEvents.PSObject.Properties)) {
    if ($eventProperty.Value -isnot [array]) {
      if ($eventProperty.Name -eq 'Stop') { throw "hooks.Stop in $Path must contain a JSON array." }
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
    throw "hooks.Stop in $Path must contain a JSON array."
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
    Write-TextAtomic -Path $Path -Content $rendered
  }
}

$hooksPreviouslyPresent = Test-Path -LiteralPath $HooksPath
$originalHooks = if ($hooksPreviouslyPresent) { [System.IO.File]::ReadAllBytes($HooksPath) } else { $null }
$backupRoot = Join-Path $HomePath 'ntfy-backups'
if ($hooksPreviouslyPresent -and (Test-Path -LiteralPath $backupRoot)) {
  $latestBackup = Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    Select-Object -First 1
  if ($null -ne $latestBackup) {
    $savedHooks = Join-Path $latestBackup.FullName 'hooks.json'
    if (-not (Test-Path -LiteralPath $savedHooks)) {
      Copy-Item -LiteralPath $HooksPath -Destination $savedHooks -Force
    }
  }
}

try {

if (-not (Test-Path -LiteralPath $ScriptPath) -or -not (Test-Path -LiteralPath $PrivateConfig)) {
  throw 'Remote notifier files are incomplete.'
}

if (-not $SkipScheduledTask) {
  $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($null -ne $existingTask) {
    $owned = $false
    foreach ($existingAction in @($existingTask.Actions)) {
      $description = '{0} {1} {2}' -f $existingAction.Execute, $existingAction.Arguments, $existingAction.WorkingDirectory
      if ($description -match '(?i)(?:watch-codex-ntfy|notify-ntfy)') { $owned = $true; break }
    }
    if (-not $owned) { throw "Scheduled task '$TaskName' already exists but is unrelated; refusing to overwrite it." }
  }
}

$privateObject = Get-Content -LiteralPath $PrivateConfig -Raw | ConvertFrom-Json
$privateChanged = $false
foreach ($default in @(
    @('include_message', $true),
    @('include_thread_title', $true),
    @('include_task_link', $false),
    @('include_task_link_action', $false),
    @('allow_insecure_auth', $false),
    @('priority', 3),
    @('tags', @('white_check_mark')),
    @('max_message_chars', 180),
    @('markdown', $false),
    @('idle_detection_mode', 'strict'),
    @('idle_grace_seconds', 1.5),
    @('idle_probe_grace_seconds', 30),
    @('goal_aware', $true),
    @('goal_poll_seconds', 1),
    @('subagent_orphan_seconds', 1800),
    @('suppress_technical_turns', $true),
    @('watch_rollouts', $true),
    @('watch_scan_seconds', 2),
    @('watch_discovery_seconds', 60),
    @('watch_initial_replay_seconds', 15),
    @('dead_retention_days', 30)
  )) {
  if ($null -eq $privateObject.PSObject.Properties[$default[0]]) {
    Add-Member -InputObject $privateObject -MemberType NoteProperty -Name $default[0] -Value $default[1]
    $privateChanged = $true
  }
}
$configuredTags = @($privateObject.tags)
if ($configuredTags.Count -eq 2 -and
    [string]$configuredTags[0] -eq 'computer' -and
    [string]$configuredTags[1] -eq 'white_check_mark') {
  $privateObject.tags = @('white_check_mark')
  $privateChanged = $true
}
$watchRootsProperty = $privateObject.PSObject.Properties['watch_roots']
if ($null -eq $watchRootsProperty) {
  Add-Member -InputObject $privateObject -MemberType NoteProperty -Name 'watch_roots' -Value @()
  $privateChanged = $true
} elseif (@($watchRootsProperty.Value).Count -ne 0) {
  # WSL/custom watcher paths belong to the source host and are not portable.
  $watchRootsProperty.Value = @()
  $privateChanged = $true
}
$workerSqlitePath = if ([string]::IsNullOrWhiteSpace($env:CODEX_SQLITE_HOME)) { $HomePath } else { [IO.Path]::GetFullPath($env:CODEX_SQLITE_HOME) }
$workerSqliteProperty = $privateObject.PSObject.Properties['worker_sqlite_path']
if ($null -eq $workerSqliteProperty) {
  Add-Member -InputObject $privateObject -MemberType NoteProperty -Name 'worker_sqlite_path' -Value $workerSqlitePath
  $privateChanged = $true
} elseif ([string]$workerSqliteProperty.Value -ne $workerSqlitePath) {
  $workerSqliteProperty.Value = $workerSqlitePath
  $privateChanged = $true
}
if ($privateChanged) {
  [IO.File]::WriteAllText($PrivateConfig, ($privateObject | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
}

$alias = $Origin -replace '^SSH:', ''
$effectiveOrigin = 'SSH:' + $env:COMPUTERNAME
if (-not [string]::IsNullOrWhiteSpace($alias) -and $alias -notin @('Windows', $env:COMPUTERNAME)) {
  $effectiveOrigin += ' (' + $alias + ')'
}
$escaped = $ScriptPath.Replace('\', '\\').Replace('"', '\"')
$escapedOrigin = $effectiveOrigin.Replace('\', '\\').Replace('"', '\"')
$windowsPowerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$escapedPowerShell = $windowsPowerShellPath.Replace('\', '\\').Replace('"', '\"')
$notifyLine = 'notify = ["' + $escapedPowerShell + '", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "' + $escaped + '", "-Origin", "' + $escapedOrigin + '"]'
$text = if (Test-Path -LiteralPath $ConfigPath) { [System.IO.File]::ReadAllText($ConfigPath) } else { '' }
$table = [regex]::Match($text, '(?m)^[ \t]*\[')
$rootText = if ($table.Success) { $text.Substring(0, $table.Index) } else { $text }
$match = [regex]::Match($rootText, '(?m)^[ \t]*notify[ \t]*=.*$')
$writeConfig = $false
if ($match.Success) {
  if ($match.Value -notmatch 'notify-ntfy\.ps1') {
    throw 'Existing remote notify command is unrelated; refusing to overwrite it.'
  }
  if ($match.Value -ne $notifyLine) {
    $text = $text.Remove($match.Index, $match.Length).Insert($match.Index, $notifyLine)
    $writeConfig = $true
  }
} else {
  if ($table.Success) {
    $text = $text.Insert($table.Index, $notifyLine + [Environment]::NewLine + [Environment]::NewLine)
  } else {
    $text = $text.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $notifyLine + [Environment]::NewLine
  }
  $writeConfig = $true
}
if ($writeConfig) {
  [System.IO.File]::WriteAllText($ConfigPath, $text, $Utf8NoBom)
}

$hookOrigin = $effectiveOrigin.Replace('"', '\"')
$hookCommand = '"{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}" -Origin "{2}" -HookEvent' -f $windowsPowerShellPath, $ScriptPath, $hookOrigin
Ensure-StopHook -Path $HooksPath -Command $hookCommand

New-Item -ItemType Directory -Path $StatePath -Force | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $PrivateConfig /inheritance:r /grant:r "${identity}:F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote private configuration.' }
& icacls.exe $ConfigPath /inheritance:r /grant:r "${identity}:F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote Codex configuration.' }
& icacls.exe $HooksPath /inheritance:r /grant:r "${identity}:F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote Codex hooks.' }
& icacls.exe $StatePath /inheritance:r /grant:r "${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote notifier state.' }

if (-not $SkipScheduledTask) {
  $wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
  $vbs = Join-Path $HomePath 'watch-codex-ntfy-hidden.vbs'
  $action = New-ScheduledTaskAction -Execute $wscript -Argument ('//B //Nologo "{0}"' -f $vbs) -WorkingDirectory $HomePath
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
  $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Durable ntfy worker for Codex completion notifications.' -Force | Out-Null
  $registeredAction = (Get-ScheduledTask -TaskName $TaskName).Actions | Select-Object -First 1
  if ([string]$registeredAction.Execute -ne $wscript -or [string]$registeredAction.Arguments -notlike "*$vbs*") {
    throw 'Remote scheduled worker action does not match the installed notifier.'
  }
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 2
}

$doctor = & $windowsPowerShellPath -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $ScriptPath -Doctor | ConvertFrom-Json
$workerState = if ($SkipScheduledTask) { 'skipped' } else { [string](Get-ScheduledTask -TaskName $TaskName).State }
if (-not $SkipScheduledTask -and $workerState -ne 'Running') {
  throw "Remote worker did not start (state: $workerState)."
}
Write-Warning 'Codex will skip the new Stop hook until you review and trust it with /hooks on this host.'
Write-Output ('OK computer=' + $env:COMPUTERNAME + ' user=' + $env:USERNAME + ' topic_configured=' + [bool]$doctor.topic_configured + ' worker=' + $workerState)
} catch {
  if ($hooksPreviouslyPresent) {
    [System.IO.File]::WriteAllBytes($HooksPath, $originalHooks)
  } else {
    Remove-Item -LiteralPath $HooksPath -Force -ErrorAction SilentlyContinue
  }
  throw
}
