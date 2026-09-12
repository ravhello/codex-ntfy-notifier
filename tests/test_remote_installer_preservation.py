from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt", "Windows installer helpers")
class RemoteInstallerPreservationTests(unittest.TestCase):
    """Exercise isolated helper code only: no SSH, workers, tasks, or HTTP."""

    def run_probe(self, source: str, probe: str) -> None:
        with tempfile.TemporaryDirectory(prefix="ntfy-remote-preserve-") as directory:
            bootstrap = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:NTFY_TEST_INSTALLER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse failed' }
function Import-TestFunction([string]$name) {
    $node = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $false)
    if ($null -eq $node) { throw "missing $name" }
    return $node.Extent.Text
}
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
'''
            result = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", bootstrap + probe],
                env={**os.environ, "NTFY_TEST_INSTALLER": str(ROOT / source), "NTFY_TEST_DIR": directory},
                capture_output=True,
                text=True,
                timeout=40,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_remote_notify_chain_is_preserved_and_idempotent(self) -> None:
        self.run_probe("src/install-remote-windows-target.ps1", r'''
foreach ($name in @('Write-TextAtomic', 'Ensure-TopLevelNotify')) {
    . ([scriptblock]::Create((Import-TestFunction $name)))
}
$path = Join-Path $env:NTFY_TEST_DIR 'config.toml'
$wrapper = 'notify = ["integration.exe", "turn-ended", "--previous-notify", "[\"powershell.exe\",\"-File\",\"C:\\work\\notify-ntfy.ps1\"]"]'
$original = $wrapper + "`nmodel = 'retained'`n[projects]`n"
[IO.File]::WriteAllText($path, $original, $Utf8NoBom)
$replacement = 'notify = ["powershell.exe", "-NoProfile", "-File", "C:\\work\\notify-ntfy.ps1"]'
1..2 | ForEach-Object { Ensure-TopLevelNotify -ConfigPath $path -NotifyLine $replacement -ExpectedMarker 'notify-ntfy.ps1' }
if ([IO.File]::ReadAllText($path) -cne $original) { throw 'wrapper changed' }
[IO.File]::WriteAllText($path, 'notify = ["old", "notify-ntfy.ps1"]' + "`nmodel = 'retained'`n", $Utf8NoBom)
1..2 | ForEach-Object { Ensure-TopLevelNotify -ConfigPath $path -NotifyLine $replacement -ExpectedMarker 'notify-ntfy.ps1' }
if ([IO.File]::ReadAllText($path) -cne ($replacement + "`nmodel = 'retained'`n")) { throw 'direct notify not idempotent' }
$unrelated = 'notify = ["integration.exe", "--previous-notify", "[\"unrelated.exe\"]"]'
[IO.File]::WriteAllText($path, $unrelated, $Utf8NoBom)
$rejected = $false
try { Ensure-TopLevelNotify -ConfigPath $path -NotifyLine $replacement -ExpectedMarker 'notify-ntfy.ps1' } catch { $rejected = $true }
if (-not $rejected -or [IO.File]::ReadAllText($path) -cne $unrelated) { throw 'unrelated wrapper was not preserved and rejected' }
''')

    def test_upgrade_does_not_transfer_credentials_unless_explicit(self) -> None:
        self.run_probe("install-remote-windows.ps1", r'''
. ([scriptblock]::Create((Import-TestFunction 'Get-RemoteInstallFiles')))
$path = Join-Path $env:NTFY_TEST_DIR 'source-private.json'
foreach ($exists in @($false, $true)) {
    if ($exists) { [IO.File]::WriteAllText($path, '{"topic":"synthetic"}', $Utf8NoBom) }
    1..2 | ForEach-Object {
        $files = Get-RemoteInstallFiles -SourceRoot $env:NTFY_TEST_DIR -PrivateConfig $path -RemotePrivateConfigExists $true
        if ($files.Count -ne 4 -or $files.Contains('ntfy-config.json')) { throw 'upgrade transfers private config' }
    }
}
foreach ($replace in @($false, $true)) {
    $files = Get-RemoteInstallFiles -SourceRoot $env:NTFY_TEST_DIR -PrivateConfig $path -RemotePrivateConfigExists $false -ReplaceRemoteConfig:$replace
    if ($files.Count -ne 5 -or $files['ntfy-config.json'] -cne $path) { throw 'fresh install lost source config' }
}
$files = Get-RemoteInstallFiles -SourceRoot $env:NTFY_TEST_DIR -PrivateConfig $path -RemotePrivateConfigExists $true -ReplaceRemoteConfig
if ($files.Count -ne 5 -or $files['ntfy-config.json'] -cne $path) { throw 'explicit replacement lost source config' }
Remove-Item -LiteralPath $path
foreach ($remoteExists in @($false, $true)) {
    $rejected = $false
    try { Get-RemoteInstallFiles -SourceRoot $env:NTFY_TEST_DIR -PrivateConfig $path -RemotePrivateConfigExists $remoteExists -ReplaceRemoteConfig | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'missing replacement config accepted' }
}
''')

    def test_upgrade_preserves_host_local_settings_and_auth(self) -> None:
        self.run_probe("src/install-remote-windows-target.ps1", r'''
$source = [IO.File]::ReadAllText($env:NTFY_TEST_INSTALLER)
$start = $source.IndexOf('$privateObject = Get-Content')
$end = $source.IndexOf('$alias = $Origin')
if ($start -lt 0 -or $end -le $start) { throw 'missing isolated private config migration' }
$migration = [scriptblock]::Create($source.Substring($start, $end - $start))
$HomePath = $env:NTFY_TEST_DIR
$PrivateConfig = Join-Path $HomePath 'ntfy-config.json'
$env:CODEX_SQLITE_HOME = ''
$original = [ordered]@{
    server = 'https://example.invalid'; topic = 'synthetic-topic'; token = 'synthetic-token';
    username = 'synthetic-user'; password = 'synthetic-password';
    watch_roots = @([ordered]@{ path = 'D:\remote-root'; sqlite_path = 'D:\remote-db'; origin = 'remote-custom' });
    worker_sqlite_path = 'D:\remote-primary-db'; custom_setting = 'retained'
}
[IO.File]::WriteAllText($PrivateConfig, ($original | ConvertTo-Json -Depth 8), $Utf8NoBom)
$PreserveLocalConfiguration = $true
. $migration
$once = [IO.File]::ReadAllText($PrivateConfig)
. $migration
if ([IO.File]::ReadAllText($PrivateConfig) -cne $once) { throw 'migration is not idempotent' }
$actual = $once | ConvertFrom-Json
foreach ($name in @('server','topic','token','username','password','worker_sqlite_path','custom_setting')) {
    if ($actual.$name -cne $original[$name]) { throw "existing field changed: $name" }
}
if (@($actual.watch_roots).Count -ne 1 -or $actual.watch_roots[0].path -cne 'D:\remote-root') { throw 'host-local roots changed' }
if (-not $actual.watch_rollouts) { throw 'new default not added' }
$PreserveLocalConfiguration = $false
. $migration
$actual = Get-Content -Raw -LiteralPath $PrivateConfig | ConvertFrom-Json
if (@($actual.watch_roots).Count -ne 0 -or $actual.worker_sqlite_path -cne $HomePath) { throw 'fresh copied topology not reset' }
if ($actual.token -cne $original.token) { throw 'auth changed during topology reset' }
''')

    def test_remote_upgrade_wires_preservation_without_any_transport(self) -> None:
        self.run_probe("install-remote-windows.ps1", r'''
. ([scriptblock]::Create((Import-TestFunction 'Get-RemoteInstallFiles')))
. ([scriptblock]::Create((Import-TestFunction 'Get-RemoteOwnershipHelpers')))
$HostName = @('synthetic-host')
$SourceRoot = Join-Path (Split-Path -Parent $env:NTFY_TEST_INSTALLER) 'src'
$ownershipHelpers = Get-RemoteOwnershipHelpers -Path (Join-Path $SourceRoot 'install-remote-windows-target.ps1')
$PrivateConfig = Join-Path $env:NTFY_TEST_DIR 'absent-local-private-config.json'
$ReplaceRemoteConfig = $false
$SkipScheduledTask = $false
$script:SentNames = @()
$script:FinishScript = ''
$script:PrepareScript = ''
function Get-FileHash {
    param($LiteralPath, $Algorithm)
    return [pscustomobject]@{ Hash = ('0' * 64) }
}
function Send-RemoteTextFile {
    param($HostAlias, $LocalPath, $RemoteUserHome, $RemoteSubdirectory, $RemoteName)
    $script:SentNames += $RemoteName
}
function Invoke-RemotePowerShell {
    param($HostAlias, $Script)
    if ($Script.Contains('private_config_existed=')) {
        $script:PrepareScript = $Script
        return ([ordered]@{ user_home='C:\synthetic'; backup='C:\synthetic\.codex\ntfy-backups\synthetic'; private_config_existed=$true; task_existed=$true; task_running=$true } | ConvertTo-Json -Compress)
    }
    if ($Script.Contains('Remote target installer exited')) {
        $script:FinishScript = $Script
        return 'OK synthetic transport only'
    }
    return ''
}
$loop = $ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.ForEachStatementAst] -and $_.Variable.VariablePath.UserPath -eq 'hostAlias' } | Select-Object -First 1
if ($null -eq $loop) { throw 'missing remote orchestration loop' }
. ([scriptblock]::Create($loop.Extent.Text))
if ($script:SentNames.Count -ne 4 -or $script:SentNames -contains 'ntfy-config.json') { throw 'orchestration transferred credentials' }
$tokens = $null; $errors = $null
[void][Management.Automation.Language.Parser]::ParseInput($script:FinishScript, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'generated finish script is invalid' }
if (-not $script:FinishScript.Contains("if (`$name -eq 'ntfy-config.json' -and -not `$false) { continue }")) { throw 'private config replacement was not skipped' }
if (-not $script:FinishScript.Contains("if (-not `$false) { `$arguments += '-PreserveLocalConfiguration' }")) { throw 'target preservation flag missing' }
if (-not $script:FinishScript.Contains('Test-OwnedNotifierProcess -Process $_ -HomePath $homePath')) { throw 'exact process ownership helper not used' }
if (-not $script:FinishScript.Contains('[void]$runtime.Handle') -or -not $script:FinishScript.Contains('$runtime.Kill()') -or $script:FinishScript.Contains('Stop-Process -Id')) { throw 'process stop does not preserve pinned identity' }
if (-not $script:PrepareScript.Contains("'config.toml','hooks.json'")) { throw 'hooks not backed up' }
if (-not $script:FinishScript.Contains("@(`$managed + 'config.toml' + 'hooks.json')")) { throw 'hooks not restored by outer rollback' }
''')

    def test_process_selector_requires_exact_home_and_actual_worker_entrypoint(self) -> None:
        self.run_probe("src/install-remote-windows-target.ps1", r'''
foreach ($name in @('Get-NotifierCommandTokens', 'Test-OwnedNotifierInvocation', 'Test-OwnedNotifierProcess')) {
    . ([scriptblock]::Create((Import-TestFunction $name)))
}
$homePath = 'C:\Users\fixture\.codex'
$positive = @(
    @('powershell.exe', '"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\Users\fixture\.codex\notify-ntfy.ps1" -Worker -Continuous'),
    @('pwsh.exe', 'pwsh.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker -PollSeconds 2'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -ScanRollouts -ScanScope Local -ScanParentPid 123 -ScanParentToken "synthetic" -Continuous'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -Maintenance'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex\watch-codex-ntfy.ps1'),
    @('wscript.exe', 'wscript.exe //B //Nologo "C:\Users\fixture\.codex\watch-codex-ntfy-hidden.vbs"')
)
foreach ($fixture in $positive) {
    if (-not (Test-OwnedNotifierProcess -Process ([pscustomobject]@{Name=$fixture[0];CommandLine=$fixture[1]}) -HomePath $homePath)) { throw 'owned worker rejected' }
}
$negative = @(
    @('powershell.exe', 'powershell.exe -File C:\Users\unrelated\.codex\notify-ntfy.ps1 -Worker -Continuous'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex-other\notify-ntfy.ps1 -Worker'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1.backup -Worker'),
    @('powershell.exe', 'powershell.exe -Command "Write-Output ''C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker''"'),
    @('powershell.exe', 'powershell.exe -File C:\diagnostic.ps1 -Input C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -HookEvent -ReadStdin'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -Test'),
    @('powershell.exe', 'powershell.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -Origin "-Worker"'),
    @('powershell.exe', 'powershell.exe -File .codex\notify-ntfy.ps1 -Worker'),
    @('powershell.exe', 'unrelated.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker'),
    @('codex.exe', 'codex.exe -File C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker'),
    @('wscript.exe', 'wscript.exe "C:\Users\unrelated\.codex\watch-codex-ntfy-hidden.vbs"'),
    @('wscript.exe', 'wscript.exe C:\diagnostic.vbs "C:\Users\fixture\.codex\watch-codex-ntfy-hidden.vbs"')
)
foreach ($fixture in $negative) {
    if (Test-OwnedNotifierProcess -Process ([pscustomobject]@{Name=$fixture[0];CommandLine=$fixture[1]}) -HomePath $homePath) { throw 'unrelated process selected' }
}
''')

    def test_scheduled_task_requires_all_actions_to_belong_to_exact_home(self) -> None:
        self.run_probe("src/install-remote-windows-target.ps1", r'''
foreach ($name in @('Get-NotifierCommandTokens', 'Test-OwnedNotifierInvocation', 'Test-OwnedScheduledTask')) {
    . ([scriptblock]::Create((Import-TestFunction $name)))
}
$homePath = 'C:\Users\fixture\.codex'
$owned = [pscustomobject]@{Execute='C:\Windows\System32\wscript.exe';Arguments='//B //Nologo "C:\Users\fixture\.codex\watch-codex-ntfy-hidden.vbs"'}
$foreign = [pscustomobject]@{Execute='C:\Windows\System32\wscript.exe';Arguments='//B //Nologo "C:\Users\unrelated\.codex\watch-codex-ntfy-hidden.vbs"'}
$diagnostic = [pscustomobject]@{Execute='powershell.exe';Arguments='-Command "Write-Output ''C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker''"'}
if (-not (Test-OwnedScheduledTask -Task ([pscustomobject]@{Actions=@($owned)}) -HomePath $homePath)) { throw 'owned task rejected' }
foreach ($actions in @(@($foreign), @($owned,$foreign), @($diagnostic))) {
    if (Test-OwnedScheduledTask -Task ([pscustomobject]@{Actions=$actions}) -HomePath $homePath) { throw 'unrelated task selected' }
}
if (Test-OwnedScheduledTask -Task ([pscustomobject]@{Actions=@()}) -HomePath $homePath) { throw 'empty task selected' }
''')

    def test_remote_stop_hook_retains_other_hooks_without_duplicates(self) -> None:
        self.run_probe("src/install-remote-windows-target.ps1", r'''
foreach ($name in @('Write-TextAtomic', 'Test-ManagedHookHandler', 'Ensure-StopHook')) {
    . ([scriptblock]::Create((Import-TestFunction $name)))
}
$path = Join-Path $env:NTFY_TEST_DIR 'hooks.json'
$original = '{"custom":"retained","hooks":{"Stop":[{"hooks":[{"type":"command","command":"unrelated.exe"}]}],"Other":[{"hooks":[{"type":"command","command":"another.exe"}]}]}}'
[IO.File]::WriteAllText($path, $original, $Utf8NoBom)
$command = '"powershell.exe" -File "C:\work\notify-ntfy.ps1" -HookEvent'
Ensure-StopHook -Path $path -Command $command
$once = [IO.File]::ReadAllText($path)
Ensure-StopHook -Path $path -Command $command
if ([IO.File]::ReadAllText($path) -cne $once) { throw 'hook installation is not idempotent' }
$actual = $once | ConvertFrom-Json
if ($actual.custom -cne 'retained' -or @($actual.hooks.Stop).Count -ne 2 -or @($actual.hooks.Other).Count -ne 1) { throw 'unrelated hooks or fields changed' }
if ($actual.hooks.Stop[0].hooks[0].command -cne 'unrelated.exe') { throw 'unrelated Stop handler changed' }
''')


if __name__ == "__main__":
    unittest.main()
