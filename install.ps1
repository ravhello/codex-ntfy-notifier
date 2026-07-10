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

  if (Test-Path -LiteralPath $Target) {
    $config = Get-Content -LiteralPath $Target -Raw -Encoding UTF8 | ConvertFrom-Json
    $changed = $false
    $changed = (Add-ConfigDefault -Config $config -Name 'token' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'username' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'password' -Value '') -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_message' -Value $true) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'include_thread_title' -Value $true) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'allow_insecure_auth' -Value $false) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'markdown' -Value $true) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'subagent_classification_grace_seconds' -Value 8) -or $changed
    $changed = (Add-ConfigDefault -Config $config -Name 'dead_retention_days' -Value 30) -or $changed
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
    tags = @('computer', 'white_check_mark')
    max_message_chars = 900
    include_message = $false
    include_thread_title = $false
    markdown = $true
    include_full_path = $false
    suppress_subagents = $true
    subagent_classification_grace_seconds = 8
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
  foreach ($name in @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'config.toml', 'ntfy-config.json')) {
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
  foreach ($name in @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'config.toml', 'ntfy-config.json')) {
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
  foreach ($privateName in @('config.toml', 'ntfy-config.json')) {
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
         $_.CommandLine -match '(?i)notify-ntfy\.ps1.*-(?:Worker|Continuous)(?:\s|$)')
      })
    if ($watchers.Count -eq 0) {
      return
    }
    Start-Sleep -Milliseconds 250
  } while ([DateTimeOffset]::UtcNow -lt $deadline)
  foreach ($process in $watchers) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
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
  Protect-PrivatePath $state

  $escapedScript = (Join-Path $HomePath 'notify-ntfy.ps1').Replace('\', '\\').Replace('"', '\"')
  $windowsPowerShell = (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe').Replace('\', '\\').Replace('"', '\"')
  $notifyLine = 'notify = ["' + $windowsPowerShell + '", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "' + $escapedScript + '"]'
  $configPath = Join-Path $HomePath 'config.toml'
  Ensure-TopLevelNotify -ConfigPath $configPath -NotifyLine $notifyLine -ExpectedMarker 'notify-ntfy.ps1'
  Protect-PrivatePath $configPath
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
  $uncCodex = Convert-WslHomeToUnc -Distro $Distro -LinuxHome $linuxCodex
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
  $managed = @('notify-ntfy.py', 'notify-ntfy-wsl.sh', 'ntfy-config.json', 'config.toml')
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

    & wsl.exe -d $Distro -- chmod 700 "$linuxCodex/notify-ntfy.py" "$linuxCodex/notify-ntfy-wsl.sh"
    if ($LASTEXITCODE -ne 0) { throw "Could not protect WSL executables in $Distro." }
    & wsl.exe -d $Distro -- chmod 600 "$linuxCodex/ntfy-config.json" "$linuxCodex/config.toml"
    if ($LASTEXITCODE -ne 0) { throw "Could not protect WSL private configuration in $Distro." }
    & wsl.exe -d $Distro -- python3 "$linuxCodex/notify-ntfy.py" --doctor | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "WSL doctor failed for $Distro." }
    Write-Status "Installed WSL bridge and native fallback in $Distro."
  } catch {
    $installationError = $_
    try {
      foreach ($name in $managed) {
        $saved = Join-Path $backup $name
        $target = Join-Path $uncCodex $name
        if (Test-Path -LiteralPath $saved) {
          Copy-Item -LiteralPath $saved -Destination $target -Force
        } elseif ($name -notin $previouslyPresent) {
          Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        }
      }
      foreach ($name in @('notify-ntfy.py', 'notify-ntfy-wsl.sh')) {
        if (Test-Path -LiteralPath (Join-Path $uncCodex $name)) {
          & wsl.exe -d $Distro -- chmod 700 "$linuxCodex/$name"
        }
      }
      foreach ($name in @('ntfy-config.json', 'config.toml')) {
        if (Test-Path -LiteralPath (Join-Path $uncCodex $name)) {
          & wsl.exe -d $Distro -- chmod 600 "$linuxCodex/$name"
        }
      }
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
$managedNames = @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'config.toml', 'ntfy-config.json')
$previouslyPresent = @($managedNames | Where-Object { Test-Path -LiteralPath (Join-Path $CodexHome $_) })
$previousTask = if ($SkipScheduledTask) { $null } else { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
$taskPreviouslyPresent = $null -ne $previousTask
$taskWasRunning = $taskPreviouslyPresent -and [string]$previousTask.State -eq 'Running'
if ($taskPreviouslyPresent -and -not (Test-OwnedScheduledTask -Task $previousTask)) {
  throw "Scheduled task '$TaskName' already exists but is unrelated; refusing to overwrite it."
}
$backup = Backup-CurrentInstallation -HomePath $CodexHome

try {
  New-PrivateConfigIfNeeded -Target $privateConfig -LegacyScript $legacyScript
  if (-not $SkipScheduledTask) {
    Stop-LegacyTask
  }
  Install-WindowsFiles -HomePath $CodexHome
  Ensure-ScheduledWorker -HomePath $CodexHome
  if (-not $NoWsl) {
    foreach ($distro in $WslDistro) {
      Install-WslNotifier -Distro $distro -PrivateConfig $privateConfig -WindowsScriptPath (Join-Path $CodexHome 'notify-ntfy.ps1')
    }
  }
  & (Join-Path $CodexHome 'notify-ntfy.ps1') -Doctor | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw 'Windows notifier doctor failed.'
  }
  Write-Status 'Installation completed without exposing the ntfy destination.'
  Write-Status 'Reload existing VS Code windows so their Codex app-server reads the new WSL notify command.'
} catch {
  $installationError = $_
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
