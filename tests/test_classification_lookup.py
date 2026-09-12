"""Isolated Windows classification checks; no notifier worker or HTTP delivery."""

from __future__ import annotations

import datetime as dt
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
NOTIFIER = ROOT / "src" / "notify-ntfy.ps1"
POWERSHELL = (
    Path(os.environ.get("WINDIR", r"C:\Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)

HARNESS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
  $env:CLASSIFICATION_NOTIFIER, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw ($errors | Out-String) }
$names = @(
  'Get-ObjectValue', 'Get-FirstObjectValue', 'Initialize-WinSqlite',
  'Invoke-SqliteRow', 'Get-StateDatabasePath', 'Read-FirstLineShared',
  'Resolve-RolloutPath', 'Get-EventClassification', 'Get-ThreadDatabaseInfo'
)
foreach ($name in $names) {
  $definition = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  }, $true))
  if ($definition.Count -ne 1) { throw "function not unique: $name" }
  . ([scriptblock]::Create($definition[0].Extent.Text))
}
function Sanitize-NotificationText { param($Text, $MaxLength) return [string]$Text }
$script:StateDatabasePathCache = @{}
$script:ThreadDatabaseCache = @{}
$script:Enumerated = New-Object 'System.Collections.Generic.List[string]'
function Get-ChildItem {
  [CmdletBinding()]
  param([string]$LiteralPath, [string]$Filter, [switch]$File, [switch]$Recurse)
  if ($Recurse) { throw 'classification must never recurse through rollout history' }
  $script:Enumerated.Add($LiteralPath)
  Microsoft.PowerShell.Management\Get-ChildItem @PSBoundParameters
}
$CodexHome = $env:CLASSIFICATION_HOME
$event = [pscustomobject]@{ source = 'vscode' }
$classification = Get-EventClassification -Event $event `
  -ThreadId $env:CLASSIFICATION_THREAD -SessionHome $CodexHome
$databaseClassification = ''
if ($env:CLASSIFICATION_DATABASE_INFO -eq '1') {
  $info = Get-ThreadDatabaseInfo -ThreadId $env:CLASSIFICATION_THREAD `
    -SqliteHome $CodexHome -SessionHome $CodexHome
  $databaseClassification = [string]$info.classification
}
[pscustomobject]@{
  classification = $classification
  database_classification = $databaseClassification
  enumerated = @($script:Enumerated.ToArray())
} | ConvertTo-Json -Depth 5 -Compress
"""


class ClassificationSourceTests(unittest.TestCase):
    def test_classification_does_not_recursively_discover_rollouts(self) -> None:
        source = NOTIFIER.read_text(encoding="utf-8")
        function = source.split("function Get-EventClassification {", 1)[1].split(
            "function Get-RawNotification {", 1
        )[0]
        self.assertNotIn("-Recurse", function)
        self.assertNotIn("Resolve-RolloutPath", function)
        self.assertIn("thread_spawn_edges", function)


@unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows SQLite classification")
class WindowsClassificationLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ntfy-classification-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / ".codex"
        self.home.mkdir()
        self.thread_id = str(uuid.uuid4())
        self.database = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, "
                "thread_source TEXT, source TEXT);"
                "CREATE TABLE thread_spawn_edges (child_thread_id TEXT PRIMARY KEY);"
            )
            connection.commit()

    def rollout(self, *, historical: bool = False, identity: str | None = None) -> Path:
        date = "2001/02/03" if historical else dt.date.today().strftime("%Y/%m/%d")
        path = self.home / "sessions" / date / f"rollout-{self.thread_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": identity or self.thread_id, "source": "vscode"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def index(self, path: Path, *, source: str = "vscode") -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, '', ?)",
                (self.thread_id, str(path), source),
            )
            connection.commit()

    def classify(self, *, database_info: bool = False) -> dict:
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", HARNESS],
            env={
                **os.environ,
                "CODEX_NTFY_NO_SPAWN": "1",
                "CLASSIFICATION_NOTIFIER": str(NOTIFIER),
                "CLASSIFICATION_HOME": str(self.home),
                "CLASSIFICATION_THREAD": self.thread_id,
                "CLASSIFICATION_DATABASE_INFO": "1" if database_info else "0",
            },
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout)

    def assert_no_rollout_enumeration(self, result: dict) -> None:
        self.assertEqual(result["enumerated"], [str(self.home)])

    def test_indexed_historical_root_needs_no_rollout_enumeration(self) -> None:
        self.index(self.rollout(historical=True))
        result = self.classify(database_info=True)
        self.assertEqual(result["classification"], "root")
        self.assertEqual(result["database_classification"], "root")
        self.assert_no_rollout_enumeration(result)

    def test_spawn_edge_overrides_indexed_generic_source_in_both_paths(self) -> None:
        self.index(self.rollout(historical=True))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO thread_spawn_edges VALUES (?)", (self.thread_id,))
            connection.commit()
        result = self.classify(database_info=True)
        self.assertEqual(result["classification"], "subagent")
        self.assertEqual(result["database_classification"], "subagent")
        self.assert_no_rollout_enumeration(result)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM thread_spawn_edges")
            connection.commit()
        self.assertEqual(self.classify()["classification"], "root")

    def test_unindexed_current_root_uses_bounded_metadata_fallback(self) -> None:
        path = self.rollout()
        result = self.classify()
        self.assertEqual(result["classification"], "root")
        self.assertIn(str(path.parent), result["enumerated"])
        self.assertNotIn(str(self.home / "sessions"), result["enumerated"])

    def test_metadata_identity_mismatch_stays_unknown(self) -> None:
        self.rollout(identity=str(uuid.uuid4()))
        self.assertEqual(self.classify()["classification"], "unknown")

    def test_indexed_metadata_identity_mismatch_stays_unknown(self) -> None:
        self.index(self.rollout(historical=True, identity=str(uuid.uuid4())), source="")
        self.assertEqual(self.classify()["classification"], "unknown")

    def test_unindexed_historical_root_stays_unknown_without_recursion(self) -> None:
        self.rollout(historical=True)
        result = self.classify()
        self.assertEqual(result["classification"], "unknown")
        self.assert_no_rollout_enumeration(result)


if __name__ == "__main__":
    unittest.main()
