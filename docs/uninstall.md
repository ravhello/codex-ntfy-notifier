# Uninstall and rollback

These procedures apply to version 2.4.2. They are intentionally explicit because `~/.codex` also belongs to Codex; never remove that whole directory.

An **uninstall** removes only this project’s managed `notify` command, `notify-ntfy` hook handlers, scripts, and worker while preserving unrelated Codex settings and hooks. A **rollback** restores the timestamped snapshot taken immediately before a particular installation or upgrade. Decide which outcome is wanted before deleting anything.

## Before changing files

1. Close or reload Codex app/CLI processes and VS Code windows after the procedure so they do not retain old hook configuration.
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
Stop-ScheduledTask -TaskName CodexNtfyWatcher -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName CodexNtfyWatcher -Confirm:$false -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -in @('powershell.exe', 'pwsh.exe', 'wscript.exe') -and
    $_.CommandLine -match '(?i)\.codex\\(?:watch-codex-ntfy(?:-hidden)?\.(?:ps1|vbs)|notify-ntfy\.ps1.*-(?:Worker|Continuous))'
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Review the process list before using `Stop-Process` in a customized installation.

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

Remove the modern handler selectively. This script scans every hook event so it also cleans up a managed handler left by an older preview, but it retains unrelated handlers, groups, events, and top-level metadata:

```powershell
$HooksPath = Join-Path $HOME '.codex\hooks.json'
if (Test-Path -LiteralPath $HooksPath -PathType Leaf) {
  $Document = Get-Content -LiteralPath $HooksPath -Raw | ConvertFrom-Json
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
            foreach ($Field in @('command', 'commandWindows', 'command_windows')) {
              $Property = $_.PSObject.Properties[$Field]
              if ($null -ne $Property -and
                  $Property.Value -is [string] -and
                  $Property.Value -match '(?i)notify-ntfy') {
                $Managed = $true
              }
            }
          }
          -not $Managed
        })

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
    $PrivateBackup = "$HooksPath.pre-ntfy-uninstall-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item -LiteralPath $HooksPath -Destination $PrivateBackup
    $Utf8NoBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($HooksPath, (($Document | ConvertTo-Json -Depth 32) + [Environment]::NewLine), $Utf8NoBom)
  }
}
```

If `$Changed` was true, inspect the diff against `$PrivateBackup` locally. Then verify that no managed command remains:

```powershell
Select-String -Path "$HOME\.codex\hooks.json" -Pattern 'notify-ntfy' -ErrorAction SilentlyContinue
```

Do not delete all of `hooks.json` and do not edit Codex’s hook trust store. A retained approval does not execute anything without a registered hook command; removing trust entries is outside this uninstall.

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

### 4. Reload Codex

Reload the Codex app/CLI and every local VS Code window. A process that already read `config.toml` or `hooks.json` can continue invoking a deleted script until it restarts.

## Roll back Windows to a selected backup

Local Windows backups can include the managed scripts, private config, `config.toml`, `hooks.json`, and an exported `CodexNtfyWatcher.xml` when that task existed before the installer run.

Stop the current worker as above. Assign a timestamp explicitly, validate that it is directly under the backup root, and restore the files it contains:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
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

Unregister-ScheduledTask -TaskName CodexNtfyWatcher -Confirm:$false -ErrorAction SilentlyContinue
$SavedTask = Join-Path $Backup 'CodexNtfyWatcher.xml'
if (Test-Path -LiteralPath $SavedTask -PathType Leaf) {
  $TaskXml = (Get-Content -LiteralPath $SavedTask -Raw) -replace '^\s*<\?xml[^?]*\?>', ''
  Register-ScheduledTask -TaskName CodexNtfyWatcher -Xml $TaskXml -Force | Out-Null
  Start-ScheduledTask -TaskName CodexNtfyWatcher
}
```

This full rollback restores or removes `hooks.json` exactly as captured. Do not use it when later unrelated hooks must survive; use the selective handler cleanup instead.

Runtime state is not part of the rollback snapshot. Before running substantially older notifier code, move `ntfy-state` to a private, timestamped sibling instead of letting an incompatible version process it. Version 2.4.2 uses record schema 1 and retains the `pending/` and `watch/` state introduced in 2.4.0; compatibility with an arbitrary older build is not guaranteed.

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
3. remove only command handlers containing `notify-ntfy` from `hooks.json`;
4. verify that no unrelated `notify` line or hook changed;
5. stop any native fallback worker;
6. remove only the WSL-managed files.

Use this Python cleanup for `hooks.json`. It preserves unrelated handlers, groups, event names, and top-level metadata; it does not edit the Codex trust store:

```sh
python3 - <<'PY'
import json
import os
import shutil
import time
from pathlib import Path

path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"
if not path.is_file():
    raise SystemExit(0)

document = json.loads(path.read_text(encoding="utf-8"))
events = document.get("hooks")
if not isinstance(events, dict):
    raise SystemExit("hooks.json has no hooks object; inspect it manually")

def managed(handler):
    if not isinstance(handler, dict):
        return False
    return any(
        isinstance(handler.get(field), str)
        and "notify-ntfy" in handler[field].lower()
        for field in ("command", "commandWindows", "command_windows")
    )

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
        original = group["hooks"]
        remaining = [handler for handler in original if not managed(handler)]
        if len(remaining) != len(original):
            changed = removed_from_event = True
        if remaining:
            updated = dict(group)
            updated["hooks"] = remaining
            kept_groups.append(updated)
        elif not original:
            kept_groups.append(group)
    if removed_from_event:
        if kept_groups:
            events[event_name] = kept_groups
        else:
            del events[event_name]

if changed:
    backup = path.with_name(path.name + ".pre-ntfy-uninstall-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    temporary = path.with_name(path.name + ".ntfy-uninstall.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(path.stat().st_mode & 0o777)
    os.replace(temporary, path)
PY
```

```sh
grep -nE '^[[:space:]]*notify[[:space:]]*=' "$HOME/.codex/config.toml"
grep -n 'notify-ntfy' "$HOME/.codex/hooks.json" 2>/dev/null || true
pkill -f '[n]otify-ntfy.py --worker' 2>/dev/null || true
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

An on-demand worker normally exits when both `pending/` and `outbox/` are empty. In strict mode, incomplete evidence can keep it alive. To stop one deliberately, first review matching processes, then terminate them:

```sh
pgrep -af 'notify-ntfy.py.*--worker' || true
pkill -f '[n]otify-ntfy.py.*--worker' 2>/dev/null || true
```

That pattern can match more than one custom Codex home for the same user; review before running it.

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
