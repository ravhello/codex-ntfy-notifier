"""Bounded local index discovery without hooks, workers, or HTTP delivery."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
NOTIFIER = ROOT / "src/notify-ntfy.ps1"
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"

HARNESS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
  $env:WATCH_CAPACITY_NOTIFIER, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw ($errors | Out-String) }
foreach ($name in @('Get-ObjectValue', 'Get-FirstObjectValue', 'Initialize-WinSqlite',
    'Get-StateDatabasePath', 'Invoke-SqliteRows', 'Get-RecentThreadRolloutPaths',
    'ConvertTo-NormalizedWatchPath', 'Test-RemoteWatchPath', 'Resolve-RolloutPath',
    'Read-FirstLineShared', 'Get-RolloutMetadata', 'Add-RolloutWatchEntry',
    'Get-RecentRolloutFiles', 'Invoke-RolloutWatchScan')) {
  $definition = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
  }, $true))
  if ($definition.Count -ne 1) { throw "function not unique: $name" }
  . ([scriptblock]::Create($definition[0].Extent.Text))
}
function Sanitize-NotificationText { param($Text, $MaxLength) return [string]$Text }
$script:ScannedPaths = New-Object 'System.Collections.Generic.List[string]'
function Scan-RolloutFile {
  param($Entry, $Config, $NowUnixMs)
  $script:ScannedPaths.Add([string]$Entry.rollout_path)
  return 1
}
function Get-ChildItem {
  [CmdletBinding()]
  param([string]$LiteralPath, [string]$Filter, [switch]$File, [switch]$Recurse)
  if ($Recurse) { throw 'local discovery must not traverse rollout history' }
  Microsoft.PowerShell.Management\Get-ChildItem @PSBoundParameters
}
$CodexHome = $env:WATCH_CAPACITY_HOME
$WatchDir = Join-Path $CodexHome 'ntfy-state/watch'
$ScanScope = 'Local'
$script:StateDatabasePathCache = @{}
$script:RolloutDiscoveryCache = @{}
$script:DurableCursorCache = @{}
$script:NextDurableCursorRefreshUnixMs = 0
$config = [pscustomobject]@{
  workerSqlitePath = $CodexHome; watchRoots = @()
  watchDiscoverySeconds = 60; watchInitialReplaySeconds = 15; watchCursorBatchSize = 64
}
$observed = Invoke-RolloutWatchScan -Config $config -NowUnixMs ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
[pscustomobject]@{ observed = $observed; paths = @($script:ScannedPaths.ToArray()) } |
  ConvertTo-Json -Depth 3 -Compress
"""


@unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows local index discovery")
class WatchIndexCapacityTests(unittest.TestCase):
    def scan_indexed_roots(self, root_count: int) -> tuple[dict, set[str]]:
        with tempfile.TemporaryDirectory(prefix="ntfy-watch-capacity-") as directory:
            home = Path(directory) / ".codex"
            # Neither the current-day nor yesterday filesystem shortcut can find
            # these files. Every observed path must come from the actual index.
            old_bucket = home / "sessions/2001/01/01"
            old_bucket.mkdir(parents=True)
            paths: set[str] = set()
            now_ms = int(time.time() * 1000)
            with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
                database.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, "
                    "updated_at INTEGER, updated_at_ms INTEGER)"
                )
                for index in range(root_count):
                    thread_id = str(uuid.uuid4())
                    rollout = old_bucket / f"rollout-{thread_id}.jsonl"
                    rollout.write_text(
                        json.dumps({"type": "session_meta", "payload": {"id": thread_id, "source": "vscode"}})
                        + "\n",
                        encoding="utf-8",
                    )
                    paths.add(str(rollout))
                    database.execute(
                        "INSERT INTO threads VALUES (?, ?, 'vscode', ?, ?)",
                        (thread_id, str(rollout), now_ms // 1000, now_ms - index),
                    )
                database.commit()
            result = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", HARNESS],
                env={
                    **os.environ,
                    "WATCH_CAPACITY_NOTIFIER": str(NOTIFIER),
                    "WATCH_CAPACITY_HOME": str(home),
                    "CODEX_NTFY_NO_SPAWN": "1",
                },
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
            return json.loads(result.stdout), paths

    def test_one_local_scan_reaches_twenty_old_date_indexed_roots_without_hooks(self) -> None:
        result, expected = self.scan_indexed_roots(20)
        self.assertEqual(result["observed"], 20)
        self.assertEqual(set(result["paths"]), expected)

    def test_recent_index_discovery_still_has_a_sixty_four_path_bound(self) -> None:
        result, expected = self.scan_indexed_roots(80)
        self.assertEqual(result["observed"], 64)
        self.assertEqual(len(set(result["paths"])), 64)
        self.assertTrue(set(result["paths"]).issubset(expected))


if __name__ == "__main__":
    unittest.main()
