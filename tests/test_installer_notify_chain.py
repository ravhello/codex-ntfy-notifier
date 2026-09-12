from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt", "Windows installer")
class NotifyChainTests(unittest.TestCase):
    def test_installer_process_scope_is_exact_home_and_worker_only(self) -> None:
        probe = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:NTFY_TEST_INSTALLER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse failed' }
$node = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-OwnedNotifierProcess' }, $false)
. ([scriptblock]::Create($node.Extent.Text))
$homePath = 'C:\Users\fixture\.codex'
$cases = @(
  @('powershell.exe', 'powershell.exe -File "C:\Users\fixture\.codex\notify-ntfy.ps1" -Worker', $true),
  @('powershell.exe', 'powershell.exe -File "C:\Users\other\.codex\notify-ntfy.ps1" -Worker', $false),
  @('powershell.exe', 'powershell.exe -File "C:\Users\fixture\.codex\notify-ntfy.ps1" -HookEvent', $false),
  @('powershell.exe', 'powershell.exe -File "C:\Users\fixture\.codex-other\notify-ntfy.ps1" -Worker', $false),
  @('powershell.exe', 'powershell.exe -File "C:\Users\fixture\.codex\notify-ntfy.ps1.bak" -Worker', $false),
  @('powershell.exe', 'powershell.exe -File "C:\tools\unrelated.ps1" -Input "C:\Users\fixture\.codex\notify-ntfy.ps1" -Worker', $false),
  @('powershell.exe', 'powershell.exe -Command "Write-Output ''C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker''"', $false),
  @('powershell.exe', 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\Users\fixture\.codex\notify-ntfy.ps1" -ScanRollouts -Continuous', $true),
  @('wscript.exe', 'wscript.exe //B "C:\Users\fixture\.codex\watch-codex-ntfy-hidden.vbs"', $true),
  @('codex.exe', 'codex.exe C:\Users\fixture\.codex\notify-ntfy.ps1 -Worker', $false)
)
foreach ($case in $cases) {
  $actual = Test-OwnedNotifierProcess -Process ([pscustomobject]@{Name=$case[0];CommandLine=$case[1]}) -HomePath $homePath
  if ($actual -ne $case[2]) { throw ('wrong ownership: ' + $case[1]) }
}
$taskFunction = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-OwnedScheduledTask' }, $false)
. ([scriptblock]::Create($taskFunction.Extent.Text))
$CodexHome = $homePath
$ownedAction = [pscustomobject]@{Execute='C:\Windows\System32\wscript.exe';Arguments='//B "C:\Users\fixture\.codex\watch-codex-ntfy-hidden.vbs"'}
if (-not (Test-OwnedScheduledTask ([pscustomobject]@{Actions=@($ownedAction)}))) { throw 'owned task rejected' }
$unrelatedAction = [pscustomobject]@{Execute='C:\Windows\System32\cmd.exe';Arguments='/c unrelated'}
if (Test-OwnedScheduledTask ([pscustomobject]@{Actions=@($ownedAction,$unrelatedAction)})) { throw 'mixed task accepted' }
'''
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", probe],
            env={**os.environ, "NTFY_TEST_INSTALLER": str(ROOT / "install.ps1")},
            capture_output=True, text=True, timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_upgrade_preserves_wrapper_and_updates_direct_notify(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ntfy-notify-chain-") as directory:
            probe = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:NTFY_TEST_INSTALLER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse failed' }
foreach ($name in @('Write-TextAtomic', 'Ensure-TopLevelNotify')) {
    $node = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $false)
    if ($null -eq $node) { throw "missing $name" }
    . ([scriptblock]::Create($node.Extent.Text))
}
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
$path = Join-Path $env:NTFY_TEST_DIR 'config.toml'
$wrapper = 'notify = ["python", "integration.py", "--previous-notify", "[\"powershell.exe\",\"-File\",\"C:\\work\\notify-ntfy.ps1\"]"]'
$original = $wrapper + "`nmodel = 'retained'`n[projects]`n"
[IO.File]::WriteAllText($path, $original, $Utf8NoBom)
$replacement = 'notify = ["powershell.exe", "-NoProfile", "-File", "C:\\work\\notify-ntfy.ps1"]'
Ensure-TopLevelNotify -ConfigPath $path -NotifyLine $replacement -ExpectedMarker 'notify-ntfy.ps1'
if ([IO.File]::ReadAllText($path) -cne $original) { throw 'wrapper changed' }
[IO.File]::WriteAllText($path, 'notify = ["old", "notify-ntfy.ps1"]' + "`nmodel = 'retained'`n", $Utf8NoBom)
Ensure-TopLevelNotify -ConfigPath $path -NotifyLine $replacement -ExpectedMarker 'notify-ntfy.ps1'
if ([IO.File]::ReadAllText($path) -cne ($replacement + "`nmodel = 'retained'`n")) { throw 'direct notify not updated' }
[IO.File]::WriteAllText($path, 'notify = ["unrelated.exe"]', $Utf8NoBom)
$rejected = $false
try { Ensure-TopLevelNotify -ConfigPath $path -NotifyLine $replacement -ExpectedMarker 'notify-ntfy.ps1' } catch { $rejected = $true }
if (-not $rejected) { throw 'unrelated notify overwritten' }
'''
            result = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", probe],
                env={**os.environ, "NTFY_TEST_INSTALLER": str(ROOT / "install.ps1"), "NTFY_TEST_DIR": directory},
                capture_output=True, text=True, timeout=40,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
