"""Durable orphan-age regressions; SQLite fixtures and loopback delivery only."""

from __future__ import annotations

from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import test_notifiers as contracts
import test_thread_database_cache as cache_fixture
from notifier_test_io import read_text_shared_delete


ROOT = Path(__file__).resolve().parents[1]
NOW = 2_000_000_000_000
OLD = NOW - 1_801_000
FIELDS = ("updated_at_ms", "updated_at", "recency_at_ms", "recency_at")
HAS_PS = os.name == "nt" and cache_fixture.POWERSHELL.exists()


class DescendantOrphanActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("orphan_activity_notifier", ROOT / "src/notify-ntfy.py")
        assert spec is not None and spec.loader is not None
        cls.notifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.notifier)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="ntfy-orphan-activity-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.records: dict[tuple[str, str], dict] = {}
        self.cache = None
        if HAS_PS:
            # Compose the persistent AST fixture without inheriting its test
            # methods, which would silently execute its entire suite twice.
            self.cache = cache_fixture.ThreadDatabaseCacheTests(methodName="runTest")
            self.cache.setUp()
            self.addCleanup(self.cache.doCleanups)
        network_guard = mock.patch.object(self.notifier.urllib.request, "urlopen",
                                          side_effect=AssertionError("unexpected HTTP"))
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def make_case(self, values: dict | None = None, *, fields=FIELDS,
                  child: str | None = None, parent: str | None = None) -> tuple[Path, str, str, Path]:
        home = self.root / str(uuid.uuid4())
        home.mkdir()
        child, parent = child or str(uuid.uuid4()), parent or str(uuid.uuid4())
        missing = home / "sessions" / f"rollout-{child}.jsonl"
        with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
            # No affinity: malformed TEXT/REAL/BLOB must stay malformed rather
            # than being silently converted to an integer by the fixture.
            extra = "".join(f", {field}" for field in fields)
            database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, thread_source TEXT, source TEXT" + extra + ")")
            database.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT)")
            database.execute("INSERT INTO threads(id,rollout_path,thread_source,source) VALUES (?,?,'subagent','vscode')", (child, str(missing)))
            database.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'running')", (parent, child))
            for field, value in (values or {}).items():
                database.execute(f"UPDATE threads SET {field}=? WHERE id=?", (value, child))
            database.commit()
        return home, parent, child, missing

    def check_children(self, home: Path, parent: str, child: str, *, now=NOW,
                       busy: bool, anchor: int | None = None, durable=False, orphan_seconds=1800) -> None:
        key = (str(home), parent)
        record = self.records.setdefault(key, {"thread_id": parent, "session_codex_home": str(home), "session_sqlite_home": str(home)})
        config = {"subagent_orphan_seconds": orphan_seconds, "idle_detection_mode": "strict"}
        result = self.notifier.active_descendants(SimpleNamespace(codex_home=home), record, config, now)
        self.assertEqual(result, (busy, int(busy), False), (result, record))
        if anchor is not None:
            self.assertEqual(record["descendant_unknown_since"][child], anchor)
        if durable:
            self.assertNotIn(child, record["descendant_unknown_since"])
        if self.cache is not None:
            observed = self.cache.ask("children", home=str(home), thread=parent,
                                      now=str(now), orphan_seconds=orphan_seconds)
            self.assertTrue(observed["info"]["ok"], observed)
            self.assertEqual(observed["info"]["busy"], busy, observed)
            self.assertEqual(observed["info"]["count"], int(busy), observed)
            self.assertFalse(observed["info"]["invalidEvidence"], observed)
            if anchor is not None:
                self.assertEqual(observed["unknown_since"][child], anchor, observed)
            if durable:
                self.assertNotIn(child, observed["unknown_since"], observed)

    def test_integer_activity_schema_parity_and_conservative_unknowns(self) -> None:
        cases = [
            ("updated_ms", {"updated_at_ms": OLD}, FIELDS, OLD),
            ("updated_seconds_legacy", {"updated_at": OLD // 1000}, ("updated_at",), OLD // 1000 * 1000),
            ("recency_seconds_legacy", {"recency_at": OLD // 1000}, ("recency_at",), OLD // 1000 * 1000),
            ("recency_ms_latest", {"updated_at_ms": OLD, "recency_at_ms": NOW - 50}, FIELDS, NOW - 50),
            ("seconds_latest", {"updated_at_ms": OLD, "recency_at": NOW // 1000 - 1}, FIELDS, NOW - 1000),
            ("updated_latest", {"updated_at_ms": NOW - 10, "recency_at_ms": OLD}, FIELDS, NOW - 10),
            ("all_absent", {}, FIELDS, 0),
            ("zeros", dict.fromkeys(FIELDS, 0), FIELDS, 0),
            ("schema_without_activity", {}, (), 0),
            ("text", {"updated_at_ms": str(OLD)}, FIELDS, 0),
            ("real", {"updated_at_ms": float(OLD)}, FIELDS, 0),
            ("real_zero", {"updated_at_ms": 0.0}, FIELDS, 0),
            ("blob", {"updated_at_ms": b"123"}, FIELDS, 0),
            ("negative", {"updated_at_ms": -1}, FIELDS, 0),
            ("seconds_overflow", {"updated_at": 9_223_372_036_854_776}, FIELDS, 0),
            ("future", {"updated_at_ms": NOW + 1}, FIELDS, 0),
            ("invalid_sibling", {"updated_at_ms": OLD, "recency_at": "bad"}, FIELDS, 0),
            ("future_sibling", {"updated_at_ms": OLD, "recency_at_ms": NOW + 1}, FIELDS, 0),
        ]
        for name, values, fields, expected in cases:
            with self.subTest(case=name):
                home, parent, child, _ = self.make_case(values, fields=fields)
                self.assertEqual(self.notifier.thread_last_activity_unix_ms(home, child, NOW), expected)
                self.check_children(home, parent, child, busy=not expected or NOW - expected < 1_800_000,
                                    anchor=None if expected else NOW, durable=bool(expected))

    def test_old_missing_child_anchor_survives_parent_candidates_and_rearms_on_resume(self) -> None:
        home, parent, child, _ = self.make_case({"updated_at_ms": OLD})
        self.check_children(home, parent, child, busy=False, durable=True)
        self.check_children(home, parent, child, now=NOW + 100, busy=False, durable=True)
        # A separate parent candidate must not start another 30-minute wait.
        second = str(uuid.uuid4())
        with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
            database.execute("UPDATE thread_spawn_edges SET parent_thread_id=?", (second,))
            database.commit()
        self.check_children(home, second, child, busy=False, durable=True)
        with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
            database.execute("UPDATE threads SET updated_at_ms=? WHERE id=?", (NOW + 100, child))
            database.commit()
        self.check_children(home, second, child, now=NOW + 100, busy=True, durable=True)
        self.check_children(home, second, child, now=NOW + 1_800_101, busy=False, durable=True)
        self.check_children(home, second, child, now=NOW + 1_800_101, busy=True, durable=True, orphan_seconds=0)

    def test_lost_or_invalid_activity_cannot_reuse_an_expired_durable_anchor(self) -> None:
        for bad_value in ("malformed", NOW + 1):
            with self.subTest(value=bad_value):
                home, parent, child, _ = self.make_case({"updated_at_ms": OLD})
                self.check_children(home, parent, child, busy=False, durable=True)
                with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
                    database.execute("UPDATE threads SET updated_at_ms=?", (bad_value,))
                    database.commit()
                self.check_children(home, parent, child, busy=True, anchor=NOW)
                with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
                    database.execute("UPDATE threads SET updated_at_ms=?", (OLD,))
                    database.commit()
                self.check_children(home, parent, child, busy=False, durable=True)
                # A schema regression makes the activity query unavailable,
                # while the indexed missing file and spawn graph remain valid.
                with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
                    database.execute("DROP TABLE threads")
                    database.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,thread_source TEXT,source TEXT)")
                    database.execute("INSERT INTO threads VALUES (?,?,'subagent','vscode')",
                                     (child, str(home / 'sessions' / f'rollout-{child}.jsonl')))
                    database.commit()
                self.check_children(home, parent, child, now=NOW + 10, busy=True, anchor=NOW + 10)

    def test_unknown_metadata_keeps_candidate_clock_and_home_identity(self) -> None:
        home, parent, child, _ = self.make_case()
        self.check_children(home, parent, child, busy=True, anchor=NOW)
        self.check_children(home, parent, child, now=NOW + 100, busy=True, anchor=NOW)
        self.check_children(home, parent, child, now=NOW + 1_800_001, busy=False, anchor=NOW)
        other, _, _, _ = self.make_case({"updated_at_ms": OLD}, child=child, parent=parent)
        self.check_children(other, parent, child, busy=False, durable=True)
        self.check_children(home, parent, child, now=NOW + 100, busy=True, anchor=NOW)
        self.assertEqual(self.notifier.thread_last_activity_unix_ms(home, str(uuid.uuid4()), NOW), 0)
        with closing(sqlite3.connect(home / "state_5.sqlite")) as database:
            database.execute("DROP TABLE threads")
            database.commit()
        self.assertEqual(self.notifier.thread_last_activity_unix_ms(home, child, NOW), 0)


class DescendantOrphanDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        contracts.NotifierContractTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        contracts.NotifierContractTests.tearDownClass()

    def new_fixture(self):
        fixture = contracts.NotifierContractTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.configure(idle_detection_mode="strict", idle_grace_seconds=0,
                          goal_poll_seconds=0.05, subagent_orphan_seconds=1800,
                          suppress_technical_turns=True, watch_rollouts=False)
        return fixture

    def prepare(self, fixture, *, recent: bool):
        parent, child, turn = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        root = fixture.write_session_meta(parent, subagent=False)
        missing = fixture.write_session_meta(child, subagent=True)
        fixture.append_rollout(root, "task_started", turn_id=turn)
        fixture.append_rollout(root, "user_message", message="Wait for current work")
        fixture.append_rollout(root, "task_complete", turn_id=turn, message="Root finished")
        database = fixture.create_state_database(parent, root, child, missing)
        missing.unlink()
        with closing(sqlite3.connect(database)) as connection:
            if "updated_at_ms" not in {row[1] for row in connection.execute("PRAGMA table_info(threads)")}:
                connection.execute("ALTER TABLE threads ADD COLUMN updated_at_ms INTEGER")
            connection.execute("UPDATE threads SET updated_at_ms=? WHERE id=?",
                               (int(time.time() * 1000) - (100 if recent else 86_400_000), child))
            connection.commit()
        return parent, child, turn, missing

    def test_completed_root_with_historical_missing_child_delivers_without_new_orphan_wait(self) -> None:
        fixture = self.new_fixture()
        for implementation in fixture.implementations():
            with self.subTest(implementation=implementation):
                if fixture.state.exists():
                    shutil.rmtree(fixture.state)
                # Keep the graph isolated for each implementation.
                with closing(sqlite3.connect(fixture.state_database)) as database:
                    database.execute("DELETE FROM thread_spawn_edges")
                    database.commit()
                parent, _, turn, _ = self.prepare(fixture, recent=False)
                event = fixture.event(thread_id=parent, turn_id=turn)
                fixture.run_ok(fixture.hook_command(implementation, event))
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                self.assertEqual(len(fixture.wait_for_payloads(1)), 1)
                fixture.run_ok(fixture.hook_command(implementation, event))
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                with fixture.server.lock:
                    self.assertEqual(len(fixture.server.payloads), 1)
                    fixture.server.payloads.clear()

    def wait_for_child_gate(self, fixture, process, *, after_due: int = 0) -> dict:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with fixture.server.lock:
                self.assertEqual(fixture.server.payloads, [], "intermediate root completion escaped the child gate")
            for path in (fixture.state / "pending").glob("*.json"):
                try:
                    record = json.loads(read_text_shared_delete(path))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                reason = record.get("idle_reason") or record.get("gate_reason")
                if reason == "subagents-active" and record.get("active_descendants") == 1:
                    due = record.get("next_probe_unix_ms") or record.get("next_attempt_unix_ms", 0)
                    if due > after_due:
                        return record
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"worker exited before child gate rc={process.returncode}\n{stdout}\n{stderr}")
            time.sleep(0.05)
        self.fail("worker never persisted the causal child gate")

    def test_recent_missing_child_appears_active_and_only_its_completion_releases_root(self) -> None:
        fixture = self.new_fixture()
        for implementation in fixture.implementations():
            with self.subTest(implementation=implementation):
                shutil.rmtree(fixture.state, ignore_errors=True)
                with closing(sqlite3.connect(fixture.state_database)) as database:
                    database.execute("DELETE FROM thread_spawn_edges")
                    database.commit()
                parent, child, turn, missing = self.prepare(fixture, recent=True)
                event = fixture.event(thread_id=parent, turn_id=turn)
                fixture.run_ok(fixture.hook_command(implementation, event))
                process = subprocess.Popen(fixture.continuous_worker_command(implementation), env=fixture.env,
                                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    first = self.wait_for_child_gate(fixture, process)
                    self.assertEqual(fixture.write_session_meta(child, subagent=True), missing)
                    child_turn = str(uuid.uuid4())
                    fixture.append_rollout(missing, "task_started", turn_id=child_turn)
                    with closing(sqlite3.connect(fixture.state_database)) as database:
                        database.execute("UPDATE threads SET updated_at_ms=? WHERE id=?",
                                         (int(time.time() * 1000) - 86_400_000, child))
                        database.commit()
                    # With old registry activity the missing-file gate would
                    # release immediately. A newly persisted later retry with
                    # no HTTP therefore proves the active file was observed.
                    first_due = first.get("next_probe_unix_ms") or first["next_attempt_unix_ms"]
                    self.wait_for_child_gate(fixture, process,
                                             after_due=max(first_due, int(time.time() * 1000) + 250))
                    fixture.append_rollout(missing, "task_complete", turn_id=child_turn, message="Child finished")
                    self.assertEqual(len(fixture.wait_for_payloads(1, timeout=60)), 1)
                finally:
                    fixture.stop_continuous_worker(process)
                fixture.run_ok(fixture.hook_command(implementation, event))
                fixture.run_ok(fixture.worker_command(implementation), timeout=60)
                with fixture.server.lock:
                    self.assertEqual(len(fixture.server.payloads), 1)
                    fixture.server.payloads.clear()


@unittest.skipUnless(HAS_PS, "Windows scan health writer")
class RolloutScanHealthRecoveryTests(unittest.TestCase):
    def test_corrupt_derived_health_is_replaced_and_later_completion_is_preserved(self) -> None:
        harness = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:HEALTH_NOTIFIER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'source parser error' }
foreach ($name in @('Get-ObjectValue','ConvertTo-CompactJson','Read-JsonFile','Write-JsonAtomic','Write-RolloutScanHealth')) {
  $definition = @($ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true))
  if ($definition.Count -ne 1) { throw "function not unique: $name" }
  . ([scriptblock]::Create($definition[0].Extent.Text))
}
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
function Write-RuntimeLog { param($Message) throw "unexpected health failure: $Message" }
function Invoke-RestMethod { throw 'unexpected HTTP' }
function Invoke-WebRequest { throw 'unexpected HTTP' }
$ScanHealthPath = Join-Path $env:HEALTH_DIRECTORY 'watch-health.json'
$RemoteScanHealthPath = Join-Path $env:HEALTH_DIRECTORY 'remote-watch-health.json'
foreach ($ScanScope in @('Local','Remote')) {
  $path = if ($ScanScope -eq 'Local') { $ScanHealthPath } else { $RemoteScanHealthPath }
  [IO.File]::WriteAllText($path, '{"status":', $Utf8NoBom)
  Write-RolloutScanHealth -Status running -StartedAt '2026-01-01T00:00:00Z'
  $first = Read-JsonFile -Path $path
  if ($first.status -ne 'running' -or $first.last_completed_at -ne '' -or $first.last_duration_ms -ne 0) { throw 'corrupt snapshot was not replaced' }
  Write-RolloutScanHealth -Status completed -StartedAt '2026-01-01T00:00:00Z' -CompletedAt '2026-01-01T00:00:01Z' -DurationMs 1000 -Observed 2
  Write-RolloutScanHealth -Status running -StartedAt '2026-01-01T00:00:02Z'
  $last = Read-JsonFile -Path $path
  if ($last.status -ne 'running' -or $last.last_completed_at -ne '2026-01-01T00:00:01Z' -or $last.last_duration_ms -ne 1000) { throw 'valid prior completion lost' }
}
'HEALTH_RECOVERY_OK'
"""
        with tempfile.TemporaryDirectory(prefix="ntfy-derived-health-") as directory:
            result = subprocess.run([str(cache_fixture.POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", harness],
                                    env={**os.environ, "HEALTH_NOTIFIER": str(ROOT / "src/notify-ntfy.ps1"),
                                         "HEALTH_DIRECTORY": directory, "CODEX_NTFY_NO_SPAWN": "1"},
                                    text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
            self.assertEqual(result.stdout.strip(), "HEALTH_RECOVERY_OK")


if __name__ == "__main__":
    unittest.main()
