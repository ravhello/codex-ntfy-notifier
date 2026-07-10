from __future__ import annotations

import json
import os
import shutil
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
        if os.name == "nt" and WINDOWS_POWERSHELL.exists():
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
        session_dir = self.codex_home / "sessions" / "2026" / "01" / "01"
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
        text = (install_home / "config.toml").read_text(encoding="utf-8-sig")
        self.assertIn("System32\\\\WindowsPowerShell", text)
        self.assertIn(nested, text)
        self.assertEqual(text.count("notify = ["), 2)
        vbs = (install_home / "watch-codex-ntfy-hidden.vbs").read_text(encoding="utf-8-sig")
        self.assertIn("WScript.ScriptFullName", vbs)
        self.assertNotIn("C:\\Windows", vbs)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_install_rolls_back_unrelated_notify_conflict(self) -> None:
        install_home = self.temp / "conflicting-home"
        install_home.mkdir()
        original = 'notify = ["other-hook"]\n\n[model]\n'
        (install_home / "config.toml").write_text(original, encoding="utf-8")
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
        self.assertFalse((install_home / "ntfy-config.json").exists())
        self.assertFalse((install_home / "notify-ntfy.ps1").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
