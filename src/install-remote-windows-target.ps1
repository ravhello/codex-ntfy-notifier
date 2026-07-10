[CmdletBinding()]
param(
  [string]$Origin = 'SSH:Windows',
  [switch]$SkipScheduledTask
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$HomePath = $PSScriptRoot
$ConfigPath = Join-Path $HomePath 'config.toml'
$ScriptPath = Join-Path $HomePath 'notify-ntfy.ps1'
$PrivateConfig = Join-Path $HomePath 'ntfy-config.json'
$StatePath = Join-Path $HomePath 'ntfy-state'
$TaskName = 'CodexNtfyWatcher'

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
    @('allow_insecure_auth', $false),
    @('dead_retention_days', 30)
  )) {
  if ($null -eq $privateObject.PSObject.Properties[$default[0]]) {
    Add-Member -InputObject $privateObject -MemberType NoteProperty -Name $default[0] -Value $default[1]
    $privateChanged = $true
  }
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
  $encoding = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($ConfigPath, $text, $encoding)
}

New-Item -ItemType Directory -Path $StatePath -Force | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $PrivateConfig /inheritance:r /grant:r "${identity}:F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote private configuration.' }
& icacls.exe $ConfigPath /inheritance:r /grant:r "${identity}:F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote Codex configuration.' }
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
Write-Output ('OK computer=' + $env:COMPUTERNAME + ' user=' + $env:USERNAME + ' topic_configured=' + [bool]$doctor.topic_configured + ' worker=' + $workerState)
