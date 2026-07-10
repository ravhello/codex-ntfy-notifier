# Uninstall and rollback

These procedures apply to version 2.3.0. They are intentionally explicit because `~/.codex` also belongs to Codex; never remove that whole directory.

An **uninstall** removes the managed hook and worker while preserving unrelated Codex settings. A **rollback** restores the timestamped snapshot taken immediately before a particular installation or upgrade. Decide which outcome is wanted before deleting anything.

## Before changing files

1. Close or reload Codex/VS Code windows after the procedure so they do not retain the old hook configuration.
2. Run doctor and inspect `queued`. Wait for it to reach zero, or explicitly accept that pending notifications will be discarded.
3. Select the correct host and user. Each local, WSL, and Remote SSH environment can have a separate `~/.codex`.
4. Make a private copy of any config or state that may be needed for rollback. It can contain credentials and message content.
5. Do not print or upload `ntfy-config.json`, state, or backups.

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

### 2. Restore or remove the hook

If no Codex settings were changed after installation, restoring `config.toml` from the chosen backup is the most exact option:

```powershell
$CodexHome = [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
$Backup = Join-Path $CodexHome 'ntfy-backups\YYYYMMDD-HHMMSS-fff' # choose explicitly
Copy-Item -LiteralPath (Join-Path $Backup 'config.toml') `
  -Destination (Join-Path $CodexHome 'config.toml') -Force
```

Do not run that copy if the selected backup has no `config.toml` or if it would overwrite later Codex settings. In that case, privately back up the current file and remove only the single root-level line containing `notify-ntfy.ps1`. Leave every unrelated line and table unchanged.

Verify afterwards:

```powershell
Select-String -Path "$HOME\.codex\config.toml" -Pattern '^\s*notify\s*='
```

If a previous non-project hook should be restored, copy its exact root-level `notify = [...]` line from a trusted pre-install backup. Do not add a second root-level `notify` key.

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

Reload every local VS Code window. A process that already read the old `config.toml` can continue invoking a deleted script until it restarts.

## Roll back Windows to a selected backup

Local Windows backups can include the managed scripts, private config, `config.toml`, and an exported `CodexNtfyWatcher.xml` when that task existed before the installer run.

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

Runtime state is not part of the rollback snapshot. Before running substantially older notifier code, move `ntfy-state` to a private, timestamped sibling instead of letting an incompatible version process it. Version 2.3.0 uses queue schema 1, but compatibility with an arbitrary older private build is not guaranteed.

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

1. privately back up `~/.codex/config.toml`;
2. remove only the root-level line containing `notify-ntfy-wsl.sh`;
3. verify that no other `notify` line was accidentally changed;
4. stop any native fallback worker;
5. remove only the WSL-managed files.

```sh
grep -nE '^[[:space:]]*notify[[:space:]]*=' "$HOME/.codex/config.toml"
pkill -f '[n]otify-ntfy.py --worker' 2>/dev/null || true
rm -f -- "$HOME/.codex/notify-ntfy-wsl.sh" "$HOME/.codex/notify-ntfy.py"
```

The Windows installer keeps up to ten WSL snapshots in `~/.codex/ntfy-backups` inside each distribution. A selected snapshot can restore `config.toml`, the bridge scripts, and the private config. If later Codex settings must be retained, remove only the managed root line instead of replacing the whole TOML file.

WSL receives a copy of `ntfy-config.json`. Delete it only after confirming that no other WSL setup uses it. If native fallback was ever used, `~/.codex/ntfy-state` can contain pending events and sensitive data. The normal Windows-bridged queue is instead in the Windows state directory.

## Remove a native or Remote SSH Linux installation

Run the following on the actual Linux target as the installed user. For a Remote SSH host, connect first with the same alias/account used during installation.

### 1. Stop and remove the user service

```sh
systemctl --user disable --now codex-ntfy.service 2>/dev/null || true
rm -f -- "$HOME/.config/systemd/user/codex-ntfy.service"
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed codex-ntfy.service 2>/dev/null || true
```

An on-demand worker normally exits when the outbox is empty. To stop one deliberately, first review matching processes, then terminate them:

```sh
pgrep -af 'notify-ntfy.py.*--worker' || true
pkill -f '[n]otify-ntfy.py.*--worker' 2>/dev/null || true
```

That pattern can match more than one custom Codex home for the same user; review before running it.

### 2. Restore or remove the hook

If no Codex settings changed after installation, copy `config.toml` from the explicitly selected backup:

```sh
codex_home=${CODEX_HOME:-"$HOME/.codex"}
backup="$codex_home/ntfy-backups/YYYYMMDD-HHMMSS-NNNNNNNNN" # choose explicitly
test -f "$backup/config.toml"
cp -p -- "$backup/config.toml" "$codex_home/config.toml"
chmod 600 "$codex_home/config.toml"
```

Otherwise, privately back up the current file and remove only the single root-level line containing `notify-ntfy.py`. Restore any prior hook from a trusted pre-install backup. Verify with:

```sh
grep -nE '^[[:space:]]*notify[[:space:]]*=' "${CODEX_HOME:-$HOME/.codex}/config.toml"
```

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

managed = ("notify-ntfy.py", "install-remote-linux-target.py", "ntfy-config.json")
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

Replace the timestamp placeholder before running the script. Run `systemctl --user daemon-reload`, then explicitly enable/start the restored unit only if that matches the selected snapshot's intended state. State is not included in backups; isolate it before running a version whose queue schema is unknown.

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
