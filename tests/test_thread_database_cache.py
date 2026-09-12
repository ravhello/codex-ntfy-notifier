"""Causal same-process cache/resume tests with local SQLite and no delivery."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import tempfile
import threading
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
$ast = [Management.Automation.Language.Parser]::ParseFile($env:CACHE_NOTIFIER, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw ($errors | Out-String) }
foreach ($name in @('Get-ObjectValue', 'Get-FirstObjectValue', 'Initialize-WinSqlite',
    'Invoke-SqliteRow', 'Get-StateDatabasePath', 'Get-ThreadDatabaseInfo',
    'Test-RecordIdleGate', 'Get-ActiveDescendants', 'Get-DescendantThreadIds',
    'Get-UnknownGateResult', 'New-GateResult', 'Set-RecordValue')) {
  $definition = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
  }, $true))
  if ($definition.Count -ne 1) { throw "function not unique: $name" }
  . ([scriptblock]::Create($definition[0].Extent.Text))
}
function Sanitize-NotificationText { param($Text, $MaxLength) return [string]$Text }
$script:OriginalSqliteRow = (Get-Command Invoke-SqliteRow).ScriptBlock
$script:QueryCount = 0
function Invoke-SqliteRow {
  param($DatabasePath, $Sql, $Parameter, $ColumnCount)
  $script:QueryCount++
  & $script:OriginalSqliteRow @PSBoundParameters
}
$script:RecursiveAttempts = 0
function Find-RolloutPathByThread { $script:RecursiveAttempts++; throw 'unexpected history fallback' }
function Resolve-RolloutPath { $script:RecursiveAttempts++; throw 'unexpected candidate fallback' }
function Get-FastRolloutProbe {
  param($Path, $CandidateTurnId, $IncludeMessage)
  $script:SelectedProbePath = $Path
  throw 'PROBE_CAPTURED'
}
function Get-FastRolloutLatestProbe {
  param($Path, $IncludeMessage)
  $script:SelectedProbePath = $Path
  throw 'PROBE_CAPTURED'
}
$script:StateDatabasePathCache = @{}
$script:ThreadDatabaseCache = @{}
$script:RolloutProbeCache = @{}
$CodexHome = $env:CACHE_HOME
while ($null -ne ($line = [Console]::ReadLine())) {
  $request = $line | ConvertFrom-Json
  if ($request.action -eq 'quit') { break }
  $script:SelectedProbePath = ''
  $info = $null
  try {
    $targetHome = if ($request.home) { [string]$request.home } else { $CodexHome }
    $cacheKey = ($targetHome + '|' + $targetHome + '|' + $request.thread).ToLowerInvariant()
    if ($request.action -eq 'expire') {
      $script:ThreadDatabaseCache[$cacheKey].expiresAtTicks = 0
    } elseif ($request.action -eq 'extend') {
      $script:ThreadDatabaseCache[$cacheKey].expiresAtTicks = [Diagnostics.Stopwatch]::GetTimestamp() + 60 * [Diagnostics.Stopwatch]::Frequency
    } elseif ($request.action -eq 'lookup') {
      $info = Get-ThreadDatabaseInfo -ThreadId $request.thread -SqliteHome $targetHome -SessionHome $targetHome
    } else {
      $record = [pscustomobject]@{
        provider = 'codex'; thread_id = [string]$request.thread; turn_id = 'resumed-turn'
        created_unix_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        session_codex_home = $targetHome; session_sqlite_home = $targetHome
        session_classification = 'root'; candidate_rollout_path = [string]$request.candidate
        event = [pscustomobject]@{ source = 'vscode' }
      }
      $config = [pscustomobject]@{
        idleDetectionMode = 'strict'; idleProbeGraceSeconds = 30; goalPollSeconds = 0.1
        unknownRetryMaxSeconds = 1; suppressSubagents = $true; includeMessage = $false
        subagentOrphanSeconds = 1800
      }
      if ($request.action -eq 'gate') {
        $info = Test-RecordIdleGate -Record $record -Config $config
      } elseif ($request.action -eq 'children') {
        $info = Get-ActiveDescendants -Record $record -Config $config `
          -NowUnixMs ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) -SessionHome $targetHome -SqliteHome $targetHome
      }
    }
    $errorText = ''
  } catch {
    $errorText = [string]$_.Exception.Message
    if ($errorText -eq 'PROBE_CAPTURED') { $errorText = '' }
  }
  [pscustomobject]@{
    info = $info; queries = $script:QueryCount; recursion = $script:RecursiveAttempts
    cache_count = $script:ThreadDatabaseCache.Count; probe_path = $script:SelectedProbePath; error = $errorText
  } | ConvertTo-Json -Depth 8 -Compress
}
"""


@unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows SQLite cache")
class ThreadDatabaseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ntfy-db-cache-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / ".codex"
        self.home.mkdir()
        self.database = self.home / "state_5.sqlite"
        self.create_database(self.database)
        self.thread_id = str(uuid.uuid4())
        self.process = subprocess.Popen(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", HARNESS],
            env={**os.environ, "CACHE_NOTIFIER": str(NOTIFIER), "CACHE_HOME": str(self.home), "CODEX_NTFY_NO_SPAWN": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.output: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=self.read_output, daemon=True)
        self.reader.start()
        self.addCleanup(self.stop_process)

    def create_database(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, thread_source TEXT, source TEXT);"
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT);"
            )
            connection.commit()

    def read_output(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.output.put(line)

    def stop_process(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.write('{"action":"quit"}\n')
            self.process.stdin.flush()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def ask(self, action: str = "lookup", *, thread: str | None = None, **values: str) -> dict:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"action": action, "thread": thread or self.thread_id, **values}) + "\n")
        self.process.stdin.flush()
        try:
            result = json.loads(self.output.get(timeout=20))
        except queue.Empty:
            self.fail(f"cache harness did not respond; process exit={self.process.poll()}")
        self.assertEqual(result["error"], "", result)
        self.assertEqual(result["recursion"], 0, result)
        return result

    def rollout(self, name: str) -> Path:
        path = self.home / "sessions" / "2001" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"event_msg","payload":{"type":"task_complete","turn_id":"old-turn"}}\n', encoding="utf-8")
        return path

    def set_path(self, path: Path, *, thread: str | None = None, database: Path | None = None) -> None:
        database = database or self.database
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("INSERT OR REPLACE INTO threads VALUES (?, ?, '', 'vscode')", (thread or self.thread_id, str(path)))
            connection.commit()
        # Deliberately distinct file metadata makes immediate invalidation
        # independent of wall-clock/NTFS timestamp resolution.
        stat = database.stat()
        os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    def test_same_process_resume_invalidates_cached_rollout_immediately(self) -> None:
        old, new = self.rollout("old"), self.rollout("resumed")
        self.set_path(old)
        first = self.ask()
        self.ask("extend")
        unchanged = self.ask()
        self.assertEqual(unchanged["queries"], first["queries"])
        self.set_path(new)
        refreshed = self.ask()
        self.assertEqual(refreshed["info"]["rolloutPath"], str(new))
        self.assertGreater(refreshed["queries"], unchanged["queries"])

    def test_expired_cache_refreshes_even_when_database_stamp_is_unchanged(self) -> None:
        self.set_path(self.rollout("stable"))
        first = self.ask()
        self.ask("expire")
        self.assertGreater(self.ask()["queries"], first["queries"])

    def test_wal_update_invalidates_cache_without_main_database_change(self) -> None:
        old, new = self.rollout("wal-old"), self.rollout("wal-resume")
        self.set_path(old)
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        self.ask()
        # The first reader can materialize WAL sidecars; a changing read stamp
        # is intentionally not cacheable. Establish the now-stable snapshot.
        self.assertEqual(self.ask()["cache_count"], 1)
        self.ask("extend")
        database_stamp = self.database.stat().st_mtime_ns
        writer.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(new), self.thread_id))
        writer.commit()
        wal = Path(str(self.database) + "-wal")
        stat = wal.stat()
        os.utime(wal, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        self.assertEqual(self.database.stat().st_mtime_ns, database_stamp)
        self.assertEqual(self.ask()["info"]["rolloutPath"], str(new))

    def test_query_error_evicts_positive_cache_and_recovers_after_repair(self) -> None:
        path = self.rollout("repaired")
        self.set_path(path)
        self.ask()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE threads")
            connection.commit()
        self.ask("expire")
        failed = self.ask()
        self.assertFalse(failed["info"]["ok"])
        self.assertEqual(failed["cache_count"], 0)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, thread_source TEXT, source TEXT)")
            connection.commit()
        self.set_path(path)
        self.assertEqual(self.ask()["info"]["rolloutPath"], str(path))

    def test_missing_row_and_missing_file_are_not_negative_cached(self) -> None:
        missing = self.ask()
        self.assertFalse(missing["info"]["found"])
        self.assertEqual(missing["cache_count"], 0)
        future = self.home / "sessions" / "future.jsonl"
        self.set_path(future)
        absent = self.ask()
        self.assertTrue(absent["info"]["found"])
        self.assertEqual(absent["info"]["rolloutPath"], "")
        self.assertEqual(absent["cache_count"], 0)
        future.parent.mkdir(parents=True, exist_ok=True)
        future.write_text('{}\n', encoding="utf-8")
        self.assertEqual(self.ask()["info"]["rolloutPath"], str(future))

    def test_cache_isolates_threads_and_homes_in_one_worker(self) -> None:
        other = str(uuid.uuid4())
        first_path, other_path = self.rollout("first"), self.rollout("other")
        self.set_path(first_path)
        self.set_path(other_path, thread=other)
        self.assertEqual(self.ask()["info"]["rolloutPath"], str(first_path))
        self.assertEqual(self.ask(thread=other)["info"]["rolloutPath"], str(other_path))
        second_home = Path(self.temp.name) / "second-home"
        second_home.mkdir()
        second_database = second_home / "state_5.sqlite"
        self.create_database(second_database)
        self.set_path(other_path, database=second_database)
        self.assertEqual(self.ask(home=str(second_home))["info"]["rolloutPath"], str(other_path))
        self.assertEqual(self.ask()["info"]["rolloutPath"], str(first_path))

    def test_missing_resumed_path_never_reuses_completed_candidate(self) -> None:
        old = self.rollout("completed-old")
        self.set_path(old)
        self.ask()
        future = old.with_name("new-resume.jsonl")
        self.set_path(future)
        absent = self.ask("gate", candidate=str(old))
        self.assertEqual(absent["info"]["state"], "unknown")
        self.assertEqual(absent["probe_path"], "")
        future.write_text('{}\n', encoding="utf-8")
        available = self.ask("gate", candidate=str(old))
        self.assertEqual(available["probe_path"], str(future))

    def test_missing_indexed_child_does_not_fall_back_to_old_rollout(self) -> None:
        parent = str(uuid.uuid4())
        old = self.rollout("old-child")
        future = old.with_name("resumed-child.jsonl")
        self.set_path(future)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, 'running')", (parent, self.thread_id))
            connection.commit()
        absent = self.ask("children", thread=parent)
        self.assertFalse(absent["info"]["ok"])
        self.assertTrue(absent["info"]["busy"])
        self.assertTrue(absent["info"]["invalidEvidence"])
        future.write_text('{}\n', encoding="utf-8")
        self.assertEqual(self.ask("children", thread=parent)["probe_path"], str(future))


if __name__ == "__main__":
    unittest.main()
