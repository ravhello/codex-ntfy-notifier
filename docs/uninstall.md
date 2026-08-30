# Uninstall and rollback

These procedures apply to version 2.6.0. They are intentionally explicit because `~/.codex`, `~/.claude`, and `~/.openclaude` belong to their respective tools and may contain unrelated configuration; never remove those whole directories.

An **uninstall** removes only this project’s managed `notify` command, `notify-ntfy` hook handlers, scripts, and worker while preserving unrelated Codex settings and hooks. A **rollback** restores the timestamped snapshot taken immediately before a particular installation or upgrade. Decide which outcome is wanted before deleting anything.

## Before changing files

1. Close or reload affected Codex, Claude Code, and AudnCode surfaces after the procedure so they do not retain old hook configuration. AudnCode normally hot-reloads for the next turn, but an already-running turn should finish first.
2. Run doctor and inspect both `pending_idle` and `queued`. Wait for both to reach zero, or explicitly accept that idle candidates and network-ready notifications will be discarded.
3. Select the correct host and user. Each local, WSL, and Remote SSH environment can have a separate `~/.codex`.
4. Make a private copy of any config or state that may be needed for rollback. It can contain credentials and message content.
5. Do not print or upload `ntfy-config.json`, `hooks.json`, Codex rollout/database state, notifier state, or backups.

Windows:

```powershell
& "$HOME\.codex\notify-ntfy.ps1" -Doctor
Get-ChildItem "$HOME\.codex\ntfy-backups" -Directory |
  Sort-Object Name -Descending |
  Select-Object Name, LastWriteTime
```

Linux/WSL:

```sh
python3 "$HOME/.codex/notify-ntfy.py" --doctor
find "$HOME/.codex/ntfy-backups" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -r
```

The newest backup is the state immediately before the newest installer run, not necessarily the state you want. Inspect filenames and timestamps without displaying private file contents.

## Remove a Windows installation

Run these commands in Windows PowerShell as the same user that installed the notifier.

### 1. Stop and remove the managed worker

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex')) # replace when customized
$ManagedNotifier = [IO.Path]::GetFullPath((Join-Path $CodexHome 'notify-ntfy.ps1'))
$ManagedWatcher = [IO.Path]::GetFullPath((Join-Path $CodexHome 'watch-codex-ntfy.ps1'))
$ManagedSupervisor = [IO.Path]::GetFullPath((Join-Path $CodexHome 'watch-codex-ntfy-hidden.vbs'))

if (-not ('CodexNtfy.Uninstall.NativeCommandLine' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CodexNtfy.Uninstall {
  public static class NativeCommandLine {
    [DllImport("shell32.dll", SetLastError = true)]
    public static extern IntPtr CommandLineToArgvW(
      [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
      out int argumentCount
    );
    [DllImport("kernel32.dll")]
    public static extern IntPtr LocalFree(IntPtr memory);
  }
}
'@
}

function Split-WindowsCommandLine {
  param([AllowEmptyString()][string]$CommandLine)
  if ([string]::IsNullOrWhiteSpace($CommandLine) -or $CommandLine.Length -gt 131072) { return }
  $Count = 0
  $Pointer = [CodexNtfy.Uninstall.NativeCommandLine]::CommandLineToArgvW($CommandLine, [ref]$Count)
  if ($Pointer -eq [IntPtr]::Zero -or $Count -le 0) { return }
  try {
    for ($Index = 0; $Index -lt $Count; $Index++) {
      $Item = [Runtime.InteropServices.Marshal]::ReadIntPtr($Pointer, $Index * [IntPtr]::Size)
      [Runtime.InteropServices.Marshal]::PtrToStringUni($Item)
    }
  } finally {
    [void][CodexNtfy.Uninstall.NativeCommandLine]::LocalFree($Pointer)
  }
}

function Test-ArgumentPresent {
  param([string[]]$Arguments, [string]$Expected)
  return @($Arguments | Where-Object {
    [string]::Equals([string]$_, $Expected, [StringComparison]::OrdinalIgnoreCase)
  }).Count -gt 0
}

function Test-OwnedNotifierProcess {
  param([object]$Process)
  $Name = [string]$Process.Name
  $CommandLine = [string]$Process.CommandLine
  $Arguments = @(Split-WindowsCommandLine $CommandLine)
  if ($Arguments.Count -lt 2 -or
      -not [string]::Equals([IO.Path]::GetFileName($Arguments[0]), $Name, [StringComparison]::OrdinalIgnoreCase)) {
    return $false
  }
  if ($Name -ieq 'wscript.exe') {
    if ($Arguments.Count -ne 4 -or $Arguments[1] -ine '//B' -or $Arguments[2] -ine '//Nologo') { return $false }
    try {
      return [string]::Equals(
        [IO.Path]::GetFullPath($Arguments[3]),
        $ManagedSupervisor,
        [StringComparison]::OrdinalIgnoreCase
      )
    } catch { return $false }
  }
  if ($Name -notin @('powershell.exe', 'pwsh.exe')) { return $false }
  $FileIndexes = @(for ($Index = 1; $Index -lt $Arguments.Count; $Index++) {
    if ($Arguments[$Index] -ieq '-File') { $Index }
  })
  if ($FileIndexes.Count -ne 1 -or $FileIndexes[0] + 1 -ge $Arguments.Count) { return $false }
  try { $Script = [IO.Path]::GetFullPath($Arguments[$FileIndexes[0] + 1]) } catch { return $false }
  if ([string]::Equals($Script, $ManagedWatcher, [StringComparison]::OrdinalIgnoreCase)) { return $true }
  if (-not [string]::Equals($Script, $ManagedNotifier, [StringComparison]::OrdinalIgnoreCase)) { return $false }
  foreach ($Mode in @('-Worker', '-Continuous', '-ScanRollouts', '-Maintenance')) {
    if (Test-ArgumentPresent $Arguments $Mode) { return $true }
  }
  return $false
}

function Test-OwnedWatcherTask {
  param([object]$Task)
  if ($null -eq $Task) { return $false }
  $Actions = @($Task.Actions)
  if ($Actions.Count -ne 1) { return $false }
  try {
    $ExpectedExecutable = [IO.Path]::GetFullPath((Join-Path $env:WINDIR 'System32\wscript.exe'))
    $ActualExecutable = [IO.Path]::GetFullPath([string]$Actions[0].Execute)
    $ActualWorkingDirectory = [IO.Path]::GetFullPath([string]$Actions[0].WorkingDirectory)
    $ExpectedArguments = '//B //Nologo "{0}"' -f $ManagedSupervisor
    return [string]::Equals($ActualExecutable, $ExpectedExecutable, [StringComparison]::OrdinalIgnoreCase) -and
      [string]::Equals([string]$Actions[0].Arguments, $ExpectedArguments, [StringComparison]::OrdinalIgnoreCase) -and
      [string]::Equals($ActualWorkingDirectory, $CodexHome, [StringComparison]::OrdinalIgnoreCase)
  } catch { return $false }
}

$Task = Get-ScheduledTask -TaskName CodexNtfyWatcher -ErrorAction SilentlyContinue
if ($null -ne $Task) {
  if (-not (Test-OwnedWatcherTask $Task)) { throw 'CodexNtfyWatcher is not owned by this Codex home; preserved.' }
  $CurrentTask = Get-ScheduledTask -TaskName CodexNtfyWatcher -ErrorAction SilentlyContinue
  if ($null -ne $CurrentTask) {
    if (-not (Test-OwnedWatcherTask $CurrentTask)) { throw 'CodexNtfyWatcher changed before stop; preserved.' }
    Stop-ScheduledTask -TaskName CodexNtfyWatcher -ErrorAction SilentlyContinue
    $CurrentTask = Get-ScheduledTask -TaskName CodexNtfyWatcher -ErrorAction SilentlyContinue
    if ($null -ne $CurrentTask) {
      if (-not (Test-OwnedWatcherTask $CurrentTask)) { throw 'CodexNtfyWatcher changed before removal; preserved.' }
      Unregister-ScheduledTask -TaskName CodexNtfyWatcher -Confirm:$false
    }
  }
}

$Snapshots = @(Get-CimInstance Win32_Process | Where-Object { Test-OwnedNotifierProcess $_ } | ForEach-Object {
  [pscustomobject]@{
    ProcessId = [int]$_.ProcessId
    CreationDate = $_.CreationDate
    Name = [string]$_.Name
    ExecutablePath = [string]$_.ExecutablePath
    CommandLine = [string]$_.CommandLine
  }
})
foreach ($Snapshot in $Snapshots) {
  $Current = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $Snapshot.ProcessId) -ErrorAction SilentlyContinue
  if ($null -eq $Current) { continue }
  if ($Current.CreationDate -ne $Snapshot.CreationDate -or
      [string]$Current.Name -cne $Snapshot.Name -or
      [string]$Current.ExecutablePath -cne $Snapshot.ExecutablePath -or
      [string]$Current.CommandLine -cne $Snapshot.CommandLine -or
      -not (Test-OwnedNotifierProcess $Current)) {
    throw "Process identity changed before stop; preserved PID $($Snapshot.ProcessId)."
  }
  Stop-Process -Id $Snapshot.ProcessId -Force
}
```

The task and process checks are deliberately tied to the selected canonical `CodexHome`. An unrelated task, a command that merely contains a similar filename, or a PID whose creation time/command changed between discovery and use is preserved instead of being stopped.

### 2. Restore or remove the managed hooks

The installation has two independent signal registrations:

- the root-level legacy `notify` command in `config.toml`;
- one managed command handler containing `notify-ntfy` under `hooks.Stop` in `hooks.json`.

If no Codex settings changed after installation, restoring `config.toml` from the chosen backup is the most exact legacy-notify rollback:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
$Backup = Join-Path $CodexHome 'ntfy-backups\YYYYMMDD-HHMMSS-fff' # choose explicitly
Copy-Item -LiteralPath (Join-Path $Backup 'config.toml') `
  -Destination (Join-Path $CodexHome 'config.toml') -Force
```

Do not run that copy if the selected backup has no `config.toml` or if it would overwrite later Codex settings. In that case, privately back up the current file and remove only the root-level line whose command contains `notify-ntfy.ps1`. Leave every unrelated line and table unchanged.

Verify afterwards:

```powershell
Select-String -Path "$HOME\.codex\config.toml" -Pattern '^\s*notify\s*='
```

If a previous non-project hook should be restored, copy its exact root-level `notify = [...]` line from a trusted pre-install backup. Do not add a second root-level `notify` key.

Remove the modern handler selectively. This script scans every hook event, but removes only the current local or Remote Windows managed command shape: the exact Windows PowerShell executable, exact canonical installed script after `-File`, fixed launcher switches, optional quoted remote `-Origin`, and final `-HookEvent` marker must all match. It retains unrelated or historical unknown handlers, groups, events, and top-level metadata, then uses a same-directory atomic replacement after comparing the original bytes again:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex')) # replace when customized
$HooksPath = Join-Path $CodexHome 'hooks.json'
$ManagedScript = [IO.Path]::GetFullPath((Join-Path $CodexHome 'notify-ntfy.ps1'))
$Utf8Strict = New-Object Text.UTF8Encoding($false, $true)

function Test-ManagedCodexHookHandler {
  param([object]$Handler, [string]$ExpectedScript)
  if ($null -eq $Handler -or $Handler -isnot [System.Management.Automation.PSCustomObject] -or
      [string]$Handler.type -ne 'command') { return $false }
  $CommandProperty = $Handler.PSObject.Properties['command']
  if ($null -eq $CommandProperty -or $CommandProperty.Value -isnot [string]) { return $false }
  $WindowsPowerShell = [IO.Path]::GetFullPath((Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'))
  $Prefix = '"{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}"' -f
    $WindowsPowerShell, $ExpectedScript
  $Pattern = '(?i)^' + [regex]::Escape($Prefix) +
    '(?:\s+-Origin\s+"(?:\\.|[^"\\])*")?\s+-HookEvent$'
  return [string]$CommandProperty.Value -match $Pattern
}

if (Test-Path -LiteralPath $HooksPath -PathType Leaf) {
  $OriginalBytes = [IO.File]::ReadAllBytes($HooksPath)
  $BomOffset = if ($OriginalBytes.Length -ge 3 -and $OriginalBytes[0] -eq 0xEF -and
      $OriginalBytes[1] -eq 0xBB -and $OriginalBytes[2] -eq 0xBF) { 3 } else { 0 }
  $OriginalText = $Utf8Strict.GetString($OriginalBytes, $BomOffset, $OriginalBytes.Length - $BomOffset)
  try { $Document = $OriginalText | ConvertFrom-Json } catch { throw "Invalid JSON in ${HooksPath}: $($_.Exception.Message)" }
  if ($null -eq $Document -or $Document -isnot [System.Management.Automation.PSCustomObject]) {
    throw "$HooksPath must contain a JSON object."
  }
  $HooksProperty = $Document.PSObject.Properties['hooks']
  $Changed = $false

  if ($null -ne $HooksProperty -and $null -ne $HooksProperty.Value) {
    if ($HooksProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
      throw "hooks in $HooksPath must contain a JSON object."
    }
    $Events = $HooksProperty.Value
    foreach ($EventProperty in @($Events.PSObject.Properties)) {
      if ($EventProperty.Value -isnot [array]) { continue }

      $KeptGroups = New-Object 'System.Collections.Generic.List[object]'
      $RemovedFromEvent = $false
      foreach ($Group in @($EventProperty.Value)) {
        if ($null -eq $Group -or $Group -isnot [System.Management.Automation.PSCustomObject]) {
          $KeptGroups.Add($Group)
          continue
        }
        $HandlersProperty = $Group.PSObject.Properties['hooks']
        if ($null -eq $HandlersProperty -or $HandlersProperty.Value -isnot [array]) {
          $KeptGroups.Add($Group)
          continue
        }

        $OriginalHandlers = @($HandlersProperty.Value)
        $KeptHandlers = @($OriginalHandlers | Where-Object { -not (Test-ManagedCodexHookHandler $_ $ManagedScript) })

        if ($KeptHandlers.Count -ne $OriginalHandlers.Count) {
          $RemovedFromEvent = $true
        }
        if ($KeptHandlers.Count -gt 0) {
          $HandlersProperty.Value = @($KeptHandlers)
          $KeptGroups.Add($Group)
        } elseif ($OriginalHandlers.Count -eq 0) {
          $KeptGroups.Add($Group)
        }
      }

      if ($RemovedFromEvent) {
        $Changed = $true
        if ($KeptGroups.Count -gt 0) {
          $EventProperty.Value = @($KeptGroups.ToArray())
        } else {
          $Events.PSObject.Properties.Remove($EventProperty.Name)
        }
      }
    }
  }

  if ($Changed) {
    $Rendered = ($Document | ConvertTo-Json -Depth 100) + [Environment]::NewLine
    [void]$Utf8Strict.GetByteCount($Rendered)
    $OriginalAcl = try { [IO.File]::GetAccessControl($HooksPath) } catch {
      Get-Acl -LiteralPath $HooksPath -ErrorAction Stop
    }
    $AclSections = [Security.AccessControl.AccessControlSections]::Access -bor
      [Security.AccessControl.AccessControlSections]::Owner -bor
      [Security.AccessControl.AccessControlSections]::Group
    $OriginalSddl = $OriginalAcl.GetSecurityDescriptorSddlForm($AclSections)
    $NormalizeAclSddl = { param([string]$Sddl) [regex]::Replace($Sddl, 'D:(P?)(AR)?AI(?=\()', 'D:$1$2') }
    $PrivateBackup = "$HooksPath.pre-ntfy-uninstall-$([Guid]::NewGuid().ToString('N'))"
    $TempPath = Join-Path (Split-Path -Parent $HooksPath) ('.' + (Split-Path -Leaf $HooksPath) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
      $Empty = [IO.File]::Open($TempPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
      $Empty.Dispose()
      $TempAcl = try { [IO.File]::GetAccessControl($TempPath) } catch {
        Get-Acl -LiteralPath $TempPath -ErrorAction Stop
      }
      $InitialTempSddl = $TempAcl.GetSecurityDescriptorSddlForm($AclSections)
      if (-not [string]::Equals($InitialTempSddl, $OriginalSddl, [StringComparison]::Ordinal)) {
        $TargetOwner = $OriginalAcl.GetOwner([Security.Principal.SecurityIdentifier])
        $TargetGroup = $OriginalAcl.GetGroup([Security.Principal.SecurityIdentifier])
        if ($TempAcl.GetOwner([Security.Principal.SecurityIdentifier]) -ne $TargetOwner) { $TempAcl.SetOwner($TargetOwner) }
        if ($TempAcl.GetGroup([Security.Principal.SecurityIdentifier]) -ne $TargetGroup) { $TempAcl.SetGroup($TargetGroup) }
        if ($TempAcl.AreAccessRulesProtected -ne $OriginalAcl.AreAccessRulesProtected) {
          $TempAcl.SetAccessRuleProtection($OriginalAcl.AreAccessRulesProtected, $false)
        }
        foreach ($Rule in @($TempAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
          [void]$TempAcl.RemoveAccessRuleSpecific($Rule)
        }
        foreach ($Rule in @($OriginalAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
          [void]$TempAcl.AddAccessRule($Rule)
        }
        try { [IO.File]::SetAccessControl($TempPath, $TempAcl) } catch {
          Set-Acl -LiteralPath $TempPath -AclObject $TempAcl -ErrorAction Stop
        }
      }
      $PreparedAcl = try { [IO.File]::GetAccessControl($TempPath) } catch {
        Get-Acl -LiteralPath $TempPath -ErrorAction Stop
      }
      $PreparedSddl = $PreparedAcl.GetSecurityDescriptorSddlForm($AclSections)
      if (-not [string]::Equals((& $NormalizeAclSddl $PreparedSddl), (& $NormalizeAclSddl $OriginalSddl), [StringComparison]::Ordinal)) {
        throw "temporary hooks.json ACL is not equivalent to the destination"
      }
      [IO.File]::WriteAllText($TempPath, $Rendered, $Utf8Strict)
      $CurrentBytes = [IO.File]::ReadAllBytes($HooksPath)
      if (-not [Linq.Enumerable]::SequenceEqual([byte[]]$OriginalBytes, [byte[]]$CurrentBytes)) {
        throw "hooks.json changed during uninstall; no changes were written: $HooksPath"
      }
      [IO.File]::Replace($TempPath, $HooksPath, $PrivateBackup, $false)
      $InstalledAcl = try { [IO.File]::GetAccessControl($HooksPath) } catch {
        Get-Acl -LiteralPath $HooksPath -ErrorAction Stop
      }
      $InstalledSddl = $InstalledAcl.GetSecurityDescriptorSddlForm($AclSections)
      if (-not [string]::Equals((& $NormalizeAclSddl $InstalledSddl), (& $NormalizeAclSddl $OriginalSddl), [StringComparison]::Ordinal)) {
        $FailedPath = "$HooksPath.failed-ntfy-uninstall-$([Guid]::NewGuid().ToString('N'))"
        try { [IO.File]::Replace($PrivateBackup, $HooksPath, $FailedPath, $false) } finally {
          if (Test-Path -LiteralPath $FailedPath) { Remove-Item -LiteralPath $FailedPath -Force }
        }
        throw "hooks.json ACL changed during uninstall; the original file was restored"
      }
    } finally {
      if (Test-Path -LiteralPath $TempPath) { Remove-Item -LiteralPath $TempPath -Force }
    }
  }
}
```

If `$Changed` was true, inspect the diff against `$PrivateBackup` locally. Rerunning the same block should be an idempotent no-op. A broad text search can still find an unrelated tool with a similar name, so it is not ownership proof; the exact structured predicate above is the verification boundary. Preserve an older or custom shape that does not match it and compare that handler with its trusted installation backup before removing it manually.

Do not delete all of `hooks.json` and do not edit Codex’s hook trust store. A retained approval does not execute anything without a registered hook command; removing trust entries is outside this uninstall.

#### Remove the optional Claude Code handlers

Skip this block if the notifier was not installed with `-EnableClaudeCode`. If installation used a custom `-CodexHome` or `-ClaudeHome`, set the same absolute directories below instead of the defaults. The managed set can appear under `Stop`, `StopFailure`, `UserPromptSubmit`, and `Notification`. This cleanup removes only handlers carrying the managed `-ClaudeHook` marker and the exact installed script path; all other Claude settings and hooks remain:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex')) # replace with the installed -CodexHome when customized
$ManagedScript = [IO.Path]::GetFullPath((Join-Path $CodexHome 'notify-ntfy.ps1'))
$ClaudeHome = [IO.Path]::GetFullPath((Join-Path $HOME '.claude')) # replace with the installed -ClaudeHome when customized
$SettingsPath = Join-Path $ClaudeHome 'settings.json'
if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
  $Document = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
  $HooksProperty = $Document.PSObject.Properties['hooks']
  $Changed = $false
  if ($null -ne $HooksProperty -and
      $null -ne $HooksProperty.Value -and
      $HooksProperty.Value -is [System.Management.Automation.PSCustomObject]) {
    $Events = $HooksProperty.Value
    foreach ($EventProperty in @($Events.PSObject.Properties)) {
      if ($EventProperty.Value -isnot [array]) { continue }
      $KeptGroups = New-Object 'System.Collections.Generic.List[object]'
      $RemovedFromEvent = $false
      foreach ($Group in @($EventProperty.Value)) {
        if ($null -eq $Group -or $Group -isnot [System.Management.Automation.PSCustomObject]) {
          $KeptGroups.Add($Group)
          continue
        }
        $HandlersProperty = $Group.PSObject.Properties['hooks']
        if ($null -eq $HandlersProperty -or $HandlersProperty.Value -isnot [array]) {
          $KeptGroups.Add($Group)
          continue
        }
        $OriginalHandlers = @($HandlersProperty.Value)
        $KeptHandlers = @($OriginalHandlers | Where-Object {
          $Managed = $false
          if ($null -ne $_ -and $_ -is [System.Management.Automation.PSCustomObject]) {
            $Args = @($_.args | ForEach-Object { [string]$_ })
            $FileIndex = [Array]::IndexOf([string[]]$Args, '-File')
            if (($Args -contains '-ClaudeHook') -and $FileIndex -ge 0 -and $FileIndex + 1 -lt $Args.Count) {
              try {
                $Managed = [string]::Equals(
                  [IO.Path]::GetFullPath($Args[$FileIndex + 1]),
                  $ManagedScript,
                  [StringComparison]::OrdinalIgnoreCase
                )
              } catch { $Managed = $false }
            }
          }
          -not $Managed
        })
        if ($KeptHandlers.Count -ne $OriginalHandlers.Count) { $RemovedFromEvent = $true }
        if ($KeptHandlers.Count -gt 0) {
          $HandlersProperty.Value = @($KeptHandlers)
          $KeptGroups.Add($Group)
        } elseif ($OriginalHandlers.Count -eq 0) {
          $KeptGroups.Add($Group)
        }
      }
      if ($RemovedFromEvent) {
        $Changed = $true
        if ($KeptGroups.Count -gt 0) {
          $EventProperty.Value = @($KeptGroups.ToArray())
        } else {
          $Events.PSObject.Properties.Remove($EventProperty.Name)
        }
      }
    }
  }
  if ($Changed) {
    $PrivateBackup = "$SettingsPath.pre-ntfy-uninstall-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item -LiteralPath $SettingsPath -Destination $PrivateBackup
    $Utf8NoBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($SettingsPath, (($Document | ConvertTo-Json -Depth 32) + [Environment]::NewLine), $Utf8NoBom)
  }
}
```

Verify locally with `Select-String -LiteralPath $SettingsPath -Pattern ([regex]::Escape($ManagedScript))`. An empty result confirms removal of this installation. Do not delete the whole Claude settings file.

#### Remove the optional AudnCode handlers

Skip this block if the notifier was not installed with `-EnableAudnCode`. Close every AudnCode window so it cannot rewrite `settings.json` during the transaction. Version 2.6.0 shape 9 has seven synchronous events: `SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`, `Notification: ^(idle_prompt|permission_prompt)$`, `PostToolUse: ^(Agent|AskUserQuestion|Bash|PowerShell|Monitor|TaskStop|KillShell|CronCreate|CronDelete|SendMessage)$`, and `SubagentStart`, all with 60-second timeouts. The cleanup scans every event so the current exact command shape and an earlier structured exec/args shape are removed. Set the same custom `-CodexHome`/`-AudnCodeHome` used at install time. Both predicates require the exact installed script, AudnCode home, origin, input marker, and hook marker; unrelated settings, groups, and handlers remain. The code rejects malformed UTF-8, replacement characters, and invalid JSON scalars before any write:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex')) # replace when customized
$ManagedScript = [IO.Path]::GetFullPath((Join-Path $CodexHome 'notify-ntfy.ps1'))
$ManagedQuotedScript = "'" + $ManagedScript.Replace("'", "''") + "'"
$AudnHome = if ([string]::IsNullOrWhiteSpace($env:CLAUDE_CONFIG_DIR)) {
  [IO.Path]::GetFullPath((Join-Path $HOME '.openclaude'))
} else {
  [IO.Path]::GetFullPath($env:CLAUDE_CONFIG_DIR)
} # replace with the installed -AudnCodeHome when explicitly customized
$SettingsPath = Join-Path $AudnHome 'settings.json'
$ManagedQuotedHome = "'" + $AudnHome.Replace("'", "''") + "'"
$AllowedAudnEvents = @('SessionStart', 'UserPromptSubmit', 'Stop', 'StopFailure', 'Notification', 'PostToolUse', 'SubagentStart')
$ManagedCommandPrefix = "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; & " +
  $ManagedQuotedScript + " -AudnCodeHook -ReadStdin -Origin 'AudnCode' -AudnCodeHome " + $ManagedQuotedHome
$ManagedCommandPattern = '(?i)^' + [regex]::Escape($ManagedCommandPrefix) +
  '\s+-AudnCodeExpectedEvent\s+''(?:' + (($AllowedAudnEvents | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')''$'

$Utf8Strict = New-Object Text.UTF8Encoding($false, $true)
function Assert-SafeUnicodeScalarText {
  param([AllowEmptyString()][string]$Text, [string]$Label)
  for ($Index = 0; $Index -lt $Text.Length; $Index++) {
    $Code = [int][char]$Text[$Index]
    if ($Code -eq 0xFFFD) { throw "$Label contains U+FFFD replacement text." }
    if ($Code -ge 0xD800 -and $Code -le 0xDBFF) {
      if ($Index + 1 -ge $Text.Length) { throw "$Label contains an unpaired high surrogate." }
      $Low = [int][char]$Text[$Index + 1]
      if ($Low -lt 0xDC00 -or $Low -gt 0xDFFF) { throw "$Label contains an unpaired high surrogate." }
      $Index++
    } elseif ($Code -ge 0xDC00 -and $Code -le 0xDFFF) {
      throw "$Label contains an unpaired low surrogate."
    }
  }
}
function Assert-JsonUnicodeScalars {
  param([object]$Value, [string]$Label)
  if ($null -eq $Value) { return }
  if ($Value -is [string]) { Assert-SafeUnicodeScalarText $Value $Label; return }
  if ($Value -is [Collections.IDictionary]) {
    foreach ($Key in $Value.Keys) {
      Assert-SafeUnicodeScalarText ([string]$Key) "$Label key"
      Assert-JsonUnicodeScalars $Value[$Key] "$Label.$Key"
    }
    return
  }
  if ($Value -is [pscustomobject]) {
    foreach ($Property in $Value.PSObject.Properties) {
      Assert-SafeUnicodeScalarText ([string]$Property.Name) "$Label property"
      Assert-JsonUnicodeScalars $Property.Value "$Label.$($Property.Name)"
    }
    return
  }
  if ($Value -is [Collections.IEnumerable]) {
    foreach ($Item in $Value) { Assert-JsonUnicodeScalars $Item "$Label[]" }
  }
}
function ConvertFrom-StrictJsonBytes {
  param([byte[]]$Bytes, [string]$Label)
  $Offset = if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) { 3 } else { 0 }
  $Text = $Utf8Strict.GetString($Bytes, $Offset, $Bytes.Length - $Offset)
  Assert-SafeUnicodeScalarText $Text $Label
  try { $Value = $Text | ConvertFrom-Json } catch { throw "Invalid JSON in ${Label}: $($_.Exception.Message)" }
  Assert-JsonUnicodeScalars $Value $Label
  return $Value
}
function Test-ManagedAudnCodeHandler {
  param([object]$Handler)
  if ($null -eq $Handler -or $Handler -isnot [pscustomobject] -or [string]$Handler.type -ne 'command') {
    return $false
  }
  $CommandProperty = $Handler.PSObject.Properties['command']
  if ($null -ne $CommandProperty -and $CommandProperty.Value -is [string] -and
      [string]$CommandProperty.Value -match $ManagedCommandPattern) {
    return $true
  }
  $ArgsProperty = $Handler.PSObject.Properties['args']
  if ($null -eq $ArgsProperty -or $ArgsProperty.Value -isnot [array]) { return $false }
  $Args = @($ArgsProperty.Value | ForEach-Object { [string]$_ })
  $FileIndexes = @(for ($Index = 0; $Index -lt $Args.Count; $Index++) { if ($Args[$Index] -ieq '-File') { $Index } })
  $HomeIndexes = @(for ($Index = 0; $Index -lt $Args.Count; $Index++) { if ($Args[$Index] -ieq '-AudnCodeHome') { $Index } })
  $OriginIndexes = @(for ($Index = 0; $Index -lt $Args.Count; $Index++) { if ($Args[$Index] -ieq '-Origin') { $Index } })
  $EventIndexes = @(for ($Index = 0; $Index -lt $Args.Count; $Index++) { if ($Args[$Index] -ieq '-AudnCodeExpectedEvent') { $Index } })
  if ($FileIndexes.Count -ne 1 -or $FileIndexes[0] + 1 -ge $Args.Count -or
      $HomeIndexes.Count -ne 1 -or $HomeIndexes[0] + 1 -ge $Args.Count -or
      $OriginIndexes.Count -ne 1 -or $OriginIndexes[0] + 1 -ge $Args.Count -or
      $EventIndexes.Count -gt 1 -or ($EventIndexes.Count -eq 1 -and $EventIndexes[0] + 1 -ge $Args.Count) -or
      @($Args | Where-Object { $_ -ieq '-AudnCodeHook' }).Count -ne 1 -or
      @($Args | Where-Object { $_ -ieq '-ReadStdin' }).Count -ne 1 -or
      -not [string]::Equals($Args[$OriginIndexes[0] + 1], 'AudnCode', [StringComparison]::OrdinalIgnoreCase)) {
    return $false
  }
  try {
    if (-not [string]::Equals([IO.Path]::GetFullPath($Args[$FileIndexes[0] + 1]), $ManagedScript, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([IO.Path]::GetFullPath($Args[$HomeIndexes[0] + 1]), $AudnHome, [StringComparison]::OrdinalIgnoreCase)) {
      return $false
    }
  } catch { return $false }
  return $EventIndexes.Count -eq 0 -or $Args[$EventIndexes[0] + 1] -in $AllowedAudnEvents
}

if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
  $OriginalBytes = [IO.File]::ReadAllBytes($SettingsPath)
  $OriginalAcl = try { [IO.File]::GetAccessControl($SettingsPath) } catch {
    Get-Acl -LiteralPath $SettingsPath -ErrorAction Stop
  }
  $AclSections = [Security.AccessControl.AccessControlSections]::Access -bor
    [Security.AccessControl.AccessControlSections]::Owner -bor
    [Security.AccessControl.AccessControlSections]::Group
  $OriginalSddl = $OriginalAcl.GetSecurityDescriptorSddlForm($AclSections)
  $NormalizeAclSddl = { param([string]$Sddl) [regex]::Replace($Sddl, 'D:(P?)(AR)?AI(?=\()', 'D:$1$2') }
  $Document = ConvertFrom-StrictJsonBytes $OriginalBytes $SettingsPath
  if ($null -eq $Document -or $Document -isnot [pscustomobject]) { throw "$SettingsPath must contain a JSON object." }
  $HooksProperty = $Document.PSObject.Properties['hooks']
  $Changed = $false
  if ($null -ne $HooksProperty -and
      $null -ne $HooksProperty.Value -and
      $HooksProperty.Value -is [System.Management.Automation.PSCustomObject]) {
    $Events = $HooksProperty.Value
    foreach ($EventProperty in @($Events.PSObject.Properties)) {
      if ($EventProperty.Value -isnot [array]) { continue }
      $KeptGroups = New-Object 'System.Collections.Generic.List[object]'
      $RemovedFromEvent = $false
      foreach ($Group in @($EventProperty.Value)) {
        if ($null -eq $Group -or $Group -isnot [System.Management.Automation.PSCustomObject]) {
          $KeptGroups.Add($Group)
          continue
        }
        $HandlersProperty = $Group.PSObject.Properties['hooks']
        if ($null -eq $HandlersProperty -or $HandlersProperty.Value -isnot [array]) {
          $KeptGroups.Add($Group)
          continue
        }
        $OriginalHandlers = @($HandlersProperty.Value)
        $KeptHandlers = @($OriginalHandlers | Where-Object {
          -not (Test-ManagedAudnCodeHandler $_)
        })
        if ($KeptHandlers.Count -ne $OriginalHandlers.Count) { $RemovedFromEvent = $true }
        if ($KeptHandlers.Count -gt 0) {
          $HandlersProperty.Value = @($KeptHandlers)
          $KeptGroups.Add($Group)
        } elseif ($OriginalHandlers.Count -eq 0) {
          $KeptGroups.Add($Group)
        }
      }
      if ($RemovedFromEvent) {
        $Changed = $true
        if ($KeptGroups.Count -gt 0) {
          $EventProperty.Value = @($KeptGroups.ToArray())
        } else {
          $Events.PSObject.Properties.Remove($EventProperty.Name)
        }
      }
    }
  }
  if ($Changed) {
    $PrivateBackup = "$SettingsPath.pre-ntfy-uninstall-$(Get-Date -Format yyyyMMdd-HHmmss)-$([Guid]::NewGuid().ToString('N'))"
    $TempPath = Join-Path (Split-Path -Parent $SettingsPath) ('.' + (Split-Path -Leaf $SettingsPath) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $Rendered = ($Document | ConvertTo-Json -Depth 100) + [Environment]::NewLine
    Assert-SafeUnicodeScalarText $Rendered "rendered AudnCode settings"
    [void]$Utf8Strict.GetByteCount($Rendered)
    try {
      # The empty temp can briefly inherit its directory ACL, but no settings
      # content is written until it has the exact ACL of settings.json.
      $Empty = [IO.File]::Open($TempPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
      $Empty.Dispose()
      $TempAcl = try { [IO.File]::GetAccessControl($TempPath) } catch {
        Get-Acl -LiteralPath $TempPath -ErrorAction Stop
      }
      $InitialTempSddl = $TempAcl.GetSecurityDescriptorSddlForm($AclSections)
      if (-not [string]::Equals($InitialTempSddl, $OriginalSddl, [StringComparison]::Ordinal)) {
        $TargetOwner = $OriginalAcl.GetOwner([Security.Principal.SecurityIdentifier])
        $TargetGroup = $OriginalAcl.GetGroup([Security.Principal.SecurityIdentifier])
        if ($TempAcl.GetOwner([Security.Principal.SecurityIdentifier]) -ne $TargetOwner) { $TempAcl.SetOwner($TargetOwner) }
        if ($TempAcl.GetGroup([Security.Principal.SecurityIdentifier]) -ne $TargetGroup) { $TempAcl.SetGroup($TargetGroup) }
        if ($TempAcl.AreAccessRulesProtected -ne $OriginalAcl.AreAccessRulesProtected) {
          $TempAcl.SetAccessRuleProtection($OriginalAcl.AreAccessRulesProtected, $false)
        }
        foreach ($Rule in @($TempAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
          [void]$TempAcl.RemoveAccessRuleSpecific($Rule)
        }
        foreach ($Rule in @($OriginalAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
          [void]$TempAcl.AddAccessRule($Rule)
        }
        try { [IO.File]::SetAccessControl($TempPath, $TempAcl) } catch {
          Set-Acl -LiteralPath $TempPath -AclObject $TempAcl -ErrorAction Stop
        }
      }
      $PreparedAcl = try { [IO.File]::GetAccessControl($TempPath) } catch {
        Get-Acl -LiteralPath $TempPath -ErrorAction Stop
      }
      $PreparedSddl = $PreparedAcl.GetSecurityDescriptorSddlForm($AclSections)
      if (-not [string]::Equals((& $NormalizeAclSddl $PreparedSddl), (& $NormalizeAclSddl $OriginalSddl), [StringComparison]::Ordinal)) {
        throw "temporary AudnCode settings ACL is not equivalent to the destination"
      }
      [IO.File]::WriteAllText($TempPath, $Rendered, $Utf8Strict)
      $CurrentBytes = [IO.File]::ReadAllBytes($SettingsPath)
      if (-not [Linq.Enumerable]::SequenceEqual([byte[]]$OriginalBytes, [byte[]]$CurrentBytes)) {
        throw "AudnCode settings changed during uninstall; no changes were written: $SettingsPath"
      }
      [IO.File]::Replace($TempPath, $SettingsPath, $PrivateBackup, $false)
      $InstalledAcl = try { [IO.File]::GetAccessControl($SettingsPath) } catch {
        Get-Acl -LiteralPath $SettingsPath -ErrorAction Stop
      }
      $InstalledSddl = $InstalledAcl.GetSecurityDescriptorSddlForm($AclSections)
      if (-not [string]::Equals((& $NormalizeAclSddl $InstalledSddl), (& $NormalizeAclSddl $OriginalSddl), [StringComparison]::Ordinal)) {
        $FailedPath = "$SettingsPath.failed-ntfy-uninstall-$([Guid]::NewGuid().ToString('N'))"
        try { [IO.File]::Replace($PrivateBackup, $SettingsPath, $FailedPath, $false) } finally {
          if (Test-Path -LiteralPath $FailedPath) { Remove-Item -LiteralPath $FailedPath -Force }
        }
        throw "AudnCode settings ACL changed during uninstall; the original file was restored"
      }
    } finally {
      if (Test-Path -LiteralPath $TempPath) { Remove-Item -LiteralPath $TempPath -Force }
    }
  }
}
```

Verify locally with `Select-String -LiteralPath $SettingsPath -Pattern ([regex]::Escape($ManagedScript))`. Then, in the same PowerShell session (the block reuses the strict helpers above), remove only this project's observation marker after validating ownership. Recognized historical hook-shape versions remain removable; an unknown, malformed, or concurrently changed marker is preserved for review:

```powershell
$MarkerPath = Join-Path $AudnHome '.codex-ntfy-hooks.json'
if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) {
  $MarkerBytes = [IO.File]::ReadAllBytes($MarkerPath)
  $Marker = ConvertFrom-StrictJsonBytes $MarkerBytes $MarkerPath
  $ShapeProperty = $Marker.PSObject.Properties['hook_shape_version']
  $KnownHistoricalShape = $null -eq $ShapeProperty
  if ($null -ne $ShapeProperty) {
    try { $KnownHistoricalShape = [int]$ShapeProperty.Value -ge 1 -and [int]$ShapeProperty.Value -le 9 } catch { $KnownHistoricalShape = $false }
  }
  $OwnedMarker = [string]$Marker.kind -eq 'codex-ntfy-audncode-hooks' -and
    [int]$Marker.schema -eq 1 -and
    $KnownHistoricalShape -and
    [string]$Marker.generation -match '^[a-f0-9]{32}$' -and
    [string]::Equals([IO.Path]::GetFullPath([string]$Marker.audncode_home), $AudnHome, [StringComparison]::OrdinalIgnoreCase)
  if (-not $OwnedMarker) { throw "Unrecognized AudnCode marker; preserved for review: $MarkerPath" }
  $CurrentMarkerBytes = [IO.File]::ReadAllBytes($MarkerPath)
  if (-not [Linq.Enumerable]::SequenceEqual([byte[]]$MarkerBytes, [byte[]]$CurrentMarkerBytes)) {
    throw "AudnCode marker changed during uninstall; preserved: $MarkerPath"
  }
  Copy-Item -LiteralPath $MarkerPath -Destination "$MarkerPath.pre-uninstall-$([Guid]::NewGuid().ToString('N'))"
  $CurrentMarkerBytes = [IO.File]::ReadAllBytes($MarkerPath)
  if (-not [Linq.Enumerable]::SequenceEqual([byte[]]$MarkerBytes, [byte[]]$CurrentMarkerBytes)) {
    throw "AudnCode marker changed after backup; preserved: $MarkerPath"
  }
  Remove-Item -LiteralPath $MarkerPath -Force
}
```

Reopen AudnCode after removing the hooks and marker. Do not delete all of `settings.json` or the entire `.openclaude` directory.

The installer also sets `messageIdleNotifThresholdMs` in AudnCode's active global configuration. Leaving that ordinary timing preference in place is safe and is the recommended manual-uninstall behavior. Automatic installation-failure rollback uses AudnCode's configuration lease and field-level compare-and-swap; a later hand-written whole-file or field rollback cannot reproduce that transaction safely and could erase newer authentication, session, or preference data. Do not restore `audncode-global.json` over the active file. If the threshold must change, close AudnCode and set the preference through AudnCode's supported configuration path.

### 3. Remove managed files

First assert that the path is the standard Codex home, then remove only named project files:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
$Expected = [IO.Path]::GetFullPath("$HOME\.codex")
if ($CodexHome -ne $Expected) { throw "Unexpected Codex home: $CodexHome" }

@(
  'notify-ntfy.ps1',
  'watch-codex-ntfy.ps1',
  'watch-codex-ntfy-hidden.vbs',
  'install-remote-windows-target.ps1'
) | ForEach-Object {
  Remove-Item -LiteralPath (Join-Path $CodexHome $_) -Force -ErrorAction SilentlyContinue
}
```

Keep `ntfy-config.json`, `ntfy-state`, and `ntfy-backups` until credentials, pending events, and rollback needs have been reviewed. To erase them deliberately:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
$Expected = [IO.Path]::GetFullPath("$HOME\.codex")
if ($CodexHome -ne $Expected) { throw "Unexpected Codex home: $CodexHome" }

Remove-Item -LiteralPath (Join-Path $CodexHome 'ntfy-config.json') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $CodexHome 'ntfy-state') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $CodexHome 'ntfy-backups') -Recurse -Force -ErrorAction SilentlyContinue
```

### 4. Reload affected coding-agent surfaces

Reload the Codex app/CLI and every local VS Code window. If Claude handlers were removed, also reload Claude Desktop's Code tab, standalone Claude Code CLI processes, and editor windows using Claude Code. AudnCode normally hot-reloads its files for the next turn; end any already-running turn before deleting the script. A process that already read a hook file can continue invoking a deleted script until it reloads the configuration or restarts.

## Roll back Windows to a selected backup

Local Windows backups can include the managed scripts, private config, `config.toml`, `hooks.json`, pre-install Claude/AudnCode settings, an AudnCode global reference snapshot, and a prior `CodexNtfyWatcher.xml`. The block below restores Codex-managed files and optionally Claude settings. It deliberately does **not** restore either AudnCode file wholesale: use the strict selective handler/marker procedure above and leave the ordinary threshold preference in place. Whole-file AudnCode restoration can erase authentication, sessions, or preferences written after the snapshot. If installation used a custom `-ClaudeHome`, assign that same absolute directory below.

Stop the current worker as above. Assign a timestamp explicitly, validate that it is directly under the backup root, and restore the files it contains:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
$ClaudeHome = [IO.Path]::GetFullPath((Join-Path $HOME '.claude')) # replace with the installed -ClaudeHome when customized
$BackupRoot = [IO.Path]::GetFullPath((Join-Path $CodexHome 'ntfy-backups'))
$Backup = [IO.Path]::GetFullPath((Join-Path $BackupRoot 'YYYYMMDD-HHMMSS-fff')) # choose explicitly
if ([IO.Path]::GetDirectoryName($Backup) -ne $BackupRoot -or -not (Test-Path -LiteralPath $Backup -PathType Container)) {
  throw "Invalid backup path: $Backup"
}

$Managed = @(
  'notify-ntfy.ps1',
  'watch-codex-ntfy.ps1',
  'watch-codex-ntfy-hidden.vbs',
  'hooks.json',
  'ntfy-config.json'
)
foreach ($Name in $Managed) {
  $Saved = Join-Path $Backup $Name
  $Target = Join-Path $CodexHome $Name
  if (Test-Path -LiteralPath $Saved -PathType Leaf) {
    Copy-Item -LiteralPath $Saved -Destination $Target -Force
  } else {
    Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
  }
}

$SavedToml = Join-Path $Backup 'config.toml'
if (Test-Path -LiteralPath $SavedToml -PathType Leaf) {
  Copy-Item -LiteralPath $SavedToml -Destination (Join-Path $CodexHome 'config.toml') -Force
} else {
  Write-Warning 'No config.toml in this backup; remove the managed notify line manually.'
}

$SavedClaudeSettings = Join-Path $Backup 'claude-settings.json'
if (Test-Path -LiteralPath $SavedClaudeSettings -PathType Leaf) {
  New-Item -ItemType Directory -Path $ClaudeHome -Force | Out-Null
  $ClaudeSettings = Join-Path $ClaudeHome 'settings.json'
  $ClaudeStage = "$ClaudeSettings.rollback"
  Copy-Item -LiteralPath $SavedClaudeSettings -Destination $ClaudeStage -Force
  Move-Item -LiteralPath $ClaudeStage -Destination $ClaudeSettings -Force
} else {
  Write-Warning 'No pre-install Claude settings snapshot in this backup; use the selective Claude handler cleanup above if needed.'
}

Unregister-ScheduledTask -TaskName CodexNtfyWatcher -Confirm:$false -ErrorAction SilentlyContinue
$SavedTask = Join-Path $Backup 'CodexNtfyWatcher.xml'
if (Test-Path -LiteralPath $SavedTask -PathType Leaf) {
  $TaskXml = (Get-Content -LiteralPath $SavedTask -Raw) -replace '^\s*<\?xml[^?]*\?>', ''
  Register-ScheduledTask -TaskName CodexNtfyWatcher -Xml $TaskXml -Force | Out-Null
  Start-ScheduledTask -TaskName CodexNtfyWatcher
}
```

This rollback restores or removes Codex `hooks.json` as captured and restores Claude settings only when the selected snapshot is intentionally safe to apply. It never restores AudnCode settings or global configuration: use the strict selective cleanup above, and remove a newly created provider file only after proving it contains no unrelated data. An absent provider snapshot never authorizes deleting a current file.

Runtime state is not part of the rollback snapshot. Before running substantially older notifier code, move `ntfy-state` to a private, timestamped sibling instead of letting an incompatible version process it. Version 2.6.0 keeps queue and pending-candidate records at schema 1, uses schema 2 for AudnCode lifecycle guards, and retains the `pending/` and `watch/` state introduced in 2.4.0; compatibility with an arbitrary older build is not guaranteed.

Remote Windows backups use the same scheduled-task XML snapshot. The installer refuses to overwrite a task named `CodexNtfyWatcher` unless its action belongs to this project, and an installation failure restores the prior definition and running state automatically.

For a Remote Windows rollback, run the preceding block in an interactive PowerShell session on the target itself. In addition, restore the target installer that is present in remote snapshots:

```powershell
$SavedTarget = Join-Path $Backup 'install-remote-windows-target.ps1'
$InstalledTarget = Join-Path $CodexHome 'install-remote-windows-target.ps1'
if (Test-Path -LiteralPath $SavedTarget -PathType Leaf) {
  Copy-Item -LiteralPath $SavedTarget -Destination $InstalledTarget -Force
} else {
  Remove-Item -LiteralPath $InstalledTarget -Force -ErrorAction SilentlyContinue
}
```

Do not rerun the target installer during a file-for-file rollback; the preceding task XML restoration already restores the selected worker definition.

## Remove a WSL bridge installation

Repeat this section for every distribution passed to `install.ps1`.

From Windows, enter the target distribution or run the commands in its shell:

```powershell
wsl.exe -d Ubuntu -- sh
```

Inside WSL:

1. privately back up `~/.codex/config.toml` and `~/.codex/hooks.json`;
2. remove only the root-level line containing `notify-ntfy-wsl.sh`;
3. remove only structured command handlers carrying `--hook-event` and the exact resolved notifier path from `hooks.json`;
4. verify that no unrelated `notify` line or hook changed;
5. stop any native fallback worker;
6. remove only the WSL-managed files.

Use this Python cleanup for `hooks.json`. It recognizes only a `type: command` handler with one of the installed parsed argument shapes: the exact resolved `notify-ntfy-wsl.sh` from this `CODEX_HOME` immediately followed by `--hook-event`, or a Python interpreter immediately followed by the exact resolved `notify-ntfy.py` and `--hook-event`. It preserves unrelated handlers, groups, event names, and top-level metadata, compares the original bytes immediately before a same-directory atomic replacement, and does not edit the Codex trust store:

```sh
python3 - <<'PY'
import json
import os
import re
import shlex
import stat
import uuid
from pathlib import Path

home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
path = home / "hooks.json"
try:
    original_stat = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISREG(original_stat.st_mode) or path.is_symlink():
    raise SystemExit(f"hooks.json is not a regular non-symlink file: {path}")

original_bytes = path.read_bytes()
document = json.loads(original_bytes.decode("utf-8-sig"))
if not isinstance(document, dict):
    raise SystemExit("hooks.json must contain a JSON object")
events = document.get("hooks")
if not isinstance(events, dict):
    raise SystemExit("hooks.json has no hooks object; inspect it manually")

wsl_script = (home / "notify-ntfy-wsl.sh").resolve(strict=False)
python_script = (home / "notify-ntfy.py").resolve(strict=False)

def resolved_absolute(argument):
    candidate = Path(argument).expanduser()
    return candidate.resolve(strict=False) if candidate.is_absolute() else None

def managed(handler):
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    for field in ("command", "commandWindows", "command_windows"):
        command = handler.get(field)
        if not isinstance(command, str):
            continue
        try:
            arguments = shlex.split(command, posix=True)
        except ValueError:
            continue
        if arguments.count("--hook-event") != 1:
            continue
        marker_index = arguments.index("--hook-event")
        if marker_index == 1 and resolved_absolute(arguments[0]) == wsl_script:
            return True
        if (
            marker_index == 2
            and re.fullmatch(r"python(?:3(?:\.\d+)*)?", Path(arguments[0]).name)
            and resolved_absolute(arguments[1]) == python_script
        ):
            return True
    return False

changed = False
for event_name, groups in list(events.items()):
    if not isinstance(groups, list):
        continue
    kept_groups = []
    removed_from_event = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        original_handlers = group["hooks"]
        remaining = [handler for handler in original_handlers if not managed(handler)]
        if len(remaining) != len(original_handlers):
            changed = removed_from_event = True
        if remaining:
            updated = dict(group)
            updated["hooks"] = remaining
            kept_groups.append(updated)
        elif not original_handlers:
            kept_groups.append(group)
    if removed_from_event:
        if kept_groups:
            events[event_name] = kept_groups
        else:
            del events[event_name]

if changed:
    rendered = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    suffix = uuid.uuid4().hex
    backup = path.with_name(path.name + ".pre-ntfy-uninstall-" + suffix)
    temporary = path.with_name("." + path.name + "." + suffix + ".tmp")
    backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(backup_fd, "wb") as handle:
        handle.write(original_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IMODE(original_stat.st_mode),
        )
        with os.fdopen(temporary_fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        current_stat = path.lstat()
        if (
            not stat.S_ISREG(current_stat.st_mode)
            or path.is_symlink()
            or (current_stat.st_dev, current_stat.st_ino) != (original_stat.st_dev, original_stat.st_ino)
            or path.read_bytes() != original_bytes
        ):
            raise RuntimeError(f"hooks.json changed during uninstall; no changes were written: {path}")
        os.replace(temporary, path)
        path.chmod(stat.S_IMODE(original_stat.st_mode))
    finally:
        temporary.unlink(missing_ok=True)
PY
```

Stop only a native fallback worker whose argument vector has the installed shape: a Python interpreter, then the exact resolved script from this `CODEX_HOME`, then an exact `--worker` token. The helper snapshots the immutable process start-time field from `/proc/<pid>/stat` plus the NUL-delimited command line, then reads both again before sending `SIGTERM`; a reused or changed PID is preserved:

```sh
python3 - <<'PY'
import os
import re
import signal
from pathlib import Path

home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
managed_script = (home / "notify-ntfy.py").resolve(strict=False)

def identity(process):
    try:
        raw_stat = process.joinpath("stat").read_bytes()
        _, separator, trailing_fields = raw_stat.rpartition(b")")
        fields = trailing_fields.strip().split()
        if not separator or len(fields) < 20:
            return None
        start_time = fields[19]  # field 22; fields here begin at proc-stat field 3
        return (start_time, process.joinpath("cmdline").read_bytes())
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None

for process in Path("/proc").iterdir():
    if not process.name.isdigit() or int(process.name) == os.getpid():
        continue
    snapshot = identity(process)
    if snapshot is None:
        continue
    arguments = [os.fsdecode(value) for value in snapshot[1].split(b"\0") if value]
    owned = (
        len(arguments) >= 3
        and re.fullmatch(r"python(?:3(?:\.\d+)*)?", Path(arguments[0]).name)
        and Path(arguments[1]).is_absolute()
        and Path(arguments[1]).resolve(strict=False) == managed_script
        and "--worker" in arguments[2:]
    )
    if not owned:
        continue
    if identity(process) != snapshot:
        raise SystemExit(f"process identity changed before stop; preserved PID {process.name}")
    os.kill(int(process.name), signal.SIGTERM)
PY

grep -nE '^[[:space:]]*notify[[:space:]]*=' "$HOME/.codex/config.toml"
rm -f -- "$HOME/.codex/notify-ntfy-wsl.sh" "$HOME/.codex/notify-ntfy.py"
```

The Windows installer keeps up to ten WSL snapshots in `~/.codex/ntfy-backups` inside each distribution. A selected snapshot can restore `config.toml`, `hooks.json`, the bridge scripts, and private config. If later Codex settings/hooks must be retained, use selective removal instead of replacing the whole file.

WSL receives a copy of `ntfy-config.json`. Delete it only after confirming that no other WSL setup uses it. If native fallback was ever used, `~/.codex/ntfy-state` can contain pending events and sensitive data. The normal Windows-bridged queue is instead in the Windows state directory.

The Windows private config can also contain a `watch_roots` entry for this distribution. Remove only the object whose `path` points at the uninstalled `\\wsl.localhost\<distro>\...` root; keep entries for other distributions and custom roots. Restart `CodexNtfyWatcher` afterwards.

## Remove a native or Remote SSH Linux installation

Run the following on the actual Linux target as the installed user. For a Remote SSH host, connect first with the same alias/account used during installation.

### 1. Stop and remove the user service

```sh
systemctl --user disable --now codex-ntfy.service 2>/dev/null || true
rm -f -- "$HOME/.config/systemd/user/codex-ntfy.service"
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed codex-ntfy.service 2>/dev/null || true
```

An on-demand worker normally exits when both `pending/` and `outbox/` are empty. A genuinely busy task can keep a pending record alive; unknown evidence is instead suppressed after `idle_probe_grace_seconds` in strict mode. To stop one deliberately, run the exact `/proc` identity helper from the WSL section above with this host's intended `CODEX_HOME`; do not use a name-only `pkill`, which can match another custom home.

### 2. Restore or remove the managed hooks

If no Codex settings changed after installation, restore `config.toml` from the explicitly selected backup. A full file-for-file rollback of `hooks.json` is covered in the Linux rollback section below; for ordinary uninstall, prefer selective removal so unrelated later hooks survive.

```sh
codex_home=${CODEX_HOME:-"$HOME/.codex"}
backup="$codex_home/ntfy-backups/YYYYMMDD-HHMMSS-NNNNNNNNN" # choose explicitly
test -f "$backup/config.toml"
cp -p -- "$backup/config.toml" "$codex_home/config.toml"
chmod 600 "$codex_home/config.toml"
```

Privately back up the current files, remove only the root-level `notify` line containing `notify-ntfy.py`, and run the selective Python `hooks.json` cleanup from the WSL section above. That cleanup works unchanged on native and Remote SSH Linux. Restore a prior legacy notification only from a trusted pre-install backup. Verify with:

```sh
grep -nE '^[[:space:]]*notify[[:space:]]*=' "${CODEX_HOME:-$HOME/.codex}/config.toml"
grep -n 'notify-ntfy' "${CODEX_HOME:-$HOME/.codex}/hooks.json" 2>/dev/null || true
```

Do not remove the entire `hooks.json` file and do not edit the Codex trust store.

### 3. Remove managed files and optional private data

```sh
codex_home=${CODEX_HOME:-"$HOME/.codex"}
case "$codex_home" in
  "$HOME/.codex"|/*/.codex) ;;
  *) echo "refusing unexpected CODEX_HOME: $codex_home" >&2; exit 2 ;;
esac

rm -f -- "$codex_home/notify-ntfy.py" "$codex_home/install-remote-linux-target.py"
```

After reviewing pending events and rollback needs, erase private data only if intended:

```sh
codex_home=${CODEX_HOME:-"$HOME/.codex"}
case "$codex_home" in
  "$HOME/.codex"|/*/.codex) ;;
  *) echo "refusing unexpected CODEX_HOME: $codex_home" >&2; exit 2 ;;
esac

rm -f -- "$codex_home/ntfy-config.json"
rm -rf -- "$codex_home/ntfy-state" "$codex_home/ntfy-backups"
```

The installer also snapshots an existing project-owned `~/.config/systemd/user/codex-ntfy.service`. It refuses to overwrite an unrelated unit with the same name. On installation failure it restores the previous file and its enabled/active state.

## Roll back Linux to a selected backup

Stop the service and any on-demand worker first. Then restore only from a timestamp directory verified to be directly beneath `ntfy-backups`:

```sh
python3 - <<'PY'
import os
import shutil
from pathlib import Path

home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
root = (home / "ntfy-backups").resolve()
backup = (root / "YYYYMMDD-HHMMSS-NNNNNNNNN").resolve()  # choose explicitly
if backup.parent != root or not backup.is_dir():
    raise SystemExit(f"invalid backup path: {backup}")

managed = ("notify-ntfy.py", "install-remote-linux-target.py", "hooks.json", "ntfy-config.json")
for name in managed:
    source = backup / name
    target = home / name
    if source.is_file():
        shutil.copy2(source, target)
    else:
        target.unlink(missing_ok=True)

source = backup / "config.toml"
if source.is_file():
    shutil.copy2(source, home / "config.toml")
else:
    print("No config.toml in backup; remove the managed notify line manually.")

unit = Path.home() / ".config" / "systemd" / "user" / "codex-ntfy.service"
saved_unit = backup / "codex-ntfy.service"
if saved_unit.is_file():
    unit.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(saved_unit, unit)
else:
    unit.unlink(missing_ok=True)
PY
```

Replace the timestamp placeholder before running the script. This full rollback restores or removes `hooks.json` exactly as captured, so do not use it when later unrelated hook changes must survive; use selective uninstall instead. Run `systemctl --user daemon-reload`, then explicitly enable/start the restored unit only if that matches the selected snapshot's intended state. State is not included in backups; isolate it before running a version whose queue schema is unknown.

## Remote hosts

Uninstalling the local machine does not alter Remote SSH hosts. Perform the Windows or Linux procedure on every target. Likewise, deleting a remote installation does not remove the local copy of the credential used to deploy it.

For Remote Windows, start an interactive PowerShell session on the host and follow the Windows section. For Remote Linux:

```sh
ssh my-linux-host
```

Confirm hostname and username before deleting anything:

```sh
hostname
id
```

## Server and client cleanup

Local removal does not delete ntfy messages already accepted by the server or cached by subscribers. After retiring a host:

- revoke its publish token;
- rotate a topic that may have leaked;
- remove subscriptions and notification history from clients if required;
- use the selected ntfy server's documented retention/deletion controls;
- review and delete obsolete private backups on every host.

Keep a backup only as long as its embedded credentials and content are still intentionally retained.
