[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string[]]$HostName,
  [string]$PrivateConfig = (Join-Path $env:USERPROFILE '.codex\ntfy-config.json'),
  [switch]$ReplaceRemoteConfig,
  [switch]$SkipScheduledTask
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$SourceRoot = Join-Path $PSScriptRoot 'src'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$SshExe = (Get-Command ssh.exe -ErrorAction Stop).Source
$ScpExe = (Get-Command scp.exe -ErrorAction Stop).Source

function Invoke-RemotePowerShell {
  param(
    [string]$HostAlias,
    [string]$Script
  )

  if ($HostAlias -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "Unsafe SSH host alias: $HostAlias"
  }
  $tokens = $null
  $parseErrors = $null
  [void][Management.Automation.Language.Parser]::ParseInput($Script, [ref]$tokens, [ref]$parseErrors)
  if ($parseErrors.Count -gt 0) {
    throw "Generated remote PowerShell is invalid: $($parseErrors.Message -join '; ')"
  }
  $loader = @'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  $encodedSource = [Console]::In.ReadLine()
  $source = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encodedSource))
  & ([scriptblock]::Create($source))
} catch {
  $message = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_.Exception.Message))
  [Console]::Out.WriteLine('__CODEX_NTFY_REMOTE_ERROR__' + $message)
  exit 1
}
'@
  $encodedLoader = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($loader))
  $encodedScript = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
  $startInfo = New-Object Diagnostics.ProcessStartInfo
  $startInfo.FileName = $SshExe
  $startInfo.Arguments = "-o BatchMode=yes -o ConnectTimeout=10 -- $HostAlias powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encodedLoader"
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardInput = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  $process = New-Object Diagnostics.Process
  $process.StartInfo = $startInfo
  try {
    if (-not $process.Start()) { throw "Could not start SSH for $HostAlias" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.StandardInput.WriteLine($encodedScript)
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(120000)) {
      $process.Kill()
      throw "SSH PowerShell timed out on $HostAlias"
    }
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    $exitCode = $process.ExitCode
  } finally {
    $process.Dispose()
  }
  $output = @()
  foreach ($text in @($stdout, $stderr)) {
    if (-not [string]::IsNullOrWhiteSpace($text)) {
      $output += @($text -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }
  }
  if ($exitCode -ne 0) {
    $remoteMessage = ''
    foreach ($line in @($output)) {
      $value = [string]$line
      if ($value.StartsWith('__CODEX_NTFY_REMOTE_ERROR__')) {
        try {
          $remoteMessage = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value.Substring(27)))
        } catch {
          $remoteMessage = 'remote error payload could not be decoded'
        }
      }
    }
    if ([string]::IsNullOrWhiteSpace($remoteMessage)) { $remoteMessage = ($output -join [Environment]::NewLine) }
    throw "SSH PowerShell failed on $HostAlias (exit $exitCode): $remoteMessage"
  }
  $clean = @($output | ForEach-Object { [string]$_ } | Where-Object { $_ -notmatch '^#< CLIXML$' -and $_ -notmatch '^<Objs Version=' })
  return ($clean -join [Environment]::NewLine).Trim()
}

function Send-RemoteTextFile {
  param(
    [string]$HostAlias,
    [string]$LocalPath,
    [string]$RemoteUserHome,
    [string]$RemoteSubdirectory,
    [string]$RemoteName
  )

  $remotePath = ($RemoteUserHome.TrimEnd('\') + '\.codex\' + $RemoteSubdirectory.Trim('\') + '\' + $RemoteName).Replace('\', '/')
  & $ScpExe -q -o BatchMode=yes -o ConnectTimeout=10 -- $LocalPath "${HostAlias}:$remotePath"
  if ($LASTEXITCODE -ne 0) {
    throw "SCP failed while copying $RemoteName to $HostAlias"
  }
}

function Get-RemoteInstallFiles {
  param(
    [string]$SourceRoot,
    [string]$PrivateConfig,
    [bool]$RemotePrivateConfigExists,
    [switch]$ReplaceRemoteConfig
  )

  $files = [ordered]@{
    'notify-ntfy.ps1' = (Join-Path $SourceRoot 'notify-ntfy.ps1')
    'watch-codex-ntfy.ps1' = (Join-Path $SourceRoot 'watch-codex-ntfy.ps1')
    'watch-codex-ntfy-hidden.vbs' = (Join-Path $SourceRoot 'watch-codex-ntfy-hidden.vbs')
    'install-remote-windows-target.ps1' = (Join-Path $SourceRoot 'install-remote-windows-target.ps1')
  }
  # Existing destinations keep their own credentials and host-local settings.
  # A source private config is needed only for a fresh install or explicit reset.
  if ($ReplaceRemoteConfig -or -not $RemotePrivateConfigExists) {
    if (-not (Test-Path -LiteralPath $PrivateConfig -PathType Leaf)) {
      throw "Private config not found: $PrivateConfig"
    }
    $files['ntfy-config.json'] = $PrivateConfig
  }
  return $files
}

function Get-RemoteOwnershipHelpers {
  param([string]$Path)

  $tokens = $null
  $errors = $null
  $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
  if ($errors.Count -gt 0) { throw 'Remote target installer is invalid.' }
  $helpers = foreach ($name in @('Get-NotifierCommandTokens', 'Test-OwnedNotifierInvocation', 'Test-OwnedNotifierProcess', 'Test-OwnedScheduledTask')) {
    $node = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $false)
    if ($null -eq $node) { throw "Missing remote ownership helper: $name" }
    $node.Extent.Text
  }
  return $helpers -join [Environment]::NewLine
}
foreach ($required in @('notify-ntfy.ps1', 'watch-codex-ntfy.ps1', 'watch-codex-ntfy-hidden.vbs', 'install-remote-windows-target.ps1')) {
  if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $required))) {
    throw "Missing source file: $required"
  }
}
$ownershipHelpers = Get-RemoteOwnershipHelpers -Path (Join-Path $SourceRoot 'install-remote-windows-target.ps1')

foreach ($hostAlias in $HostName) {
  Write-Host "[codex-ntfy] Installing on SSH host $hostAlias ..."
  $stageName = 'ntfy-stage-' + [Guid]::NewGuid().ToString('N')
  $prepare = @'
$ErrorActionPreference = 'Stop'
__OWNERSHIP_HELPERS__
$homePath = Join-Path $env:USERPROFILE '.codex'
New-Item -ItemType Directory -Path $homePath -Force | Out-Null
$task = Get-ScheduledTask -TaskName 'CodexNtfyWatcher' -ErrorAction SilentlyContinue
if (__MANAGE_TASK__ -and $null -ne $task) {
  if (-not (Test-OwnedScheduledTask -Task $task -HomePath $homePath)) { throw 'CodexNtfyWatcher exists but is unrelated; refusing to overwrite it.' }
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$stage = Join-Path $homePath '__STAGE__'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
& icacls.exe $stage /inheritance:r /grant:r "${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote staging directory.' }
$backup = Join-Path $homePath ('ntfy-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
& icacls.exe $backup /inheritance:r /grant:r "${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect remote backup directory.' }
$managed = @('notify-ntfy.ps1','watch-codex-ntfy.ps1','watch-codex-ntfy-hidden.vbs','install-remote-windows-target.ps1','ntfy-config.json','config.toml','hooks.json')
foreach ($name in $managed) {
  $source = Join-Path $homePath $name
  if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $backup $name) -Force }
}
if ($null -ne $task) {
  $taskXml = (Export-ScheduledTask -TaskName 'CodexNtfyWatcher') -replace '(?i)encoding="utf-16"', 'encoding="utf-8"'
  [IO.File]::WriteAllText((Join-Path $backup 'CodexNtfyWatcher.xml'), $taskXml, (New-Object Text.UTF8Encoding($false)))
}
$backupRoot = Split-Path -Parent $backup
Get-ChildItem -LiteralPath $backupRoot -Directory | Sort-Object Name -Descending | Select-Object -Skip 10 | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
[ordered]@{ user_home=$env:USERPROFILE; backup=$backup; private_config_existed=[bool](Test-Path -LiteralPath (Join-Path $homePath 'ntfy-config.json') -PathType Leaf); task_existed=[bool]($null -ne $task); task_running=[bool]($null -ne $task -and [string]$task.State -eq 'Running') } | ConvertTo-Json -Compress
'@
  $prepare = $prepare.Replace('__STAGE__', $stageName)
  $prepare = $prepare.Replace('__OWNERSHIP_HELPERS__', $ownershipHelpers)
  $prepare = $prepare.Replace('__MANAGE_TASK__', $(if ($SkipScheduledTask) { '$false' } else { '$true' }))
  $prepared = Invoke-RemotePowerShell -HostAlias $hostAlias -Script $prepare | ConvertFrom-Json
  $remoteUserHome = [string]$prepared.user_home
  if ([string]::IsNullOrWhiteSpace($remoteUserHome) -or [string]::IsNullOrWhiteSpace([string]$prepared.backup)) {
    throw "Could not determine USERPROFILE on $hostAlias"
  }

  try {
    $filesToSend = Get-RemoteInstallFiles -SourceRoot $SourceRoot -PrivateConfig $PrivateConfig `
      -RemotePrivateConfigExists ([bool]$prepared.private_config_existed) -ReplaceRemoteConfig:$ReplaceRemoteConfig
    $copyPrivateConfigLiteral = if ($filesToSend.Contains('ntfy-config.json')) { '$true' } else { '$false' }
    $expectedHashes = [ordered]@{}
    foreach ($entry in $filesToSend.GetEnumerator()) {
      Send-RemoteTextFile -HostAlias $hostAlias -LocalPath $entry.Value -RemoteUserHome $remoteUserHome -RemoteSubdirectory $stageName -RemoteName $entry.Key
      $expectedHashes[$entry.Key] = (Get-FileHash -LiteralPath $entry.Value -Algorithm SHA256).Hash
    }

    $safeOrigin = 'SSH:' + $hostAlias
    $escapedBackup = ([string]$prepared.backup).Replace("'", "''")
    $escapedHashes = ($expectedHashes | ConvertTo-Json -Compress).Replace("'", "''")
    $skipLiteral = if ($SkipScheduledTask) { '$true' } else { '$false' }
    $taskExistedLiteral = if ([bool]$prepared.task_existed) { '$true' } else { '$false' }
    $taskRunningLiteral = if ([bool]$prepared.task_running) { '$true' } else { '$false' }
    $finish = @"
`$ErrorActionPreference = 'Stop'
$ownershipHelpers
`$homePath = Join-Path `$env:USERPROFILE '.codex'
`$stage = Join-Path `$homePath '$stageName'
`$backup = '$escapedBackup'
`$managed = @('notify-ntfy.ps1','watch-codex-ntfy.ps1','watch-codex-ntfy-hidden.vbs','install-remote-windows-target.ps1','ntfy-config.json')
if (-not $skipLiteral) {
  `$currentTask = Get-ScheduledTask -TaskName 'CodexNtfyWatcher' -ErrorAction SilentlyContinue
  if (`$null -ne `$currentTask -and -not (Test-OwnedScheduledTask -Task `$currentTask -HomePath `$homePath)) { throw 'CodexNtfyWatcher ownership changed; refusing to stop it.' }
}
try {
  `$expectedHashes = '$escapedHashes' | ConvertFrom-Json
  foreach (`$property in `$expectedHashes.PSObject.Properties) {
    `$stagedFile = Join-Path `$stage `$property.Name
    if (-not (Test-Path -LiteralPath `$stagedFile)) { throw "Missing staged file: `$(`$property.Name)" }
    `$actualHash = (Get-FileHash -LiteralPath `$stagedFile -Algorithm SHA256).Hash
    if (`$actualHash -ne [string]`$property.Value) { throw "Hash mismatch for staged file: `$(`$property.Name)" }
  }
  if (-not $skipLiteral) {
    Stop-ScheduledTask -TaskName 'CodexNtfyWatcher' -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    `$oldWorkers = Get-CimInstance Win32_Process | Where-Object { Test-OwnedNotifierProcess -Process `$_ -HomePath `$homePath }
    foreach (`$process in `$oldWorkers) {
      `$runtime = `$null
      try {
        `$runtime = [Diagnostics.Process]::GetProcessById([int]`$process.ProcessId)
        [void]`$runtime.Handle
        `$current = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f [int]`$process.ProcessId) -ErrorAction Stop
        `$startTicks = `$runtime.StartTime.ToUniversalTime().Ticks
        if (`$null -ne `$current -and `$current.CreationDate -eq `$process.CreationDate -and
            (`$startTicks - (`$startTicks % 10)) -eq `$current.CreationDate.ToUniversalTime().Ticks -and
            (Test-OwnedNotifierProcess -Process `$current -HomePath `$homePath)) {
          `$runtime.Kill()
        }
      } catch [ArgumentException] {
        # The observed process exited before its handle could be acquired.
      } catch [InvalidOperationException] {
        # Never retry an exited process using a potentially reused PID.
      } finally {
        if (`$null -ne `$runtime) { `$runtime.Dispose() }
      }
    }
  }
  foreach (`$name in `$managed) {
    if (`$name -eq 'ntfy-config.json' -and -not $copyPrivateConfigLiteral) { continue }
    `$source = Join-Path `$stage `$name
    `$target = Join-Path `$homePath `$name
    `$swap = `$target + '.new'
    Move-Item -LiteralPath `$source -Destination `$swap -Force
    if (Test-Path -LiteralPath `$target) {
      `$replaceBackup = `$target + '.replace-backup'
      Remove-Item -LiteralPath `$replaceBackup -Force -ErrorAction SilentlyContinue
      [IO.File]::Replace(`$swap, `$target, `$replaceBackup)
      Remove-Item -LiteralPath `$replaceBackup -Force -ErrorAction SilentlyContinue
    } else {
      [IO.File]::Move(`$swap, `$target)
    }
  }
  `$powerShell = Join-Path `$env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
  `$targetInstaller = Join-Path `$homePath 'install-remote-windows-target.ps1'
  `$arguments = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',`$targetInstaller,'-Origin','$safeOrigin')
  if (-not $copyPrivateConfigLiteral) { `$arguments += '-PreserveLocalConfiguration' }
  if ($skipLiteral) { `$arguments += '-SkipScheduledTask' }
  `$result = & `$powerShell @arguments 2>&1
  if (`$LASTEXITCODE -ne 0) { throw "Remote target installer exited `$LASTEXITCODE" }
  [Console]::Out.Write((`$result -join [Environment]::NewLine))
} catch {
  foreach (`$name in @(`$managed + 'config.toml' + 'hooks.json')) {
    `$saved = Join-Path `$backup `$name
    `$target = Join-Path `$homePath `$name
    if (Test-Path -LiteralPath `$saved) { Copy-Item -LiteralPath `$saved -Destination `$target -Force }
    else { Remove-Item -LiteralPath `$target -Force -ErrorAction SilentlyContinue }
  }
  if (-not $skipLiteral) {
    `$taskXml = Join-Path `$backup 'CodexNtfyWatcher.xml'
    if ($taskExistedLiteral -and (Test-Path -LiteralPath `$taskXml)) {
      `$taskXmlContent = [IO.File]::ReadAllText(`$taskXml) -replace '^\s*<\?xml[^?]*\?>', ''
      Register-ScheduledTask -TaskName 'CodexNtfyWatcher' -Xml `$taskXmlContent -Force | Out-Null
      if ($taskRunningLiteral) { Start-ScheduledTask -TaskName 'CodexNtfyWatcher' -ErrorAction SilentlyContinue }
    } elseif (-not $taskExistedLiteral) {
      Unregister-ScheduledTask -TaskName 'CodexNtfyWatcher' -Confirm:`$false -ErrorAction SilentlyContinue
    }
  }
  throw
} finally {
  Remove-Item -LiteralPath `$stage -Recurse -Force -ErrorAction SilentlyContinue
}
"@
    $result = Invoke-RemotePowerShell -HostAlias $hostAlias -Script $finish
    Write-Host "[codex-ntfy] Remote result: $result"
  } finally {
    $cleanup = "Remove-Item -LiteralPath (Join-Path (Join-Path `$env:USERPROFILE '.codex') '$stageName') -Recurse -Force -ErrorAction SilentlyContinue"
    try {
      Invoke-RemotePowerShell -HostAlias $hostAlias -Script $cleanup | Out-Null
    } catch {
      Write-Warning "Could not remove the private staging directory on $hostAlias."
    }
  }
}
