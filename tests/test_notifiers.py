from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_NOTIFIER = ROOT / "src" / "notify-ntfy.ps1"
PYTHON_NOTIFIER = ROOT / "src" / "notify-ntfy.py"
INSTALLER = ROOT / "install.ps1"
WINDOWS_POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


class RecordingServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), RecordingHandler)
        self.payloads: list[dict] = []
        self.statuses: list[int] = []
        self.redirect_url: str | None = None
        self.redirect_hits = 0
        self.lock = threading.Lock()


class RecordingHandler(BaseHTTPRequestHandler):
    server: RecordingServer

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        with self.server.lock:
            self.server.payloads.append(payload)
            status = self.server.statuses.pop(0) if self.server.statuses else 200
        response = json.dumps({"id": f"test-{len(self.server.payloads)}"}).encode()
        self.send_response(status)
        if 300 <= status < 400 and self.server.redirect_url:
            self.send_header("Location", self.server.redirect_url)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self) -> None:  # noqa: N802
        with self.server.lock:
            self.server.redirect_hits += 1
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class NotifierContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = RecordingServer()
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="codex-ntfy-test-"))
        self.codex_home = self.temp / "codex-home"
        self.state = self.temp / "state"
        self.codex_home.mkdir()
        self.state_database = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(self.state_database)
        try:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, thread_source TEXT)"
            )
            connection.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, status TEXT)"
            )
            connection.commit()
        finally:
            connection.close()
        self.config = self.temp / "ntfy-config.json"
        self.config.write_text(
            json.dumps(
                {
                    "server": f"http://127.0.0.1:{self.server.server_port}",
                    "topic": "test-topic",
                    "include_message": True,
                    "include_thread_title": False,
                    "timeout_seconds": 2,
                    "retry_max_seconds": 0.1,
                    "max_attempts": 0,
                    "sent_retention_days": 1,
                    "dead_retention_days": 1,
                    "suppress_subagents": True,
                    "subagent_classification_grace_seconds": 0,
                    "idle_detection_mode": "off",
                }
            ),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "CODEX_NTFY_CONFIG": str(self.config),
                "CODEX_NTFY_STATE_DIR": str(self.state),
                "CODEX_NTFY_NO_SPAWN": "1",
            }
        )
        with self.server.lock:
            self.server.payloads.clear()
            self.server.statuses.clear()
            self.server.redirect_url = None
            self.server.redirect_hits = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def implementations(self) -> list[str]:
        values = ["python"]
        if os.name == "nt" and WINDOWS_POWERSHELL.exists() and os.environ.get("CODEX_NTFY_TEST_PYTHON_ONLY") != "1":
            values.append("powershell")
        return values

    def event(self, *, thread_id: str | None = None, turn_id: str | None = None) -> dict:
        return {
            "type": "agent-turn-complete",
            "thread-id": thread_id or str(uuid.uuid4()),
            "turn-id": turn_id or str(uuid.uuid4()),
            "cwd": "C:\\work\\perfect notifier",
            "last-assistant-message": "Fatto: test concorrente completato.",
        }

    def write_session_meta(self, thread_id: str, *, subagent: bool) -> Path:
        session_dir = self.codex_home / "sessions" / time.strftime("%Y") / time.strftime("%m") / time.strftime("%d")
        session_dir.mkdir(parents=True, exist_ok=True)
        source: object = (
            {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": str(uuid.uuid4()),
                        "depth": 1,
                        "agent_path": "/root/audit",
                    }
                }
            }
            if subagent
            else "vscode"
        )
        path = session_dir / f"rollout-{thread_id}.jsonl"
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": thread_id, "source": source}}) + "\n",
            encoding="utf-8",
        )
        return path

    def configure(self, **updates: object) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update(updates)
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def append_rollout(self, path: Path, event_type: str, *, turn_id: str = "", message: str = "") -> None:
        payload: dict[str, object] = {"type": event_type}
        if turn_id:
            payload["turn_id"] = turn_id
        if event_type == "task_complete":
            payload["last_agent_message"] = message or "Turn completed."
        elif event_type == "user_message":
            payload["message"] = message or "Continue the task."
        elif event_type == "thread_goal_updated":
            payload["goal"] = {"status": message or "active"}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")

    def create_goal_database(self, thread_id: str, status: str) -> Path:
        database = self.codex_home / "goals_1.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
            connection.execute("INSERT INTO thread_goals(thread_id, status) VALUES (?, ?)", (thread_id, status))
            connection.commit()
        finally:
            connection.close()
        return database

    def create_state_database(self, root_id: str, root_rollout: Path, child_id: str, child_rollout: Path) -> Path:
        database = self.state_database
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, thread_source TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS thread_spawn_edges (parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, status TEXT)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO threads(id, rollout_path, source, thread_source) VALUES (?, ?, 'vscode', 'user')",
                (root_id, str(root_rollout)),
            )
            connection.execute(
                "INSERT OR REPLACE INTO threads(id, rollout_path, source, thread_source) VALUES (?, ?, ?, 'subagent')",
                (child_id, str(child_rollout), json.dumps({"subagent": {}})),
            )
            connection.execute(
                "INSERT OR REPLACE INTO thread_spawn_edges(parent_thread_id, child_thread_id, status) VALUES (?, ?, 'open')",
                (root_id, child_id),
            )
            connection.commit()
        finally:
            connection.close()
        return database

    def start_worker(self, implementation: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self.worker_command(implementation),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def assert_worker_ok(self, process: subprocess.Popen[str], *, timeout: float = 15) -> None:
        stdout, stderr = process.communicate(timeout=timeout)
        self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")

    def wait_for_payloads(self, count: int, *, timeout: float = 10) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.server.lock:
                payloads = list(self.server.payloads)
            if len(payloads) >= count:
                return payloads
            time.sleep(0.05)
        with self.server.lock:
            return list(self.server.payloads)

    def hook_command(self, implementation: str, event: dict, *, origin: str = "test-host") -> list[str]:
        raw = json.dumps(event, ensure_ascii=False)
        if implementation == "powershell":
            return [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(POWERSHELL_NOTIFIER),
                "-NoSpawn",
                "-Origin",
                origin,
                raw,
            ]
        return [sys.executable, str(PYTHON_NOTIFIER), "--no-spawn", "--origin", origin, raw]

    def modern_hook_command(self, implementation: str, *, origin: str = "test-host") -> list[str]:
        if implementation == "powershell":
            return [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(POWERSHELL_NOTIFIER),
                "-NoSpawn",
                "-HookEvent",
                "-ReadStdin",
                "-Origin",
                origin,
            ]
        return [
            sys.executable,
            str(PYTHON_NOTIFIER),
            "--no-spawn",
            "--hook-event",
            "--read-stdin",
            "--origin",
            origin,
        ]

    def worker_command(self, implementation: str) -> list[str]:
        if implementation == "powershell":
            return [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(POWERSHELL_NOTIFIER),
                "-Worker",
                "-PollSeconds",
                "1",
                "-RetryBaseSeconds",
                "0.05",
            ]
        return [
            sys.executable,
            str(PYTHON_NOTIFIER),
            "--worker",
            "--poll-seconds",
            "0.1",
            "--retry-base-seconds",
            "0.05",
        ]

    def continuous_worker_command(self, implementation: str) -> list[str]:
        command = self.worker_command(implementation)
        if implementation == "powershell":
            command[command.index("-Worker")] = "-Continuous"
        else:
            command[command.index("--worker")] = "--continuous"
        return command

    def run_ok(self, command: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        return result

    def test_delivery_and_deduplication(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event()
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.hook_command(implementation, event))
                outbox = list((self.state / "outbox").glob("*.json"))
                self.assertEqual(len(outbox), 1)
                self.run_ok(self.worker_command(implementation))
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payloads = list(self.server.payloads)
                    self.server.payloads.clear()
                self.assertEqual(len(payloads), 1)
                payload = payloads[0]
                self.assertEqual(payload["topic"], "test-topic")
                self.assertTrue(payload["sequence_id"].startswith("codex-"))
                self.assertIn("perfect notifier", payload["title"])
                self.assertIn("test-host", payload["message"])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_idle_gate_coalesces_auto_continuations(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                first_turn = "00000000-0000-7000-8000-000000000001"
                final_turn = "00000000-0000-7000-8000-000000000002"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=first_turn)
                self.append_rollout(rollout, "user_message", message="Start the task")
                self.append_rollout(rollout, "task_complete", turn_id=first_turn, message="Intermediate")
                self.append_rollout(rollout, "task_started", turn_id=final_turn)
                stale_event = self.event(thread_id=thread_id, turn_id=first_turn)
                stale_event["last-assistant-message"] = "INTERMEDIATE"
                self.run_ok(self.hook_command(implementation, stale_event))
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)

                process = self.start_worker(implementation)
                try:
                    time.sleep(0.35)
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    self.append_rollout(rollout, "user_message", message="Automatic continuation")
                    self.append_rollout(rollout, "task_complete", turn_id=final_turn, message="Final")
                    time.sleep(0.02)
                    self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id, turn_id=final_turn)))
                    self.assert_worker_ok(process)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)

                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                suppressed = [json.loads(path.read_text(encoding="utf-8-sig")) for path in (self.state / "suppressed").glob("*.json")]
                self.assertTrue(any(receipt.get("reason") == "superseded" for receipt in suppressed))
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_lost_newer_hook_is_recovered_without_an_intermediate_notification(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                first_turn = "00000000-0000-7000-8000-000000000011"
                final_turn = "00000000-0000-7000-8000-000000000012"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=first_turn)
                self.append_rollout(rollout, "user_message", message="Start")
                self.append_rollout(rollout, "task_complete", turn_id=first_turn, message="INTERMEDIATE")
                self.append_rollout(rollout, "task_started", turn_id=final_turn)
                self.append_rollout(rollout, "user_message", message="Automatic continuation")
                self.append_rollout(rollout, "task_complete", turn_id=final_turn, message="FINAL")

                # Only the stale live hook arrives. The idle probe must recover
                # the newer completion itself; this test has no watcher scan.
                stale_event = self.event(thread_id=thread_id, turn_id=first_turn)
                stale_event["last-assistant-message"] = "INTERMEDIATE"
                self.run_ok(self.hook_command(implementation, stale_event))
                self.run_ok(self.worker_command(implementation), timeout=20)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertIn("FINAL", payloads[0]["message"])
                self.assertNotIn("INTERMEDIATE", payloads[0]["message"])
                suppressed = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertTrue(any(receipt.get("reason") == "superseded" for receipt in suppressed))
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_watcher_backfill_cannot_replace_a_newer_live_hook(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=True,
            watch_scan_seconds=0.1,
            watch_initial_replay_seconds=60,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                old_turn = "00000000-0000-7000-8000-000000000021"
                new_turn = "00000000-0000-7000-8000-000000000022"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=old_turn)
                self.append_rollout(rollout, "user_message", message="Old")
                self.append_rollout(rollout, "task_complete", turn_id=old_turn, message="STALE")
                self.append_rollout(rollout, "task_started", turn_id=new_turn)
                self.append_rollout(rollout, "user_message", message="New")
                self.append_rollout(rollout, "task_complete", turn_id=new_turn, message="NEWEST")
                newest_event = self.event(thread_id=thread_id, turn_id=new_turn)
                newest_event["last-assistant-message"] = "NEWEST"
                self.run_ok(self.hook_command(implementation, newest_event))

                process = subprocess.Popen(
                    self.continuous_worker_command(implementation),
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    payloads = self.wait_for_payloads(1)
                    self.assertEqual(len(payloads), 1)
                    self.assertIn("NEWEST", payloads[0]["message"])
                    self.assertNotIn("STALE", payloads[0]["message"])
                    time.sleep(0.2)
                    with self.server.lock:
                        self.assertEqual(len(self.server.payloads), 1)
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_mode_off_preserves_two_turns_from_the_same_thread(self) -> None:
        self.configure(idle_detection_mode="off")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                first = self.event(thread_id=thread_id, turn_id="00000000-0000-7000-8000-000000000031")
                second = self.event(thread_id=thread_id, turn_id="00000000-0000-7000-8000-000000000032")
                first["last-assistant-message"] = "FIRST"
                second["last-assistant-message"] = "SECOND"
                self.run_ok(self.hook_command(implementation, first))
                self.run_ok(self.hook_command(implementation, second))
                self.run_ok(self.worker_command(implementation))
                payloads = self.wait_for_payloads(2)
                self.assertEqual(len(payloads), 2)
                self.assertEqual({payload["message"].splitlines()[0] for payload in payloads}, {"FIRST", "SECOND"})
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_outbox_epoch_is_not_coalesced_with_a_later_pending_turn(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                first_turn = "00000000-0000-7000-8000-000000000033"
                second_turn = "00000000-0000-7000-8000-000000000034"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=first_turn)
                self.append_rollout(rollout, "user_message", message="First request")
                self.append_rollout(rollout, "task_complete", turn_id=first_turn, message="FIRST EPOCH")

                self.configure(idle_detection_mode="off")
                first = self.event(thread_id=thread_id, turn_id=first_turn)
                first["last-assistant-message"] = "FIRST EPOCH"
                self.run_ok(self.hook_command(implementation, first))
                self.assertEqual(len(list((self.state / "outbox").glob("*.json"))), 1)

                self.append_rollout(rollout, "task_started", turn_id=second_turn)
                self.append_rollout(rollout, "user_message", message="Second request")
                self.append_rollout(rollout, "task_complete", turn_id=second_turn, message="SECOND EPOCH")
                self.configure(
                    idle_detection_mode="strict",
                    idle_grace_seconds=0,
                    goal_poll_seconds=0.05,
                    suppress_technical_turns=True,
                )
                second = self.event(thread_id=thread_id, turn_id=second_turn)
                second["last-assistant-message"] = "SECOND EPOCH"
                self.run_ok(self.hook_command(implementation, second))
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)

                self.run_ok(self.worker_command(implementation), timeout=20)
                payloads = self.wait_for_payloads(2)
                self.assertEqual(len(payloads), 2)
                messages = {payload["message"].splitlines()[0] for payload in payloads}
                self.assertEqual(messages, {"FIRST EPOCH", "SECOND EPOCH"})
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_strict_mode_suppresses_a_technical_turn(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000041"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                # No user_message: this models review/compact/tool-only work.
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="TECHNICAL")
                event = self.event(thread_id=thread_id, turn_id=turn_id)
                event["last-assistant-message"] = "TECHNICAL"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=20)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                receipts = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertTrue(any(receipt.get("reason") == "technical-turn" for receipt in receipts))
                shutil.rmtree(self.state, ignore_errors=True)

    def test_modern_stop_upgrades_an_earlier_technical_receipt(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000043"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="STOP AUTHORITATIVE")
                legacy = self.event(thread_id=thread_id, turn_id=turn_id)
                legacy["last-assistant-message"] = "legacy technical"
                self.run_ok(self.hook_command(implementation, legacy))
                self.run_ok(self.worker_command(implementation), timeout=20)
                receipts = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertTrue(any(receipt.get("reason") == "technical-turn" for receipt in receipts))
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])

                stop = {
                    "hook_event_name": "Stop",
                    "session_id": thread_id,
                    "turn_id": turn_id,
                    "cwd": "C:\\work\\perfect notifier",
                    "last_assistant_message": "STOP AUTHORITATIVE",
                    "stop_hook_active": False,
                }
                result = subprocess.run(
                    self.modern_hook_command(implementation),
                    input=json.dumps(stop),
                    env=self.env,
                    text=True,
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "{}")
                self.run_ok(self.worker_command(implementation), timeout=20)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertIn("STOP AUTHORITATIVE", payloads[0]["message"])
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_python_stale_suppression_cannot_remove_modern_stop(self) -> None:
        spec = importlib.util.spec_from_file_location("codex_ntfy_notifier_under_test", PYTHON_NOTIFIER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        original_environment = os.environ.copy()
        try:
            os.environ.update(self.env)
            runtime = module.Runtime()
            runtime.ensure()
            key = "a" * 64
            stale = {
                "key": key,
                "thread_id": str(uuid.uuid4()),
                "turn_id": str(uuid.uuid4()),
                "origin": "Codex",
                "source_event": "legacy-notify",
            }
            authoritative = {**stale, "source_event": "Stop"}
            module.atomic_write_json(runtime.pending / f"{key}.json", authoritative)

            module.write_suppressed_receipt(runtime, stale, "subagent")

            kept = module.read_json(runtime.pending / f"{key}.json")
            self.assertEqual(kept["source_event"], "Stop")
            self.assertFalse((runtime.suppressed / f"{key}.json").exists())
        finally:
            os.environ.clear()
            os.environ.update(original_environment)

    def test_goal_awareness_can_be_disabled_explicitly(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            goal_aware=False,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000042"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="Manual override")
                self.append_rollout(rollout, "thread_goal_updated", message="active")
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="OVERRIDE")
                database = self.create_goal_database(thread_id, "active")
                event = self.event(thread_id=thread_id, turn_id=turn_id)
                event["last-assistant-message"] = "OVERRIDE"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=20)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertIn("OVERRIDE", payloads[0]["message"])
                shutil.rmtree(self.state, ignore_errors=True)
                database.unlink(missing_ok=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_invalid_idle_detection_mode_is_rejected(self) -> None:
        self.configure(idle_detection_mode="maybe")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                command = (
                    [
                        str(WINDOWS_POWERSHELL),
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(POWERSHELL_NOTIFIER),
                        "-Doctor",
                    ]
                    if implementation == "powershell"
                    else [sys.executable, str(PYTHON_NOTIFIER), "--doctor"]
                )
                result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)

    def test_recovered_completion_respects_message_opt_out(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        secret = "PRIVATE-RECOVERED-MESSAGE-MUST-NOT-PERSIST"
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                old_turn = "00000000-0000-7000-8000-000000000051"
                new_turn = "00000000-0000-7000-8000-000000000052"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=old_turn)
                self.append_rollout(rollout, "task_complete", turn_id=old_turn, message="OLD")
                self.append_rollout(rollout, "task_started", turn_id=new_turn)
                self.append_rollout(rollout, "task_complete", turn_id=new_turn, message=secret)
                event = self.event(thread_id=thread_id, turn_id=old_turn)
                event["last-assistant-message"] = "OLD"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=20)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertNotIn(secret, payloads[0]["message"])
                persisted = "\n".join(
                    path.read_text(encoding="utf-8-sig", errors="replace")
                    for path in self.state.rglob("*.json")
                )
                self.assertNotIn(secret, persisted)
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_new_recovered_content_honors_a_later_message_opt_out(self) -> None:
        self.configure(
            include_message=True,
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=True,
        )
        secret = "LATER-PRIVATE-CONTENT-MUST-NOT-PERSIST"
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                old_turn = "00000000-0000-7000-8000-000000000053"
                new_turn = "00000000-0000-7000-8000-000000000054"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=old_turn)
                self.append_rollout(rollout, "user_message", message="Old")
                self.append_rollout(rollout, "task_complete", turn_id=old_turn, message="OLD ALLOWED")
                old_event = self.event(thread_id=thread_id, turn_id=old_turn)
                old_event["last-assistant-message"] = "OLD ALLOWED"
                self.run_ok(self.hook_command(implementation, old_event))

                self.configure(include_message=False)
                self.append_rollout(rollout, "task_started", turn_id=new_turn)
                self.append_rollout(rollout, "user_message", message="New")
                self.append_rollout(rollout, "task_complete", turn_id=new_turn, message=secret)
                self.run_ok(self.worker_command(implementation), timeout=20)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertNotIn(secret, payloads[0]["message"])
                persisted = "\n".join(
                    path.read_text(encoding="utf-8-sig", errors="replace")
                    for path in self.state.rglob("*.json")
                )
                self.assertNotIn(secret, persisted)
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_large_partial_jsonl_completion_is_reassembled(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        secret = "LARGE-PRIVATE-" + ("x" * 140_000)
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000071"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                terminal_line = (
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "turn_id": turn_id,
                                "last_agent_message": secret,
                            },
                        }
                    )
                    + "\n"
                ).encode()
                split = len(terminal_line) // 2
                with rollout.open("ab") as handle:
                    handle.write(terminal_line[:split])

                event = self.event(thread_id=thread_id, turn_id=turn_id)
                # Keep the synthetic hook small: real oversized legacy argv is
                # exactly why rollout recovery exists.
                event["last-assistant-message"] = "hook-candidate"
                self.run_ok(self.hook_command(implementation, event))
                process = self.start_worker(implementation)
                try:
                    deadline = time.monotonic() + 8
                    observed_wait = False
                    while time.monotonic() < deadline and not observed_wait:
                        for pending_path in (self.state / "pending").glob("*.json"):
                            try:
                                pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                            except (OSError, json.JSONDecodeError):
                                continue
                            reason = str(pending_record.get("idle_reason") or pending_record.get("gate_reason") or "")
                            observed_wait = bool(reason)
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertTrue(observed_wait, "worker did not inspect the incomplete JSONL record")
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    with rollout.open("ab") as handle:
                        handle.write(terminal_line[split:])
                    self.assert_worker_ok(process, timeout=20)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                self.assertEqual(len(self.wait_for_payloads(1)), 1)
                persisted = "\n".join(
                    path.read_text(encoding="utf-8-sig", errors="replace")
                    for path in self.state.rglob("*.json")
                )
                self.assertNotIn("LARGE-PRIVATE-", persisted)
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_active_goal_waits_for_terminal_status(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000003"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="Finish the goal")
                self.append_rollout(rollout, "thread_goal_updated", message="active")
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="Goal step")
                database = self.create_goal_database(thread_id, "active")
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id, turn_id=turn_id)))
                process = self.start_worker(implementation)
                try:
                    deadline = time.monotonic() + 8
                    observed_reason = ""
                    while time.monotonic() < deadline and observed_reason != "goal-active":
                        for pending_path in (self.state / "pending").glob("*.json"):
                            try:
                                pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                            except (OSError, json.JSONDecodeError):
                                continue
                            observed_reason = str(
                                pending_record.get("idle_reason") or pending_record.get("gate_reason") or ""
                            )
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertEqual(observed_reason, "goal-active", "worker never observed the active goal")
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    connection = sqlite3.connect(database)
                    try:
                        # Codex removes a terminal goal row. The rollout can
                        # still contain a stale `active` update, so readable +
                        # absent must be treated as terminal/idle.
                        connection.execute("DELETE FROM thread_goals WHERE thread_id = ?", (thread_id,))
                        connection.commit()
                    finally:
                        connection.close()
                    self.assert_worker_ok(process)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                self.assertEqual(len(self.wait_for_payloads(1)), 1)
                shutil.rmtree(self.state, ignore_errors=True)
                database.unlink(missing_ok=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_root_waits_for_running_descendant(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            subagent_orphan_seconds=60,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                root_id = str(uuid.uuid4())
                child_id = str(uuid.uuid4())
                root_turn = "00000000-0000-7000-8000-000000000005"
                child_turn = "00000000-0000-7000-8000-000000000006"
                root_rollout = self.write_session_meta(root_id, subagent=False)
                child_rollout = self.write_session_meta(child_id, subagent=True)
                self.append_rollout(root_rollout, "task_started", turn_id=root_turn)
                self.append_rollout(root_rollout, "user_message", message="Wait for every child")
                self.append_rollout(root_rollout, "task_complete", turn_id=root_turn, message="Root candidate")
                self.append_rollout(child_rollout, "task_started", turn_id=child_turn)
                database = self.create_state_database(root_id, root_rollout, child_id, child_rollout)
                self.run_ok(self.hook_command(implementation, self.event(thread_id=root_id, turn_id=root_turn)))
                process = self.start_worker(implementation)
                try:
                    deadline = time.monotonic() + 8
                    observed_reason = ""
                    while time.monotonic() < deadline and "subagent" not in observed_reason:
                        for pending_path in (self.state / "pending").glob("*.json"):
                            try:
                                pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                            except (OSError, json.JSONDecodeError):
                                continue
                            observed_reason = str(
                                pending_record.get("idle_reason") or pending_record.get("gate_reason") or ""
                            )
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertIn("subagent", observed_reason, "worker never observed the active descendant")
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    self.append_rollout(child_rollout, "task_complete", turn_id=child_turn, message="Child done")
                    self.assert_worker_ok(process)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                self.assertEqual(len(self.wait_for_payloads(1)), 1)
                shutil.rmtree(self.state, ignore_errors=True)
                database.unlink(missing_ok=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_root_waits_for_an_open_descendant_rollout_to_appear(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            subagent_orphan_seconds=60,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                root_id = str(uuid.uuid4())
                child_id = str(uuid.uuid4())
                root_turn = "00000000-0000-7000-8000-000000000061"
                child_turn = "00000000-0000-7000-8000-000000000062"
                root_rollout = self.write_session_meta(root_id, subagent=False)
                child_rollout = self.write_session_meta(child_id, subagent=True)
                self.append_rollout(root_rollout, "task_started", turn_id=root_turn)
                self.append_rollout(root_rollout, "user_message", message="Wait for child creation")
                self.append_rollout(root_rollout, "task_complete", turn_id=root_turn, message="Root")
                database = self.create_state_database(root_id, root_rollout, child_id, child_rollout)
                child_rollout.unlink()

                event = self.event(thread_id=root_id, turn_id=root_turn)
                event["last-assistant-message"] = "Root"
                self.run_ok(self.hook_command(implementation, event))
                process = self.start_worker(implementation)
                try:
                    # Hosted Windows runners can be briefly CPU-starved while
                    # launching PowerShell/Python processes. Wait for an
                    # observed gate decision, not a fixed startup assumption.
                    deadline = time.monotonic() + 30
                    observed_reason = ""
                    while time.monotonic() < deadline and "subagent" not in observed_reason:
                        for pending_path in (self.state / "pending").glob("*.json"):
                            try:
                                pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                            except (OSError, json.JSONDecodeError):
                                continue
                            observed_reason = str(
                                pending_record.get("idle_reason") or pending_record.get("gate_reason") or ""
                            )
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            self.fail(
                                "worker exited before observing the missing child rollout: "
                                f"returncode={process.returncode}\nstdout={stdout}\nstderr={stderr}"
                            )
                        time.sleep(0.05)
                    self.assertIn("subagent", observed_reason, "missing child rollout did not hold the root")
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])

                    recreated = self.write_session_meta(child_id, subagent=True)
                    self.assertEqual(recreated, child_rollout)
                    self.append_rollout(child_rollout, "task_started", turn_id=child_turn)
                    self.append_rollout(child_rollout, "task_complete", turn_id=child_turn, message="Child done")
                    self.assert_worker_ok(process, timeout=30)
                    self.assertEqual(len(self.wait_for_payloads(1)), 1)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                    shutil.rmtree(self.state, ignore_errors=True)
                    shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                    database.unlink(missing_ok=True)
                    with self.server.lock:
                        self.server.payloads.clear()

    def test_strict_mode_waits_when_the_spawn_database_disappears(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000063"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="Database race")
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="DB RESTORED")
                event = self.event(thread_id=thread_id, turn_id=turn_id)
                event["last-assistant-message"] = "DB RESTORED"
                self.run_ok(self.hook_command(implementation, event))
                hidden_database = self.state_database.with_suffix(".hidden")
                self.state_database.replace(hidden_database)
                process = self.start_worker(implementation)
                try:
                    deadline = time.monotonic() + 8
                    observed_reason = ""
                    while time.monotonic() < deadline and not observed_reason:
                        for pending_path in (self.state / "pending").glob("*.json"):
                            try:
                                pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                            except (OSError, json.JSONDecodeError):
                                continue
                            observed_reason = str(
                                pending_record.get("idle_reason") or pending_record.get("gate_reason") or ""
                            )
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertTrue(observed_reason, "missing spawn DB was not observed")
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    hidden_database.replace(self.state_database)
                    self.assert_worker_ok(process, timeout=20)
                finally:
                    if hidden_database.exists() and not self.state_database.exists():
                        hidden_database.replace(self.state_database)
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                self.assertEqual(len(self.wait_for_payloads(1)), 1)
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_modern_stop_hook_is_a_pending_root_candidate(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000004"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="Hook final")
                hook = {
                    "hook_event_name": "Stop",
                    "session_id": thread_id,
                    "turn_id": turn_id,
                    "cwd": "C:\\work\\perfect notifier",
                    "last_assistant_message": "Hook final",
                    "stop_hook_active": False,
                }
                result = subprocess.run(
                    self.modern_hook_command(implementation),
                    input=json.dumps(hook),
                    env=self.env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(result.stdout.strip(), "{}")
                pending = list((self.state / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                self.assertEqual(json.loads(pending[0].read_text(encoding="utf-8-sig"))["session_classification"], "root")
                self.run_ok(self.worker_command(implementation))
                self.assertEqual(len(self.wait_for_payloads(1)), 1)
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_modern_stop_for_a_descendant_is_suppressed(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000005"
                rollout = self.write_session_meta(thread_id, subagent=True)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="Child result")
                hook = {
                    "hook_event_name": "Stop",
                    "session_id": thread_id,
                    "turn_id": turn_id,
                    "cwd": "C:\\work\\perfect notifier",
                    "last_assistant_message": "Child result",
                    "stop_hook_active": False,
                }
                result = subprocess.run(
                    self.modern_hook_command(implementation),
                    input=json.dumps(hook),
                    env=self.env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(result.stdout.strip(), "{}")
                self.assertFalse(list((self.state / "pending").glob("*.json")))
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                receipts = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertEqual([receipt.get("reason") for receipt in receipts], ["subagent"])
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_unknown_modern_stop_is_reclassified_when_child_rollout_appears(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000006"
                hook = {
                    "hook_event_name": "Stop",
                    "session_id": thread_id,
                    "turn_id": turn_id,
                    "cwd": "C:\\work\\perfect notifier",
                    "last_assistant_message": "Late child result",
                    "stop_hook_active": False,
                }
                result = subprocess.run(
                    self.modern_hook_command(implementation),
                    input=json.dumps(hook),
                    env=self.env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(result.stdout.strip(), "{}")
                pending = list((self.state / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                self.assertEqual(
                    json.loads(pending[0].read_text(encoding="utf-8-sig"))["session_classification"],
                    "unknown",
                )

                rollout = self.write_session_meta(thread_id, subagent=True)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="Late child result")
                self.run_ok(self.worker_command(implementation), timeout=20)

                self.assertFalse(list((self.state / "pending").glob("*.json")))
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                receipts = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertEqual([receipt.get("reason") for receipt in receipts], ["subagent"])
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows WSL bridge test")
    def test_wsl_bridge_mode_propagates_windows_hook_failures(self) -> None:
        self.config.write_text("{invalid-json", encoding="utf-8")
        hook = {
            "hook_event_name": "Stop",
            "session_id": str(uuid.uuid4()),
            "turn_id": "00000000-0000-7000-8000-000000000091",
            "cwd": "C:\\work\\bridge",
            "last_assistant_message": "Bridge fallback",
            "stop_hook_active": False,
        }
        direct = subprocess.run(
            self.modern_hook_command("powershell"),
            input=json.dumps(hook),
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(direct.returncode, 0, direct.stdout + direct.stderr)
        self.assertEqual(direct.stdout.strip(), "{}")

        bridge_command = self.modern_hook_command("powershell")
        bridge_command.insert(bridge_command.index("-HookEvent") + 1, "-BridgeFallback")
        bridged = subprocess.run(
            bridge_command,
            input=json.dumps(hook),
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertNotEqual(bridged.returncode, 0)
        self.assertEqual(bridged.stdout.strip(), "")

    def test_continuous_worker_recovers_a_lost_hook_from_rollout(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=True,
            watch_scan_seconds=0.1,
            watch_initial_replay_seconds=60,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000007"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="The live hook will be lost")
                process = subprocess.Popen(
                    self.continuous_worker_command(implementation),
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not list((self.state / "watch").glob("*.json")):
                        time.sleep(0.05)
                    self.assertTrue(list((self.state / "watch").glob("*.json")))
                    self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="Recovered final")
                    self.assertEqual(len(self.wait_for_payloads(1)), 1)
                    time.sleep(0.2)
                    with self.server.lock:
                        self.assertEqual(len(self.server.payloads), 1)
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_watcher_does_not_advance_past_completion_when_session_metadata_is_temporarily_unreadable(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=True,
            watch_scan_seconds=0.1,
            watch_initial_replay_seconds=60,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000008"
                session_dir = self.codex_home / "sessions" / time.strftime("%Y") / time.strftime("%m") / time.strftime("%d")
                session_dir.mkdir(parents=True, exist_ok=True)
                rollout = session_dir / f"rollout-{thread_id}.jsonl"
                rollout.write_text("{temporarily-unreadable-session-meta}\n", encoding="utf-8")
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="Recover after metadata retry")
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="METADATA RECOVERED")
                lifecycle_tail = "\n".join(rollout.read_text(encoding="utf-8").splitlines()[1:]) + "\n"

                process = subprocess.Popen(
                    self.continuous_worker_command(implementation),
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    time.sleep(0.5)
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    cursor_states = [
                        json.loads(path.read_text(encoding="utf-8-sig"))
                        for path in (self.state / "watch").glob("*.json")
                    ]
                    self.assertTrue(all(int(state.get("offset", 0) or 0) == 0 for state in cursor_states))

                    metadata = json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"id": thread_id, "source": "vscode", "cwd": "/work/metadata-retry"},
                        }
                    )
                    rollout.write_text(metadata + "\n" + lifecycle_tail, encoding="utf-8")
                    payloads = self.wait_for_payloads(1, timeout=15)
                    self.assertEqual(len(payloads), 1)
                    self.assertIn("METADATA RECOVERED", payloads[0]["message"])
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_watcher_discovers_recent_old_date_and_archived_rollouts(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=True,
            watch_scan_seconds=0.1,
            watch_discovery_seconds=5,
            watch_initial_replay_seconds=60,
            suppress_technical_turns=True,
        )
        locations = (
            Path("sessions") / "2001" / "01" / "01",
            Path("archived_sessions"),
        )
        for implementation in self.implementations():
            for location in locations:
                with self.subTest(implementation=implementation, location=str(location)):
                    thread_id = str(uuid.uuid4())
                    turn_id = "00000000-0001-7000-8000-" + ("1" if location.parts[0] == "sessions" else "2") * 12
                    directory = self.codex_home / location
                    directory.mkdir(parents=True, exist_ok=True)
                    rollout = directory / f"rollout-{thread_id}.jsonl"
                    rollout.write_text(
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {"id": thread_id, "source": "vscode", "cwd": "/work/discovery"},
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    self.append_rollout(rollout, "task_started", turn_id=turn_id)
                    self.append_rollout(rollout, "user_message", message="Old session, fresh work")
                    self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="DISCOVERED")
                    process = subprocess.Popen(
                        self.continuous_worker_command(implementation),
                        env=self.env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        payloads = self.wait_for_payloads(1, timeout=15)
                        self.assertEqual(len(payloads), 1)
                        self.assertIn("DISCOVERED", payloads[0]["message"])
                    finally:
                        process.terminate()
                        stdout, stderr = process.communicate(timeout=10)
                        self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")
                    shutil.rmtree(self.state, ignore_errors=True)
                    shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                    shutil.rmtree(self.codex_home / "archived_sessions", ignore_errors=True)
                    with self.server.lock:
                        self.server.payloads.clear()

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows multi-root watcher test")
    def test_windows_watcher_recovers_a_lost_wsl_root_event(self) -> None:
        secondary_home = self.temp / "wsl-codex-home"
        secondary_home.mkdir()
        secondary_database = sqlite3.connect(secondary_home / "state_5.sqlite")
        try:
            secondary_database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, thread_source TEXT)"
            )
            secondary_database.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, status TEXT)"
            )
            secondary_database.commit()
        finally:
            secondary_database.close()
        session_dir = secondary_home / "sessions" / time.strftime("%Y") / time.strftime("%m") / time.strftime("%d")
        session_dir.mkdir(parents=True)
        thread_id = str(uuid.uuid4())
        turn_id = "00000000-0000-7000-8000-000000000081"
        rollout = session_dir / f"rollout-{thread_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": thread_id, "source": "vscode", "cwd": "/home/test/wsl-project"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.append_rollout(rollout, "task_started", turn_id=turn_id)
        self.append_rollout(rollout, "user_message", message="WSL task")
        self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="WSL RECOVERED")
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=True,
            watch_scan_seconds=0.1,
            watch_initial_replay_seconds=60,
            watch_roots=[{"path": str(secondary_home), "origin": "WSL:test"}],
            suppress_technical_turns=True,
        )
        process = subprocess.Popen(
            self.continuous_worker_command("powershell"),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            payloads = self.wait_for_payloads(1, timeout=15)
            self.assertEqual(len(payloads), 1)
            self.assertIn("WSL RECOVERED", payloads[0]["message"])
            self.assertIn("Source: WSL:test", payloads[0]["message"])
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")

    def test_python_kick_worker_recovers_a_stranded_outbox(self) -> None:
        event = self.event()
        self.run_ok(self.hook_command("python", event))
        self.assertEqual(len(list((self.state / "outbox").glob("*.json"))), 1)
        worker_env = self.env.copy()
        worker_env.pop("CODEX_NTFY_NO_SPAWN", None)
        kick = subprocess.run(
            [sys.executable, str(PYTHON_NOTIFIER), "--kick-worker", "--poll-seconds", "0.1"],
            env=worker_env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(kick.returncode, 0, kick.stdout + kick.stderr)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and list((self.state / "outbox").glob("*.json")):
            time.sleep(0.1)
        self.assertFalse(list((self.state / "outbox").glob("*.json")))
        self.assertEqual(len(list((self.state / "sent").glob("*.json"))), 1)

    def test_retry_reuses_sequence_id(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                with self.server.lock:
                    self.server.statuses[:] = [503, 200]
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation), timeout=30)
                with self.server.lock:
                    payloads = list(self.server.payloads)
                    self.server.payloads.clear()
                    self.server.statuses.clear()
                self.assertEqual(len(payloads), 2)
                self.assertEqual(payloads[0]["sequence_id"], payloads[1]["sequence_id"])
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                self.assertEqual(len(list((self.state / "sent").glob("*.json"))), 1)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_permanent_http_error_goes_to_dead_letter(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                with self.server.lock:
                    self.server.statuses[:] = [400]
                event = self.event()
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=15)
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    self.server.payloads.clear()
                    self.server.statuses.clear()
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                self.assertEqual(len(list((self.state / "dead").glob("*.json"))), 1)
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=15)
                with self.server.lock:
                    self.assertFalse(self.server.payloads)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_redirect_is_rejected_without_following_it(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["token"] = "test-token-never-forward"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        redirect_target = RecordingServer()
        target_thread = threading.Thread(target=redirect_target.serve_forever, daemon=True)
        target_thread.start()
        try:
            for implementation in self.implementations():
                with self.subTest(implementation=implementation):
                    with self.server.lock:
                        self.server.statuses[:] = [302]
                        self.server.redirect_url = f"http://127.0.0.1:{redirect_target.server_port}/redirected"
                    self.run_ok(self.hook_command(implementation, self.event()))
                    self.run_ok(self.worker_command(implementation), timeout=15)
                    with self.server.lock:
                        self.assertEqual(len(self.server.payloads), 1)
                        self.server.payloads.clear()
                        self.server.statuses.clear()
                    with redirect_target.lock:
                        self.assertFalse(redirect_target.payloads)
                        self.assertEqual(redirect_target.redirect_hits, 0)
                    self.assertFalse(list((self.state / "outbox").glob("*.json")))
                    self.assertEqual(len(list((self.state / "dead").glob("*.json"))), 1)
                    shutil.rmtree(self.state, ignore_errors=True)
        finally:
            redirect_target.shutdown()
            redirect_target.server_close()
            target_thread.join(timeout=5)

    def test_runtime_retention_cleans_old_receipts_and_dead_letters(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                for name in ("outbox", "sent", "suppressed", "dead"):
                    (self.state / name).mkdir(parents=True, exist_ok=True)
                old_paths = []
                fresh_paths = []
                for name in ("sent", "suppressed", "dead"):
                    old = self.state / name / "old.json"
                    fresh = self.state / name / "fresh.json"
                    old.write_text("{}", encoding="utf-8")
                    fresh.write_text("{}", encoding="utf-8")
                    old_paths.append(old)
                    fresh_paths.append(fresh)
                old_time = time.time() - 3 * 86400
                for path in old_paths:
                    os.utime(path, (old_time, old_time))

                self.run_ok(self.worker_command(implementation), timeout=15)
                self.assertTrue(all(not path.exists() for path in old_paths))
                self.assertTrue(all(path.exists() for path in fresh_paths))
                shutil.rmtree(self.state, ignore_errors=True)

    def test_credentials_require_https_outside_loopback(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"server": "http://192.0.2.1", "token": "test-token", "max_attempts": 1})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation), timeout=15)
                with self.server.lock:
                    self.assertFalse(self.server.payloads)
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                dead = list((self.state / "dead").glob("*.json"))
                self.assertEqual(len(dead), 1)
                self.assertIn("insecure", dead[0].read_text(encoding="utf-8-sig").lower())
                shutil.rmtree(self.state, ignore_errors=True)

    def test_server_url_credentials_are_rejected_and_redacted_by_doctor(self) -> None:
        secret_url = "http://url-user:url-password@example.invalid/private?token=query-secret"
        self.configure(server=secret_url, max_attempts=1, idle_detection_mode="off")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                doctor_command = (
                    [
                        str(WINDOWS_POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(POWERSHELL_NOTIFIER), "-Doctor",
                    ]
                    if implementation == "powershell"
                    else [sys.executable, str(PYTHON_NOTIFIER), "--doctor"]
                )
                doctor = self.run_ok(doctor_command)
                combined = doctor.stdout + doctor.stderr
                self.assertNotIn("url-user", combined)
                self.assertNotIn("url-password", combined)
                self.assertNotIn("query-secret", combined)
                self.assertEqual(json.loads(doctor.stdout)["server"], "http://example.invalid")

                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation), timeout=20)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                dead = list((self.state / "dead").glob("*.json"))
                self.assertEqual(len(dead), 1)
                persisted = dead[0].read_text(encoding="utf-8-sig", errors="replace")
                self.assertNotIn("url-password", persisted)
                self.assertNotIn("query-secret", persisted)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_incomplete_basic_auth_never_publishes_anonymously(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"username": "publisher", "password": "", "max_attempts": 1})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation), timeout=15)
                with self.server.lock:
                    self.assertFalse(self.server.payloads)
                dead = list((self.state / "dead").glob("*.json"))
                self.assertEqual(len(dead), 1)
                self.assertIn("username and password", dead[0].read_text(encoding="utf-8-sig").lower())
                shutil.rmtree(self.state, ignore_errors=True)

    def test_token_auth_takes_precedence_over_stale_basic_fields(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"token": "test-token", "username": "stale-user", "password": ""})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation), timeout=15)
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    self.server.payloads.clear()
                shutil.rmtree(self.state, ignore_errors=True)

    def test_utf8_bom_private_config_is_supported(self) -> None:
        config_text = self.config.read_text(encoding="utf-8")
        self.config.write_text(config_text, encoding="utf-8-sig")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation), timeout=15)
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    self.server.payloads.clear()
                shutil.rmtree(self.state, ignore_errors=True)

    def test_malformed_payload_is_ignored(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                if implementation == "powershell":
                    command = [
                        str(WINDOWS_POWERSHELL),
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(POWERSHELL_NOTIFIER),
                        "-NoSpawn",
                        "{not-json",
                    ]
                else:
                    command = [sys.executable, str(PYTHON_NOTIFIER), "--no-spawn", "{not-json"]
                self.run_ok(command)
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                shutil.rmtree(self.state, ignore_errors=True)

    def test_poison_record_is_dead_lettered_without_blocking_queue(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                outbox = self.state / "outbox"
                outbox.mkdir(parents=True)
                (outbox / ("0" * 64 + ".json")).write_text("{}", encoding="utf-8")
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    self.server.payloads.clear()
                self.assertFalse(list(outbox.glob("*.json")))
                self.assertEqual(len(list((self.state / "dead").glob("*.json"))), 1)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_outbox_drops_prompt_and_preserves_redacted_markdown(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event()
                event["input-messages"] = ["prompt-private-marker"]
                event["last-assistant-message"] = "Prima riga\n\n- token=top-secret-value\n- seconda riga"
                self.run_ok(self.hook_command(implementation, event))
                queued = list((self.state / "outbox").glob("*.json"))
                self.assertEqual(len(queued), 1)
                raw_record = queued[0].read_text(encoding="utf-8-sig")
                self.assertNotIn("input-messages", raw_record)
                self.assertNotIn("prompt-private-marker", raw_record)
                self.assertNotIn("top-secret-value", raw_record)
                record = json.loads(raw_record)
                self.assertIn("\n\n- token=[REDACTED]\n", record["event"]["last-assistant-message"])
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    payload = self.server.payloads.pop()
                self.assertTrue(payload["markdown"])
                self.assertIn("\n\n- token=[REDACTED]\n", payload["message"])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_message_can_be_excluded_from_storage_and_delivery(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["include_message"] = False
        self.config.write_text(json.dumps(config), encoding="utf-8")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event()
                event["last-assistant-message"] = "private-final-message-marker"
                self.run_ok(self.hook_command(implementation, event))
                queued = list((self.state / "outbox").glob("*.json"))
                self.assertEqual(len(queued), 1)
                record = json.loads(queued[0].read_text(encoding="utf-8-sig"))
                self.assertEqual(record["event"]["last-assistant-message"], "")
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    payload = self.server.payloads.pop()
                self.assertNotIn("private-final-message-marker", payload["message"])
                self.assertIn("Turn completed.", payload["message"])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_thread_title_requires_explicit_opt_in(self) -> None:
        thread_id = str(uuid.uuid4())
        sensitive_title = "sensitive prompt-derived title"
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": thread_id, "thread_name": sensitive_title}) + "\n",
            encoding="utf-8",
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                config = json.loads(self.config.read_text(encoding="utf-8-sig"))
                config["include_thread_title"] = False
                self.config.write_text(json.dumps(config), encoding="utf-8")
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertNotIn(sensitive_title, payload["title"])
                self.assertIn("perfect notifier", payload["title"])
                shutil.rmtree(self.state, ignore_errors=True)

                config = json.loads(self.config.read_text(encoding="utf-8-sig"))
                config["include_thread_title"] = True
                self.config.write_text(json.dumps(config), encoding="utf-8")
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertIn(sensitive_title, payload["title"])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_concurrent_hooks_create_one_outbox_item(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event()
                commands = [self.hook_command(implementation, event) for _ in range(6)]
                processes = [subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for command in commands]
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
                self.assertEqual(len(list((self.state / "outbox").glob("*.json"))), 1)
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    self.server.payloads.clear()
                shutil.rmtree(self.state, ignore_errors=True)

    def test_subagent_completion_is_suppressed(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                self.write_session_meta(thread_id, subagent=True)
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                self.assertEqual(len(list((self.state / "suppressed").glob("*.json"))), 1)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_python_bridge_classification_override_writes_suppressed_receipt(self) -> None:
        event = self.event()
        command = [
            sys.executable,
            str(PYTHON_NOTIFIER),
            "--no-spawn",
            "--origin",
            "test-wsl",
            "--session-classification",
            "subagent",
            json.dumps(event),
        ]
        self.run_ok(command)
        self.assertFalse(list((self.state / "outbox").glob("*.json")))
        self.assertEqual(len(list((self.state / "suppressed").glob("*.json"))), 1)

    def test_worker_reclassifies_subagent_created_after_hook(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.assertEqual(len(list((self.state / "outbox").glob("*.json"))), 1)
                self.write_session_meta(thread_id, subagent=True)
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    self.assertFalse(self.server.payloads)
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                self.assertFalse(list((self.state / "sent").glob("*.json")))
                self.assertEqual(len(list((self.state / "suppressed").glob("*.json"))), 1)
                shutil.rmtree(self.state, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows sharing regression")
    def test_powershell_reads_rollout_while_codex_writer_is_open(self) -> None:
        thread_id = str(uuid.uuid4())
        session = self.write_session_meta(thread_id, subagent=True)
        escaped = str(session).replace("'", "''")
        holder_script = (
            f"$stream=[IO.FileStream]::new('{escaped}',[IO.FileMode]::Open,[IO.FileAccess]::Write,"
            "[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete);"
            "try{[Console]::Out.WriteLine('READY');[Console]::Out.Flush();Start-Sleep -Seconds 30}"
            "finally{$stream.Dispose()}"
        )
        holder = subprocess.Popen(
            [str(WINDOWS_POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", holder_script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "READY")
            self.run_ok(self.hook_command("powershell", self.event(thread_id=thread_id)))
            self.assertFalse(list((self.state / "outbox").glob("*.json")))
        finally:
            holder.terminate()
            holder.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_installer_migrates_legacy_secret_without_printing_it(self) -> None:
        install_home = self.temp / "install-home"
        install_home.mkdir(parents=True)
        secret = "test-topic-that-must-not-appear-in-output"
        (install_home / "notify-ntfy.ps1").write_text(
            "$DefaultServer = 'https://ntfy.sh'\n" f"$DefaultTopic = '{secret}'\n",
            encoding="utf-8",
        )
        (install_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INSTALLER),
                "-CodexHome",
                str(install_home),
                "-NoWsl",
                "-SkipScheduledTask",
            ],
            env={**os.environ, "CODEX_NTFY_TOKEN": "test-auth-secret-that-must-not-appear"},
            text=True,
            capture_output=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, msg=combined)
        self.assertNotIn(secret, combined)
        self.assertNotIn("test-auth-secret-that-must-not-appear", combined)
        private_config = json.loads((install_home / "ntfy-config.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(private_config["topic"], secret)
        self.assertEqual(private_config["token"], "test-auth-secret-that-must-not-appear")
        self.assertEqual(private_config["idle_detection_mode"], "strict")
        self.assertEqual(private_config["idle_grace_seconds"], 1.5)
        self.assertTrue(private_config["goal_aware"])
        self.assertTrue(private_config["watch_rollouts"])
        self.assertEqual(private_config["watch_discovery_seconds"], 60)
        self.assertEqual(private_config["watch_roots"], [])
        self.assertEqual(private_config["max_attempts"], 0)
        self.assertNotIn(secret, (install_home / "notify-ntfy.ps1").read_text(encoding="utf-8-sig"))
        self.assertIn("notify-ntfy.ps1", (install_home / "config.toml").read_text(encoding="utf-8-sig"))

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_fresh_windows_install_is_private_and_updates_managed_hook(self) -> None:
        install_home = ROOT / ".test-runtime" / f"custom-codex-home-{uuid.uuid4().hex}"
        install_home.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, install_home, True)
        old_hook = 'notify = ["powershell.exe", "-File", "C:\\\\old\\\\notify-ntfy.ps1"]\n\n'
        nested = '[tools]\nnotify = ["nested-tool-hook"]\n'
        (install_home / "config.toml").write_text(old_hook + nested, encoding="utf-8")
        existing_hooks = {
            "metadata": {"preserve": True},
            "hooks": {
                "Stop": [
                    {
                        "matcher": "keep-me",
                        "hooks": [
                            {"type": "command", "command": r"C:\tools\notify-ntfy-helper.exe"},
                            {"type": "command", "command": r"C:\old\notify-ntfy.ps1"},
                        ],
                    }
                ],
                "SubagentStop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "/old/notify-ntfy.py"},
                            {"type": "command", "command": "/usr/bin/unrelated-hook"},
                        ]
                    }
                ],
            },
        }
        (install_home / "hooks.json").write_text(json.dumps(existing_hooks), encoding="utf-8")
        relative_home = os.path.relpath(install_home, ROOT)
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INSTALLER),
                "-CodexHome",
                relative_home,
                "-NoWsl",
                "-SkipScheduledTask",
            ],
            env={**os.environ, "CODEX_NTFY_TOPIC": "fresh-test-topic"},
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((install_home / "ntfy-config.json").read_text(encoding="utf-8-sig"))
        self.assertFalse(config["include_message"])
        self.assertFalse(config["include_thread_title"])
        self.assertEqual(config["idle_detection_mode"], "strict")
        self.assertEqual(config["idle_grace_seconds"], 1.5)
        self.assertEqual(config["idle_probe_grace_seconds"], 30)
        self.assertTrue(config["goal_aware"])
        self.assertEqual(config["goal_poll_seconds"], 1)
        self.assertEqual(config["subagent_orphan_seconds"], 1800)
        self.assertTrue(config["suppress_technical_turns"])
        self.assertTrue(config["watch_rollouts"])
        self.assertEqual(config["watch_scan_seconds"], 2)
        self.assertEqual(config["watch_discovery_seconds"], 60)
        self.assertEqual(config["watch_initial_replay_seconds"], 15)
        self.assertEqual(config["watch_roots"], [])
        text = (install_home / "config.toml").read_text(encoding="utf-8-sig")
        self.assertIn("System32\\\\WindowsPowerShell", text)
        self.assertIn(nested, text)
        self.assertEqual(text.count("notify = ["), 2)
        hooks_path = install_home / "hooks.json"
        installed_hooks = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(installed_hooks["metadata"]["preserve"])
        all_handlers = [
            handler
            for groups in installed_hooks["hooks"].values()
            for group in groups
            for handler in group.get("hooks", [])
        ]
        commands = [str(handler.get("command", "")) for handler in all_handlers]
        self.assertIn(r"C:\tools\notify-ntfy-helper.exe", commands)
        self.assertIn("/usr/bin/unrelated-hook", commands)
        self.assertNotIn(r"C:\old\notify-ntfy.ps1", commands)
        self.assertNotIn("/old/notify-ntfy.py", commands)
        managed_stop = [
            handler
            for group in installed_hooks["hooks"]["Stop"]
            for handler in group.get("hooks", [])
            if "notify-ntfy.ps1" in str(handler.get("command", ""))
        ]
        self.assertEqual(len(managed_stop), 1)
        self.assertIn("-HookEvent", managed_stop[0]["command"])
        first_hooks_bytes = hooks_path.read_bytes()
        reinstall = subprocess.run(
            [
                str(WINDOWS_POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(INSTALLER), "-CodexHome", relative_home, "-NoWsl", "-SkipScheduledTask",
            ],
            env={**os.environ, "CODEX_NTFY_TOPIC": "fresh-test-topic"},
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(reinstall.returncode, 0, reinstall.stdout + reinstall.stderr)
        self.assertEqual(hooks_path.read_bytes(), first_hooks_bytes)
        vbs = (install_home / "watch-codex-ntfy-hidden.vbs").read_text(encoding="utf-8-sig")
        self.assertIn("WScript.ScriptFullName", vbs)
        self.assertNotIn("C:\\Windows", vbs)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_install_rolls_back_unrelated_notify_conflict(self) -> None:
        install_home = self.temp / "conflicting-home"
        install_home.mkdir()
        original = 'notify = ["other-hook"]\n\n[model]\n'
        (install_home / "config.toml").write_text(original, encoding="utf-8")
        original_hooks = json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-hook"}]}]}}
        )
        (install_home / "hooks.json").write_text(original_hooks, encoding="utf-8")
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INSTALLER),
                "-CodexHome",
                str(install_home),
                "-NoWsl",
                "-SkipScheduledTask",
            ],
            env={**os.environ, "CODEX_NTFY_TOPIC": "rollback-test-topic"},
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated", (result.stdout + result.stderr).lower())
        self.assertEqual((install_home / "config.toml").read_text(encoding="utf-8-sig"), original)
        self.assertEqual((install_home / "hooks.json").read_text(encoding="utf-8-sig"), original_hooks)
        self.assertFalse((install_home / "ntfy-config.json").exists())
        self.assertFalse((install_home / "notify-ntfy.ps1").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
