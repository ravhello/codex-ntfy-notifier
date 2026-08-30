from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_NOTIFIER = ROOT / "src" / "notify-ntfy.ps1"
PYTHON_NOTIFIER = ROOT / "src" / "notify-ntfy.py"
INSTALLER = ROOT / "install.ps1"
WINDOWS_POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
_PWSH_EXECUTABLE = shutil.which("pwsh.exe") or shutil.which("pwsh")
POWERSHELL_7 = Path(_PWSH_EXECUTABLE) if _PWSH_EXECUTABLE else None


class RecordingServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), RecordingHandler)
        self.payloads: list[dict] = []
        self.raw_bodies: list[bytes] = []
        self.request_headers: list[dict[str, str]] = []
        self.statuses: list[int] = []
        self.redirect_url: str | None = None
        self.redirect_hits = 0
        self.lock = threading.Lock()


class RecordingHandler(BaseHTTPRequestHandler):
    server: RecordingServer

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        payload = json.loads(raw_body.decode("utf-8", errors="strict"))
        with self.server.lock:
            self.server.payloads.append(payload)
            self.server.raw_bodies.append(raw_body)
            self.server.request_headers.append({key.lower(): value for key, value in self.headers.items()})
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
        self.audncode_home = self.temp / "audncode-home"
        self.audncode_hosts: dict[str, tuple[subprocess.Popen[bytes], int]] = {}
        self.codex_home.mkdir()
        for audncode_directory in ("projects", "sessions", "teams", "tasks"):
            (self.audncode_home / audncode_directory).mkdir(parents=True, exist_ok=True)
        self.audncode_hook_generation = uuid.uuid4().hex
        self.write_audncode_hook_observation_marker(
            installed_unix_ms=int(time.time() * 1000) - 1000,
            generation=self.audncode_hook_generation,
        )
        self.state_database = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(self.state_database)
        try:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, thread_source TEXT, title TEXT)"
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
                    "timeout_seconds": 5,
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
            self.server.raw_bodies.clear()
            self.server.request_headers.clear()
            self.server.statuses.clear()
            self.server.redirect_url = None
            self.server.redirect_hits = 0

    def tearDown(self) -> None:
        hosts_by_pid = {
            process.pid: (process, started_at)
            for process, started_at in self.audncode_hosts.values()
        }
        for process, _started_at in hosts_by_pid.values():
            if process.poll() is None:
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=5)
        shutil.rmtree(self.temp, ignore_errors=True)

    def implementations(self) -> list[str]:
        if os.environ.get("CODEX_NTFY_TEST_POWERSHELL_ONLY") == "1":
            return ["powershell"] if os.name == "nt" and WINDOWS_POWERSHELL.exists() else []
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

    def index_thread_rollout(self, thread_id: str, rollout: Path, *, subagent: bool) -> None:
        source = json.dumps({"subagent": {"parent_thread_id": str(uuid.uuid4())}}) if subagent else "vscode"
        connection = sqlite3.connect(self.state_database)
        try:
            connection.execute(
                "INSERT OR REPLACE INTO threads(id, rollout_path, source, thread_source, title) "
                "VALUES (?, ?, ?, ?, '')",
                (thread_id, str(rollout), source, "subagent" if subagent else "user"),
            )
            connection.commit()
        finally:
            connection.close()

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

    def assert_worker_ok(self, process: subprocess.Popen[str], *, timeout: float = 60) -> None:
        stdout, stderr = process.communicate(timeout=timeout)
        self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")

    def windows_pid_is_alive(self, pid: int) -> bool:
        if os.name != "nt" or pid <= 0:
            return False
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            close_handle(handle)

    def taskkill_tree_if_running(
        self,
        pid: int,
        *,
        timeout: float = 20,
        tree: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = ["taskkill", "/PID", str(pid)]
        if tree:
            command.append("/T")
        command.append("/F")
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0 and self.windows_pid_is_alive(pid):
            self.fail(result.stdout + result.stderr)
        return result

    def stop_continuous_worker(self, process: subprocess.Popen[str]) -> tuple[str, str]:
        child_pids: set[int] = set()
        for health_name in ("watch-health.json", "remote-watch-health.json", "delivery-health.json"):
            try:
                child_pid = int(
                    json.loads((self.state / health_name).read_text(encoding="utf-8-sig")).get("pid", 0) or 0
                )
                if child_pid > 0:
                    child_pids.add(child_pid)
            except (FileNotFoundError, PermissionError, json.JSONDecodeError, TypeError, ValueError):
                pass
        if process.poll() is None:
            process.terminate()
        for child_pid in child_pids - {process.pid}:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(child_pid, signal.SIGTERM)
            else:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
        return stdout, stderr

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

    def claude_hook_command(self, *, origin: str = "Claude Code", input_encoding: int | None = None) -> list[str]:
        if input_encoding is not None:
            script_path = str(POWERSHELL_NOTIFIER).replace("'", "''")
            escaped_origin = origin.replace("'", "''")
            command = (
                f"[Console]::InputEncoding = [Text.Encoding]::GetEncoding({input_encoding}); "
                f"& '{script_path}' -NoSpawn -ClaudeHook -ReadStdin -Origin '{escaped_origin}'"
            )
            return [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ]
        return [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_NOTIFIER),
            "-NoSpawn",
            "-ClaudeHook",
            "-ReadStdin",
            "-Origin",
            origin,
        ]

    def claude_event(
        self,
        *,
        session_id: str | None = None,
        prompt_id: str | None = None,
        transcript_path: Path | None = None,
    ) -> dict:
        return {
            "hook_event_name": "Stop",
            "session_id": session_id or str(uuid.uuid4()),
            "prompt_id": prompt_id or str(uuid.uuid4()),
            "transcript_path": str(transcript_path or self.temp / "missing-transcript.jsonl"),
            "cwd": r"C:\work\perfect notifier",
            "last_assistant_message": "Claude final response.",
            "stop_hook_active": False,
            "background_tasks": [],
            "session_crons": [],
        }

    def claude_idle_event(
        self,
        event: dict,
        *,
        notification_type: str = "idle_prompt",
        include_prompt_id: bool = False,
    ) -> dict:
        idle = {
            "hook_event_name": "Notification",
            "notification_type": notification_type,
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "message": "Claude is waiting for input",
        }
        if include_prompt_id and event.get("prompt_id"):
            idle["prompt_id"] = event["prompt_id"]
        return idle

    def claude_prompt_event(self, event: dict, *, prompt_id: str | None = None) -> dict:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": event["session_id"],
            "prompt_id": prompt_id or event.get("prompt_id", ""),
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "prompt": "Continue",
        }

    def append_claude_goal_status(
        self,
        transcript: Path,
        *,
        met: bool,
        failed: bool | None = None,
        sentinel: bool | None = None,
        reason: str | None = None,
    ) -> str:
        marker = str(uuid.uuid4())
        attachment: dict[str, object] = {"type": "goal_status", "met": met}
        if failed is not None:
            attachment["failed"] = failed
        if sentinel is not None:
            attachment["sentinel"] = sentinel
        if reason is not None:
            attachment["reason"] = reason
        entry = {
            "type": "attachment",
            "uuid": marker,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attachment": attachment,
        }
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
        return marker

    def run_claude_hook(
        self, event: dict, *, input_encoding: int | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            self.claude_hook_command(input_encoding=input_encoding),
            input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            env=self.env,
            capture_output=True,
            timeout=60,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
        self.assertEqual(stdout.strip(), "{}")
        return result

    def audncode_hook_command(
        self,
        *,
        expected_event: str = "Stop",
        origin: str = "AudnCode",
        powershell_path: Path = WINDOWS_POWERSHELL,
    ) -> list[str]:
        return [
            str(powershell_path),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_NOTIFIER),
            "-NoSpawn",
            "-AudnCodeHook",
            "-ReadStdin",
            "-Origin",
            origin,
            "-AudnCodeHome",
            str(self.audncode_home),
            "-AudnCodeExpectedEvent",
            expected_event,
        ]

    def write_audncode_hook_observation_marker(
        self,
        *,
        installed_unix_ms: int,
        generation: str | None = None,
    ) -> Path:
        resolved_generation = generation or uuid.uuid4().hex
        marker = self.audncode_home / ".codex-ntfy-hooks.json"
        marker.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "codex-ntfy-audncode-hooks",
                    "notifier_version": "2.6.0",
                    "hook_shape_version": 9,
                    "audncode_home": str(self.audncode_home.resolve()),
                    "generation": resolved_generation,
                    "installed_unix_ms": installed_unix_ms,
                }
            ),
            encoding="utf-8",
        )
        self.audncode_hook_generation = resolved_generation
        return marker

    def windows_process_start_utc_ticks(self, pid: int) -> int:
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"[Diagnostics.Process]::GetProcessById({pid}).StartTime.ToUniversalTime().Ticks",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return int(result.stdout.strip())

    def protect_audncode_recovery_directory(self, path: Path) -> None:
        if not hasattr(self, "_windows_current_sid"):
            result = subprocess.run(
                [
                    str(WINDOWS_POWERSHELL),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self._windows_current_sid = result.stdout.strip()
        acl_env = os.environ.copy()
        acl_env.update(
            {
                "CODEX_NTFY_TEST_ACL_PATH": str(path),
                "CODEX_NTFY_TEST_ACL_CURRENT_SID": self._windows_current_sid,
            }
        )
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                r"""
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
  [Security.AccessControl.InheritanceFlags]::ObjectInherit
foreach ($sidText in @($env:CODEX_NTFY_TEST_ACL_CURRENT_SID, 'S-1-5-18', 'S-1-5-32-544')) {
  $sid = [Security.Principal.SecurityIdentifier]::new($sidText)
  $rule = [Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance,
    [Security.AccessControl.PropagationFlags]::None,
    [Security.AccessControl.AccessControlType]::Allow
  )
  [void]$acl.AddAccessRule($rule)
}
[IO.Directory]::SetAccessControl($env:CODEX_NTFY_TEST_ACL_PATH, $acl)
""",
            ],
            env=acl_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def protect_audncode_recovery_file(self, path: Path) -> None:
        if not hasattr(self, "_windows_current_sid"):
            self.protect_audncode_recovery_directory(path.parent)
        acl_env = os.environ.copy()
        acl_env.update(
            {
                "CODEX_NTFY_TEST_ACL_PATH": str(path),
                "CODEX_NTFY_TEST_ACL_CURRENT_SID": self._windows_current_sid,
            }
        )
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                r"""
$acl = [Security.AccessControl.FileSecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sidText in @($env:CODEX_NTFY_TEST_ACL_CURRENT_SID, 'S-1-5-18', 'S-1-5-32-544')) {
  $sid = [Security.Principal.SecurityIdentifier]::new($sidText)
  $rule = [Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
  )
  [void]$acl.AddAccessRule($rule)
}
[IO.File]::SetAccessControl($env:CODEX_NTFY_TEST_ACL_PATH, $acl)
""",
            ],
            env=acl_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def create_audncode_recovery_marker(
        self,
        event: dict,
        *,
        host: tuple[subprocess.Popen[bytes], int] | None = None,
        manager_instance_id: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        selected_host = host or self.audncode_hosts[event["session_id"]]
        manager_process, _host_started_at = selected_host
        manager_id = manager_instance_id or str(uuid.uuid4())
        operation = operation_id or str(uuid.uuid4())
        attempt = attempt_id or str(uuid.uuid4())
        recovery_root = self.audncode_home / "codex-ntfy-recovery"
        recovery_root.mkdir(parents=True, exist_ok=True)
        self.protect_audncode_recovery_directory(recovery_root)
        marker_dir = recovery_root / manager_id
        marker_dir.mkdir(parents=True, exist_ok=True)
        self.protect_audncode_recovery_directory(marker_dir)
        marker_path = marker_dir / f"{operation}-{attempt}.json"
        now_ms = int(time.time() * 1000)
        marker: dict[str, object] = {
            "schema": 1,
            "kind": "codex-ntfy-audncode-recovery",
            "manager_instance_id": manager_id,
            "operation_id": operation,
            "attempt_id": attempt,
            "session_id": event["session_id"],
            "manager_pid": manager_process.pid,
            "manager_process_start_utc_ticks": self.windows_process_start_utc_ticks(
                manager_process.pid
            ),
            "state": "recovering",
            "revision": 1,
            "reason": "runtime-attempt-active",
            "failure_record_uuid": "",
            "created_unix_ms": now_ms,
            "updated_unix_ms": now_ms,
        }
        marker_path.write_text(json.dumps(marker, separators=(",", ":")), encoding="utf-8")
        self.protect_audncode_recovery_file(marker_path)
        return marker_path, marker

    def transition_audncode_recovery_marker(
        self,
        marker_path: Path,
        marker: dict[str, object],
        *,
        state: str,
        reason: str,
        failure_record_uuid: str,
        revision: int = 2,
    ) -> dict[str, object]:
        transitioned = {
            **marker,
            "state": state,
            "revision": revision,
            "reason": reason,
            "failure_record_uuid": failure_record_uuid,
            "updated_unix_ms": max(
                int(marker["created_unix_ms"]), int(time.time() * 1000)
            ),
        }
        temporary = marker_path.with_name(f".{marker_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(transitioned, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(temporary, marker_path)
        self.protect_audncode_recovery_file(marker_path)
        return transitioned

    def write_audncode_recovery_marker(
        self, marker_path: Path, marker: dict[str, object]
    ) -> None:
        temporary = marker_path.with_name(f".{marker_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(marker, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, marker_path)
        self.protect_audncode_recovery_file(marker_path)

    def prepare_audncode_managed_failure(
        self, label: str
    ) -> tuple[dict, dict, str, Path, dict[str, object]]:
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt=f"Managed recovery {label}")
        )
        failure, _user_uuid, failure_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message=f"Managed provider failure {label}",
        )
        marker_path, marker = self.create_audncode_recovery_marker(event)
        return event, failure, failure_uuid, marker_path, marker

    def seed_audncode_recovery_journal_fixture(self, label: str) -> dict[str, object]:
        """Create a valid content-bearing recovery journal without live hook latency."""
        thread_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        key = hashlib.sha256(
            f"codex-ntfy/v1|claude|{thread_id}|{turn_id}".encode("utf-8")
        ).hexdigest()
        now_ms = int(time.time() * 1000)
        private_sentinel = f"private recovery journal {label} <Qwen & S1>"
        successor = {
            "schema": 1,
            "key": key,
            "sequence_id": f"claude-{key[:32]}",
            "provider": "claude",
            "origin": "audncode",
            "weak_identity": False,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "candidate_kind": "audncode_stop",
            "source_event": "Stop",
            "completion_event_type": "task_complete",
            "candidate_revision": uuid.uuid4().hex,
            "candidate_identity": "",
            "audncode_stop_failure_uuid": "",
            "audncode_recovery_managed": False,
            "audncode_recovery_binding_invalid": False,
            "audncode_stop_failure_ambiguous": False,
            "created_unix_ms": now_ms,
            "next_attempt_unix_ms": 0,
            "attempts": 0,
            "event": {
                "type": "agent-turn-complete",
                "cwd": rf"C:\private\{label}",
                "last-assistant-message": private_sentinel,
            },
        }
        serialized = json.dumps(
            successor, separators=(",", ":"), ensure_ascii=False
        )
        successor_hash = hashlib.sha256(
            ("audncode-managed-recovery-successor/v1|" + serialized).encode("utf-8")
        ).hexdigest()
        failure_revision = uuid.uuid4().hex
        candidate_identity = hashlib.sha256(
            f"failure-proof|{label}|{uuid.uuid4()}".encode("utf-8")
        ).hexdigest()
        failure_uuid = str(uuid.uuid4())
        recovery_binding = hashlib.sha256(
            f"recovery-binding|{label}|{uuid.uuid4()}".encode("utf-8")
        ).hexdigest()
        receipt_key = hashlib.sha256(
            (
                "audncode-managed-recovery-succeeded-receipt/v1|"
                f"{key}|{candidate_identity}|{failure_uuid}"
            ).encode("utf-8")
        ).hexdigest()
        receipt_name = f"r-{receipt_key}.json"
        suppressed_dir = self.state / "suppressed"
        journal_dir = self.state / "recovery-journals"
        suppressed_dir.mkdir(parents=True, exist_ok=True)
        journal_dir.mkdir(parents=True, exist_ok=True)
        tombstone_path = suppressed_dir / receipt_name
        index_path = journal_dir / receipt_name
        tombstone_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "key": key,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "origin": "audncode",
                    "candidate_revision": failure_revision,
                    "suppressed_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "audncode-managed-recovery-succeeded",
                    "candidate_identity": candidate_identity,
                    "audncode_stop_failure_uuid": failure_uuid,
                    "audncode_recovery_binding": recovery_binding,
                    "audncode_recovery_terminal_hash": hashlib.sha256(
                        f"terminal|{label}".encode("utf-8")
                    ).hexdigest(),
                    "successor": serialized,
                    "successor_hash": successor_hash,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        index_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "key": key,
                    "receipt_name": receipt_name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return {
            "key": key,
            "successor": successor,
            "failure_revision": failure_revision,
            "tombstone_path": tombstone_path,
            "index_path": index_path,
            "private_sentinel": private_sentinel,
        }

    def write_schema2_terminal_suppression(
        self, fixture: dict[str, object], record: dict[str, object], reason: str
    ) -> Path:
        path = self.state / "suppressed" / f"{fixture['key']}.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "key": record["key"],
                    "provider": record["provider"],
                    "weak_identity": record["weak_identity"],
                    "sequence_id": record["sequence_id"],
                    "thread_id": record["thread_id"],
                    "turn_id": record["turn_id"],
                    "origin": record["origin"],
                    "candidate_kind": record["candidate_kind"],
                    "source_event": record["source_event"],
                    "completion_event_type": record["completion_event_type"],
                    "candidate_revision": record["candidate_revision"],
                    "suppressed_at": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return path

    def assert_content_free_suppression_receipt(
        self, record: dict[str, object], reason: str
    ) -> dict[str, object]:
        receipts = list((self.state / "suppressed").glob("*.json"))
        self.assertEqual(len(receipts), 1, self.state_debug())
        receipt = json.loads(receipts[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(receipt.get("schema"), 2, receipt)
        self.assertEqual(receipt.get("reason"), reason, receipt)
        self.assertEqual(receipt.get("provider"), "claude", receipt)
        self.assertFalse(receipt.get("weak_identity"), receipt)
        self.assertRegex(str(receipt.get("candidate_revision", "")), r"^[0-9a-f]{32}$")
        for field in (
            "key",
            "sequence_id",
            "thread_id",
            "turn_id",
            "candidate_kind",
            "source_event",
            "completion_event_type",
            "candidate_revision",
        ):
            self.assertEqual(receipt.get(field), record.get(field), receipt)
        for private_field in ("event", "last-assistant-message", "transcript_path"):
            self.assertNotIn(private_field, receipt, receipt)
        return receipt

    def start_audncode_host(
        self, session_id: str, *, cwd: str = r"C:\work\perfect notifier"
    ) -> tuple[subprocess.Popen[bytes], int]:
        # AudnCode starts hooks as child processes. The proxy keeps that real
        # ancestry in the contract tests instead of relying on an unrelated
        # sleeping PID, which would mask cross-window attribution bugs.
        runner = r"""
import base64
import json
import os
import subprocess
import sys
import threading
import time

while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    try:
        request = json.loads(line.decode("utf-8"))
        if "batch" in request:
            batch_items = request["batch"]
            children = [
                subprocess.Popen(
                    item["command"],
                    env=item["env"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for item in batch_items
            ]
            batch_results = [None] * len(children)

            def communicate_child(index, child, item):
                wait_for_path = item.get("wait_for_path", "")
                if wait_for_path:
                    deadline = time.time() + 30
                    while not os.path.exists(wait_for_path) and time.time() < deadline:
                        time.sleep(0.01)
                    if not os.path.exists(wait_for_path):
                        child.kill()
                        stdout, stderr = child.communicate()
                        batch_results[index] = {
                            "returncode": 124,
                            "stdout": base64.b64encode(stdout).decode("ascii"),
                            "stderr": base64.b64encode(
                                stderr + b"proxy start barrier timed out"
                            ).decode("ascii"),
                        }
                        return
                try:
                    stdout, stderr = child.communicate(
                        input=base64.b64decode(item["input"]),
                        timeout=180,
                    )
                except subprocess.TimeoutExpired as exc:
                    child.kill()
                    stdout, stderr = child.communicate()
                    if exc.stdout:
                        stdout = exc.stdout + stdout
                    if exc.stderr:
                        stderr = exc.stderr + stderr
                    child.returncode = 124
                batch_results[index] = {
                    "returncode": child.returncode,
                    "stdout": base64.b64encode(stdout).decode("ascii"),
                    "stderr": base64.b64encode(stderr).decode("ascii"),
                }

            batch_threads = [
                threading.Thread(
                    target=communicate_child,
                    args=(index, child, item),
                    daemon=True,
                )
                for index, (child, item) in enumerate(zip(children, batch_items))
            ]
            for batch_thread in batch_threads:
                batch_thread.start()
            for batch_thread in batch_threads:
                batch_thread.join(timeout=185)
            if any(batch_thread.is_alive() for batch_thread in batch_threads):
                for child in children:
                    if child.poll() is None:
                        child.kill()
                raise subprocess.TimeoutExpired("concurrent hook batch", 185)
            response = {"batch": batch_results}
            sys.stdout.buffer.write(json.dumps(response).encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
            continue
        child = subprocess.Popen(
            request["command"],
            env=request["env"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child_started_path = request.get("child_started_path", "")
        if child_started_path:
            child_started_temp = (
                f"{child_started_path}.{os.getpid()}.{child.pid}.{time.time_ns()}.tmp"
            )
            try:
                with open(child_started_temp, "w", encoding="ascii") as stream:
                    stream.write(str(child.pid))
                os.replace(child_started_temp, child_started_path)
            finally:
                if os.path.exists(child_started_temp):
                    os.unlink(child_started_temp)
        prearm_seen = threading.Event()
        monitor_stop = threading.Event()
        monitor = None
        prearm_path = request.get("kill_when_prearm_path", "")
        if prearm_path:
            def kill_after_prearm():
                deadline = time.time() + 15
                while not monitor_stop.is_set() and time.time() < deadline:
                    try:
                        with open(prearm_path, "r", encoding="utf-8-sig") as stream:
                            state = json.load(stream)
                        if state.get("audncode_prompt_prearm_pending") is True:
                            prearm_seen.set()
                            child.kill()
                            return
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.005)
            monitor = threading.Thread(target=kill_after_prearm, daemon=True)
            monitor.start()
        delay_input_ms = max(0, int(request.get("delay_input_ms", 0)))
        if delay_input_ms:
            delay_deadline = time.monotonic() + (delay_input_ms / 1000)
            while child.poll() is None and time.monotonic() < delay_deadline:
                time.sleep(min(0.01, max(0, delay_deadline - time.monotonic())))
        try:
            close_stdin_after_ms = max(0, int(request.get("close_stdin_after_ms", 0)))
            exited_before_stdin_close = False
            if close_stdin_after_ms:
                stdin_bytes = base64.b64decode(request["input"])
                if stdin_bytes:
                    child.stdin.write(stdin_bytes)
                    child.stdin.flush()
                close_deadline = time.monotonic() + (close_stdin_after_ms / 1000)
                while child.poll() is None and time.monotonic() < close_deadline:
                    time.sleep(min(0.01, max(0, close_deadline - time.monotonic())))
                exited_before_stdin_close = child.poll() is not None
                child.stdin.close()
                child.stdin = None
                stdout, stderr = child.communicate(timeout=60)
            elif request.get("hold_stdin_open"):
                stdin_bytes = base64.b64decode(request["input"])
                if stdin_bytes:
                    child.stdin.write(stdin_bytes)
                    child.stdin.flush()
                child.wait(timeout=60)
                stdout = child.stdout.read()
                stderr = child.stderr.read()
                child.stdin.close()
            else:
                stdout, stderr = child.communicate(
                    input=base64.b64decode(request["input"]),
                    timeout=60,
                )
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
            raise
        finally:
            monitor_stop.set()
            if monitor is not None:
                monitor.join(timeout=1)
        response = {
            "returncode": child.returncode,
            "stdout": base64.b64encode(stdout).decode("ascii"),
            "stderr": base64.b64encode(stderr).decode("ascii"),
            "prearm_seen": prearm_seen.is_set(),
            "exited_before_stdin_close": exited_before_stdin_close,
        }
    except subprocess.TimeoutExpired as exc:
        response = {
            "returncode": 124,
            "stdout": base64.b64encode(exc.stdout or b"").decode("ascii"),
            "stderr": base64.b64encode(exc.stderr or b"hook timed out").decode("ascii"),
        }
    except Exception as exc:
        response = {
            "returncode": 125,
            "stdout": "",
            "stderr": base64.b64encode(repr(exc).encode("utf-8")).decode("ascii"),
        }
    sys.stdout.buffer.write(json.dumps(response).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
"""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", runner],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        started_at = int(time.time() * 1000)
        marker_path = self.audncode_home / "sessions" / f"{process.pid}.json"
        marker_path.write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "sessionId": session_id,
                    "cwd": cwd,
                    "startedAt": started_at,
                    "kind": "interactive",
                    "entrypoint": "cli",
                }
            ),
            encoding="utf-8",
        )
        return process, started_at

    def audncode_event(
        self,
        *,
        session_id: str | None = None,
        transcript_path: Path | None = None,
        message: str = "AudnCode final response.",
        project_root: Path | str | None = None,
    ) -> dict:
        resolved_session_id = session_id or str(uuid.uuid4())
        resolved_project_root = str(project_root or r"C:\work\perfect notifier")
        if project_root is not None:
            Path(resolved_project_root).mkdir(parents=True, exist_ok=True)
        host = self.audncode_hosts.get(resolved_session_id)
        if host is None:
            host = self.start_audncode_host(resolved_session_id, cwd=resolved_project_root)
            self.audncode_hosts[resolved_session_id] = host
        host_process, host_started_at = host
        marker_path = self.audncode_home / "sessions" / f"{host_process.pid}.json"
        marker_path.write_text(
            json.dumps(
                {
                    "pid": host_process.pid,
                    "sessionId": resolved_session_id,
                    "cwd": resolved_project_root,
                    "startedAt": host_started_at,
                    "kind": "interactive",
                    "entrypoint": "cli",
                }
            ),
            encoding="utf-8",
        )
        return {
            "hook_event_name": "Stop",
            "session_id": resolved_session_id,
            "transcript_path": str(
                transcript_path or self.audncode_home / "projects" / f"{resolved_session_id}.jsonl"
            ),
            "cwd": resolved_project_root,
            "last_assistant_message": message,
            "stop_hook_active": False,
        }

    def exit_audncode_host(self, session_id: str) -> None:
        host_process, _host_started_at = self.audncode_hosts.pop(session_id)
        marker_path = self.audncode_home / "sessions" / f"{host_process.pid}.json"
        if host_process.poll() is None:
            host_process.terminate()
        host_process.communicate(timeout=10)
        # AudnCode's real cleanup registry unlinks this marker on a clean exit.
        marker_path.unlink(missing_ok=True)

    def write_audncode_cron_lock(
        self,
        event: dict,
        *,
        owner_session_id: str | None = None,
        owner_pid: int | None = None,
        acquired_at: int | None = None,
    ) -> Path:
        resolved_session_id = owner_session_id or event["session_id"]
        if owner_pid is None:
            owner_pid = self.audncode_hosts[resolved_session_id][0].pid
        cron_directory = Path(event["cwd"]) / ".claude"
        cron_directory.mkdir(parents=True, exist_ok=True)
        lock_path = cron_directory / "scheduled_tasks.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "sessionId": resolved_session_id,
                    "pid": owner_pid,
                    "acquiredAt": acquired_at or int(time.time() * 1000),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return lock_path

    def append_audncode_queue_operation(
        self, event: dict, operation: str, *, content: str | None = None
    ) -> None:
        entry = {
            "type": "queue-operation",
            "operation": operation,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sessionId": event["session_id"],
        }
        if content is not None:
            entry["content"] = content
        with Path(event["transcript_path"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def append_audncode_task_notification(
        self,
        event: dict,
        content: str,
        *,
        origin_kind: str = "task-notification",
        is_sidechain: bool = False,
    ) -> None:
        entry = {
            "parentUuid": None,
            "isSidechain": is_sidechain,
            "type": "user",
            "message": {"role": "user", "content": content},
            "uuid": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origin": {"kind": origin_kind},
            "userType": "external",
            "cwd": event["cwd"],
            "sessionId": event["session_id"],
            "version": "0.9.1",
        }
        with Path(event["transcript_path"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def append_audncode_task_notification_attachment(
        self, event: dict, content: str, *, command_mode: str = "task-notification"
    ) -> None:
        entry = {
            "parentUuid": str(uuid.uuid4()),
            "isSidechain": False,
            "attachment": {
                "type": "queued_command",
                "prompt": content,
                "commandMode": command_mode,
            },
            "type": "attachment",
            "uuid": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "userType": "external",
            "cwd": event["cwd"],
            "sessionId": event["session_id"],
            "version": "0.9.1",
        }
        with Path(event["transcript_path"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def write_audncode_remote_agent_sidecar(
        self,
        event: dict,
        *,
        task_id: str = "r1234abcd",
        remote_task_type: str = "ultrareview",
        is_remote_review: bool = True,
    ) -> Path:
        remote_directory = (
            Path(event["transcript_path"]).parent
            / event["session_id"]
            / "remote-agents"
        )
        remote_directory.mkdir(parents=True, exist_ok=True)
        sidecar = remote_directory / f"remote-agent-{task_id}.meta.json"
        sidecar.write_text(
            json.dumps(
                {
                    "taskId": task_id,
                    "remoteTaskType": remote_task_type,
                    "sessionId": str(uuid.uuid4()),
                    "title": "Remote review",
                    "command": "/ultrareview",
                    "spawnedAt": int(time.time() * 1000),
                    "isRemoteReview": is_remote_review,
                    "isLongRunning": True,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return sidecar

    def write_audncode_task(
        self,
        event: dict,
        *,
        task_id: str = "1",
        status: str,
        task_list_id: str | None = None,
    ) -> Path:
        task_directory = self.audncode_home / "tasks" / (task_list_id or event["session_id"])
        task_directory.mkdir(parents=True, exist_ok=True)
        task_path = task_directory / f"{task_id}.json"
        task_path.write_text(
            json.dumps(
                {
                    "id": task_id,
                    "subject": "Verify notification finality",
                    "description": "Do not notify before the goal is complete.",
                    "status": status,
                    "blocks": [],
                    "blockedBy": [],
                }
            ),
            encoding="utf-8",
        )
        return task_path

    def write_audncode_team(self, event: dict, *, teammate_active: bool) -> Path:
        team_name = "notifier-team"
        team_directory = self.audncode_home / "teams" / team_name
        team_directory.mkdir(parents=True, exist_ok=True)
        config_path = team_directory / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "name": team_name,
                    "leadAgentId": "team-lead",
                    "leadSessionId": event["session_id"],
                    "members": [
                        {"agentId": "team-lead", "name": "lead", "isActive": True},
                        {"agentId": "worker-1", "name": "worker", "isActive": teammate_active},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def audncode_prompt_event(self, event: dict, *, prompt: str = "Continue") -> dict:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "prompt": prompt,
        }

    def audncode_session_start_event(
        self, event: dict, *, source: str = "startup"
    ) -> dict:
        return {
            "hook_event_name": "SessionStart",
            "source": source,
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
        }

    def audncode_subagent_start_event(
        self,
        event: dict,
        *,
        agent_id: str,
        agent_type: str = "general-purpose",
    ) -> dict:
        return {
            "hook_event_name": "SubagentStart",
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "agent_id": agent_id,
            "agent_type": agent_type,
        }

    def audncode_send_message_event(
        self,
        event: dict,
        *,
        success: bool,
        message: str,
        recipient: str = "worker",
    ) -> dict:
        return {
            "hook_event_name": "PostToolUse",
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "tool_name": "SendMessage",
            "tool_input": {"to": recipient, "message": "Continue the task"},
            "tool_response": {"data": {"success": success, "message": message}},
            "tool_use_id": f"send-message-{uuid.uuid4().hex}",
        }

    def append_audncode_goal_status(
        self,
        event: dict,
        *,
        met: bool,
        failed: bool | None = None,
        sentinel: bool | None = None,
        reason: str | None = None,
    ) -> str:
        marker = str(uuid.uuid4())
        attachment: dict[str, object] = {"type": "goal_status", "met": met}
        if failed is not None:
            attachment["failed"] = failed
        if sentinel is not None:
            attachment["sentinel"] = sentinel
        if reason is not None:
            attachment["reason"] = reason
        entry = {
            "parentUuid": None,
            "isSidechain": False,
            "type": "attachment",
            "uuid": marker,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attachment": attachment,
            "sessionId": event["session_id"],
        }
        with Path(event["transcript_path"]).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
        return marker

    def append_audncode_stop_failure_proof(
        self,
        event: dict,
        *,
        error: str,
        message: str,
        error_details: str | None = None,
        assistant_uuid: str | None = None,
        assistant_timestamp_ms: int | None = None,
        parent_uuid: str | None = None,
        append_user: bool = True,
        user_after_error: bool = False,
        agent_id: str | None = None,
        sidechain: bool = False,
        user_content: object = "Current root prompt",
        user_is_meta: bool | None = None,
        user_task_notification: bool = False,
        prefix_entries: list[dict[str, object]] | None = None,
        user_after_error_content: object | None = None,
    ) -> tuple[dict, str, str]:
        transcript = Path(event["transcript_path"])
        _state_path, session_state = self.read_audncode_session_state(event["session_id"])
        busy_ms = int(session_state["busy_unix_ms"])
        user_uuid = parent_uuid or str(uuid.uuid4())
        resolved_assistant_uuid = assistant_uuid or str(uuid.uuid4())
        timestamp_ms = assistant_timestamp_ms if assistant_timestamp_ms is not None else busy_ms + 2
        user_entry = {
            "parentUuid": None,
            "isSidechain": False,
            "type": "user",
            "message": {"role": "user", "content": user_content},
            "uuid": user_uuid,
            "timestamp": datetime.fromtimestamp((busy_ms - 1) / 1000, timezone.utc).isoformat(),
            "sessionId": event["session_id"],
        }
        if user_is_meta is not None:
            user_entry["isMeta"] = user_is_meta
        if user_task_notification:
            user_entry.update(
                {
                    "origin": {"kind": "task-notification"},
                    "userType": "external",
                    "cwd": event["cwd"],
                    "version": "0.9.1",
                }
            )
        assistant_entry = {
            "parentUuid": user_uuid,
            "isSidechain": sidechain,
            "type": "assistant",
            "uuid": resolved_assistant_uuid,
            "timestamp": datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat(),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": message}],
            },
            "isApiErrorMessage": True,
            "error": error,
            "sessionId": event["session_id"],
        }
        if error_details is not None:
            assistant_entry["errorDetails"] = error_details
        if agent_id is not None:
            assistant_entry["agentId"] = agent_id
        later_user = {
            **user_entry,
            "parentUuid": resolved_assistant_uuid,
            "uuid": str(uuid.uuid4()),
            "timestamp": datetime.fromtimestamp((timestamp_ms + 1) / 1000, timezone.utc).isoformat(),
            "message": {
                "role": "user",
                "content": (
                    user_after_error_content
                    if user_after_error_content is not None
                    else "A later root prompt"
                ),
            },
        }
        entries = list(prefix_entries or [])
        if append_user:
            entries.append(user_entry)
        entries.append(assistant_entry)
        if user_after_error:
            entries.append(later_user)
        with transcript.open("a", encoding="utf-8", newline="\n") as stream:
            for entry in entries:
                stream.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
        failure = {
            **event,
            "hook_event_name": "StopFailure",
            "error": error,
            "last_assistant_message": message,
        }
        failure.pop("stop_hook_active", None)
        if error_details is not None:
            failure["error_details"] = error_details
        return failure, user_uuid, resolved_assistant_uuid

    def audncode_idle_event(self, event: dict, *, notification_type: str = "idle_prompt") -> dict:
        return {
            "hook_event_name": "Notification",
            "notification_type": notification_type,
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "message": "Claude is waiting for your input",
        }

    def append_audncode_question(
        self,
        event: dict,
        *,
        question: str = "Quale opzione devo usare?",
        tool_name: str = "AskUserQuestion",
        tool_use_id: str | None = None,
        sidechain: bool = False,
        agent_id: str | None = None,
        multiple_tool_calls: bool = False,
    ) -> dict[str, object]:
        _state_path, session_state = self.read_audncode_session_state(event["session_id"])
        busy_ms = int(session_state["busy_unix_ms"])
        root_uuid = str(uuid.uuid4())
        assistant_uuid = str(uuid.uuid4())
        resolved_tool_use_id = tool_use_id or f"toolu_{uuid.uuid4().hex}"
        tool_input = {
            "questions": [
                {
                    "question": question,
                    "header": "Scelta",
                    "options": [
                        {"label": "Prima", "description": "Usa la prima opzione."},
                        {"label": "Seconda", "description": "Usa la seconda opzione."},
                    ],
                    "multiSelect": False,
                }
            ]
        }
        root_entry = {
            "parentUuid": None,
            "isSidechain": False,
            "type": "user",
            "message": {"role": "user", "content": "Prompt root corrente"},
            "uuid": root_uuid,
            "timestamp": datetime.fromtimestamp((busy_ms + 1) / 1000, timezone.utc).isoformat(),
            "sessionId": event["session_id"],
        }
        content: list[dict[str, object]] = [
            {"type": "thinking", "thinking": "Serve una scelta esplicita."},
            {
                "type": "tool_use",
                "id": resolved_tool_use_id,
                "name": tool_name,
                "input": tool_input,
            },
        ]
        if multiple_tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex}",
                    "name": "AskUserQuestion",
                    "input": tool_input,
                }
            )
        assistant_entry: dict[str, object] = {
            "parentUuid": root_uuid,
            "isSidechain": sidechain,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": content,
                "stop_reason": "tool_use",
            },
            "uuid": assistant_uuid,
            "timestamp": datetime.fromtimestamp((busy_ms + 2) / 1000, timezone.utc).isoformat(),
            "sessionId": event["session_id"],
        }
        if agent_id is not None:
            assistant_entry["agentId"] = agent_id
        with Path(event["transcript_path"]).open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            for entry in (root_entry, assistant_entry):
                stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        return {
            "root_uuid": root_uuid,
            "assistant_uuid": assistant_uuid,
            "tool_use_id": resolved_tool_use_id,
            "tool_input": tool_input,
            "question": question,
        }

    def append_audncode_question_answer(self, event: dict, question: dict[str, object]) -> dict:
        answer_uuid = str(uuid.uuid4())
        answer_entry = {
            "parentUuid": question["assistant_uuid"],
            "isSidechain": False,
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": question["tool_use_id"],
                        "content": "L'utente ha scelto: Prima",
                    }
                ],
            },
            "uuid": answer_uuid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sessionId": event["session_id"],
        }
        with Path(event["transcript_path"]).open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(json.dumps(answer_entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        return {
            "hook_event_name": "PostToolUse",
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "tool_name": "AskUserQuestion",
            "tool_use_id": question["tool_use_id"],
            "tool_input": question["tool_input"],
            "tool_response": {"answers": {"Scelta": "Prima"}},
        }

    def audncode_cron_tool_event(
        self,
        event: dict,
        *,
        action: str,
        cron_id: str,
        recurring: bool = False,
        durable: bool = False,
    ) -> dict:
        if action == "create":
            return {
                "hook_event_name": "PostToolUse",
                "session_id": event["session_id"],
                "transcript_path": event["transcript_path"],
                "cwd": event["cwd"],
                "tool_name": "CronCreate",
                "tool_input": {
                    "cron": "*/5 * * * *",
                    "prompt": "Run the scheduled verification",
                    "recurring": recurring,
                    "durable": durable,
                },
                "tool_response": {
                    "data": {
                        "id": cron_id,
                        "humanSchedule": "every 5 minutes",
                        "recurring": recurring,
                        "durable": durable,
                    }
                },
                "tool_use_id": f"cron-create-{cron_id}",
            }
        if action == "delete":
            return {
                "hook_event_name": "PostToolUse",
                "session_id": event["session_id"],
                "transcript_path": event["transcript_path"],
                "cwd": event["cwd"],
                "tool_name": "CronDelete",
                "tool_input": {"id": cron_id},
                "tool_response": {"data": {"id": cron_id}},
                "tool_use_id": f"cron-delete-{cron_id}",
            }
        raise ValueError(f"unsupported cron action: {action}")

    def append_audncode_cron_tool_proof(self, event: dict, tool_event: dict) -> None:
        tool_use_id = tool_event["tool_use_id"]
        assistant_uuid = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc)
        assistant_entry = {
            "parentUuid": str(uuid.uuid4()),
            "isSidechain": False,
            "type": "assistant",
            "uuid": assistant_uuid,
            "timestamp": timestamp.isoformat(),
            "sessionId": event["session_id"],
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": tool_event["tool_name"],
                        "input": tool_event["tool_input"],
                    }
                ],
            },
        }
        result_entry = {
            "parentUuid": assistant_uuid,
            "isSidechain": False,
            "type": "user",
            "uuid": str(uuid.uuid4()),
            "timestamp": (timestamp + timedelta(milliseconds=1)).isoformat(),
            "sessionId": event["session_id"],
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(
                            tool_event["tool_response"], separators=(",", ":")
                        ),
                    }
                ],
            },
        }
        with Path(event["transcript_path"]).open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            for entry in (assistant_entry, result_entry):
                stream.write(
                    json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n"
                )

    def run_audncode_hook(
        self,
        event: dict,
        *,
        host: tuple[subprocess.Popen[bytes], int] | None = None,
        delay_input_ms: int = 0,
        kill_when_prearm_path: Path | None = None,
        child_started_path: Path | None = None,
        powershell_path: Path = WINDOWS_POWERSHELL,
        env_overrides: dict[str, str] | None = None,
        expect_success: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        selected_host = host or self.audncode_hosts.get(str(event.get("session_id", "")))
        self.assertIsNotNone(selected_host, "AudnCode hook has no owning host")
        host_process, _host_started_at = selected_host
        self.assertIsNotNone(host_process.stdin)
        self.assertIsNotNone(host_process.stdout)
        expected_event = str(event.get("hook_event_name", ""))
        command = self.audncode_hook_command(
            expected_event=expected_event,
            powershell_path=powershell_path,
        )
        request = {
            "command": command,
            "input": base64.b64encode(
                json.dumps(event, ensure_ascii=False).encode("utf-8")
            ).decode("ascii"),
            "env": {**self.env, **(env_overrides or {})},
            "delay_input_ms": delay_input_ms,
        }
        if kill_when_prearm_path is not None:
            request["kill_when_prearm_path"] = str(kill_when_prearm_path)
        if child_started_path is not None:
            request["child_started_path"] = str(child_started_path)
        host_process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        host_process.stdin.flush()
        response_line = host_process.stdout.readline()
        self.assertTrue(response_line, "AudnCode host proxy exited before returning the hook result")
        response = json.loads(response_line.decode("utf-8"))
        result = subprocess.CompletedProcess(
            command,
            int(response["returncode"]),
            base64.b64decode(response["stdout"]),
            base64.b64decode(response["stderr"]),
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if expect_success:
            self.assertEqual(result.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
            self.assertEqual(stdout.strip(), "{}")
        elif kill_when_prearm_path is not None:
            self.assertTrue(response.get("prearm_seen"), response)
        return result

    def run_audncode_raw_hook(
        self,
        raw: bytes,
        *,
        expected_event: str,
        host: tuple[subprocess.Popen[bytes], int],
        delay_input_ms: int = 0,
        hold_stdin_open: bool = False,
        close_stdin_after_ms: int = 0,
        child_started_path: Path | None = None,
        env_overrides: dict[str, str] | None = None,
        expect_success: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        host_process, _host_started_at = host
        self.assertIsNotNone(host_process.stdin)
        self.assertIsNotNone(host_process.stdout)
        command = self.audncode_hook_command(expected_event=expected_event)
        request: dict[str, object] = {
            "command": command,
            "input": base64.b64encode(raw).decode("ascii"),
            "env": {**self.env, **(env_overrides or {})},
            "delay_input_ms": delay_input_ms,
            "hold_stdin_open": hold_stdin_open,
            "close_stdin_after_ms": close_stdin_after_ms,
        }
        if child_started_path is not None:
            request["child_started_path"] = str(child_started_path)
        host_process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        host_process.stdin.flush()
        response_line = host_process.stdout.readline()
        self.assertTrue(response_line, "AudnCode host exited before raw hook returned")
        response = json.loads(response_line.decode("utf-8"))
        if close_stdin_after_ms > 0:
            self.assertFalse(response.get("exited_before_stdin_close"), response)
        result = subprocess.CompletedProcess(
            command,
            int(response["returncode"]),
            base64.b64decode(response["stdout"]),
            base64.b64decode(response["stderr"]),
        )
        if expect_success:
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )
            self.assertEqual(result.stdout.decode("utf-8").strip(), "{}")
        return result

    def run_audncode_hooks_concurrently(
        self,
        events: list[dict],
        *,
        host: tuple[subprocess.Popen[bytes], int] | None = None,
        env_overrides: list[dict[str, str]] | None = None,
        wait_for_paths: list[Path | None] | None = None,
    ) -> list[subprocess.CompletedProcess[bytes]]:
        self.assertTrue(events, "concurrent AudnCode hook batch must not be empty")
        resolved_env_overrides = (
            [{} for _event in events] if env_overrides is None else env_overrides
        )
        self.assertEqual(len(resolved_env_overrides), len(events))
        resolved_wait_for_paths = (
            [None for _event in events] if wait_for_paths is None else wait_for_paths
        )
        self.assertEqual(len(resolved_wait_for_paths), len(events))
        selected_host = host or self.audncode_hosts.get(
            str(events[0].get("session_id", ""))
        )
        self.assertIsNotNone(selected_host, "AudnCode hooks have no owning host")
        host_process, _host_started_at = selected_host
        self.assertIsNotNone(host_process.stdin)
        self.assertIsNotNone(host_process.stdout)
        commands = [
            self.audncode_hook_command(
                expected_event=str(event.get("hook_event_name", ""))
            )
            for event in events
        ]
        request = {
            "batch": [
                {
                    "command": command,
                    "input": base64.b64encode(
                        json.dumps(event, ensure_ascii=False).encode("utf-8")
                    ).decode("ascii"),
                    "env": {**self.env, **env_override},
                    "wait_for_path": "" if wait_for_path is None else str(wait_for_path),
                }
                for event, env_override, wait_for_path, command in zip(
                    events, resolved_env_overrides, resolved_wait_for_paths, commands
                )
            ]
        }
        host_process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        host_process.stdin.flush()
        response_line = host_process.stdout.readline()
        self.assertTrue(
            response_line,
            "AudnCode host proxy exited before returning concurrent hook results",
        )
        response = json.loads(response_line.decode("utf-8"))
        self.assertIn("batch", response, response)
        self.assertEqual(len(response["batch"]), len(events), response)
        results: list[subprocess.CompletedProcess[bytes]] = []
        for item, command in zip(response["batch"], commands):
            result = subprocess.CompletedProcess(
                command,
                int(item["returncode"]),
                base64.b64decode(item["stdout"]),
                base64.b64decode(item["stderr"]),
            )
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")
            self.assertEqual(
                result.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}"
            )
            self.assertEqual(stdout.strip(), "{}")
            results.append(result)
        return results

    def write_reverse_boundary_transcript(
        self,
        path: Path,
        *,
        prefix: bytes,
        line: bytes,
        target: bytes,
        split_offset: int,
    ) -> None:
        target_index = line.index(target)
        boundary = len(prefix) + target_index + split_offset
        suffix_length = boundary + 65536 - len(prefix) - len(line)
        self.assertGreater(suffix_length, 0)
        suffix_parts: list[bytes] = []
        remaining = suffix_length
        while remaining > 0:
            chunk_length = min(300, remaining)
            suffix_parts.append((b"x" * (chunk_length - 1) + b"\n") if chunk_length > 1 else b"\n")
            remaining -= chunk_length
        data = prefix + line + b"".join(suffix_parts)
        self.assertEqual(len(data) - 65536, boundary)
        path.write_bytes(data)

    def state_debug(self) -> str:
        files = {
            name: [path.name for path in (self.state / name).glob("*.json")]
            for name in (
                "pending",
                "outbox",
                "sent",
                "suppressed",
                "dead",
                "recovery-journals",
            )
        }
        pending_details: list[dict] = []
        for pending_path in (self.state / "pending").glob("*.json"):
            try:
                pending = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                pending_details.append(
                    {
                        "name": pending_path.name,
                        "gate_reason": pending.get("gate_reason"),
                        "next_attempt_unix_ms": pending.get("next_attempt_unix_ms"),
                    }
                )
            except (OSError, json.JSONDecodeError):
                pass
        files["pending_details"] = pending_details
        log_path = self.state / "notify.log"
        try:
            log = log_path.read_text(encoding="utf-8-sig", errors="replace") if log_path.exists() else ""
        except OSError as error:
            log = f"<log temporarily unavailable: {error}>"
        return json.dumps(files) + "\n" + log[-4000:]

    def read_audncode_session_state(self, session_id: str) -> tuple[Path, dict]:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            for path in (self.state / "claude-sessions").glob("*.json"):
                try:
                    state = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    # Atomic replace can briefly remove the destination or race
                    # this read with a still-unflushed replacement.
                    continue
                if state.get("session_id") == session_id and "state" in state:
                    return path, state
            time.sleep(0.01)
        self.fail(f"AudnCode session state not found for {session_id}: {self.state_debug()}")

    def write_audncode_session_state_clone(
        self,
        source_state: dict,
        *,
        session_id: str,
        transcript_path: Path,
    ) -> tuple[Path, dict]:
        state = json.loads(json.dumps(source_state))
        state["session_id"] = session_id
        state["transcript_path"] = str(transcript_path)
        state["audncode_remote_claims_valid"] = True
        state["audncode_remote_claims"] = []
        key = hashlib.sha256(
            f"codex-ntfy/v1|claude-session|{session_id}".encode("utf-8")
        ).hexdigest()
        path = self.state / "claude-sessions" / f"{key}.json"
        path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        return path, state

    def read_audncode_ingress_guard(self, host_pid: int) -> tuple[Path, dict]:
        for path in (self.state / "claude-sessions").glob(
            "audn-lifecycle-ingress-*.json"
        ):
            try:
                state = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("registry_kind") == "ingress" and state.get("host_pid") == host_pid:
                return path, state
        self.fail(f"AudnCode ingress guard not found for PID {host_pid}: {self.state_debug()}")

    def read_json_retry(self, path: Path, *, timeout: float = 2.0) -> dict:
        deadline = time.time() + timeout
        last_error: OSError | json.JSONDecodeError | None = None
        while time.time() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as error:
                last_error = error
                time.sleep(0.01)
        self.fail(f"could not read atomic JSON {path}: {last_error}\n{self.state_debug()}")

    def read_windows_acl_summary(self, path: Path) -> dict:
        script = r"""
$sections = [Security.AccessControl.AccessControlSections]::Owner -bor
  [Security.AccessControl.AccessControlSections]::Group -bor
  [Security.AccessControl.AccessControlSections]::Access
$acl = [IO.File]::GetAccessControl(
  $env:CODEX_NTFY_TEST_ACL_PATH,
  $sections
)
$sddl = $acl.GetSecurityDescriptorSddlForm($sections)
# SetAccessControl/File.Replace can materialize the semantically equivalent
# AutoInherited control bit while preserving owner, group, every ACE, and the
# protected state. Ignore only that Windows bookkeeping bit.
$sddl = $sddl -replace 'D:(P?)(AR)?AI(?=\()', 'D:$1$2'
[pscustomobject]@{
  sddl = $sddl
  protected = [bool]$acl.AreAccessRulesProtected
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={**os.environ, "CODEX_NTFY_TEST_ACL_PATH": str(path)},
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

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

    def rollout_scan_command(self, implementation: str, *, scope: str = "") -> list[str]:
        if implementation == "powershell":
            command = [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(POWERSHELL_NOTIFIER),
                "-ScanRollouts",
            ]
            if scope:
                command.extend(["-ScanScope", scope])
            return command
        return [sys.executable, str(PYTHON_NOTIFIER), "--scan-rollouts"]

    def run_ok(self, command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        return result

    def wait_for_process_health(
        self,
        path: Path,
        process: subprocess.Popen[str],
        statuses: set[str],
        *,
        timeout: float = 30,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        health: dict[str, object] = {}
        while time.monotonic() < deadline:
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                health = value if isinstance(value, dict) else {}
            except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                health = {}
            if str(health.get("status", "")) in statuses or process.poll() is not None:
                break
            time.sleep(0.05)
        return health

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

    def test_compact_payload_contract_is_identical(self) -> None:
        thread_id = "11111111-1111-7111-8111-111111111111"
        turn_id = "22222222-2222-7222-8222-222222222222"
        captured: dict[str, dict] = {}
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id, turn_id=turn_id)))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    captured[implementation] = self.server.payloads.pop()
                shutil.rmtree(self.state, ignore_errors=True)

        expected = {
            "topic": "test-topic",
            "title": "perfect notifier",
            "message": "Fatto: test concorrente completato. · test-host · #11111111",
            "tags": ["white_check_mark"],
            "sequence_id": captured["python"]["sequence_id"],
        }
        self.assertEqual(captured["python"], expected)
        self.assertNotIn("click", captured["python"])
        self.assertNotIn("actions", captured["python"])
        self.assertFalse(
            any(
                word in captured["python"]["title"].lower()
                for word in ("codex", "done", "stopped", "gpt")
            )
        )
        if "powershell" in captured:
            self.assertEqual(captured["powershell"], expected)

    def test_task_link_click_is_opt_in_and_identical(self) -> None:
        thread_id = "77777777-7777-7777-8777-777777777777"
        expected_url = f"https://chatgpt.com/codex/tasks/{thread_id}"
        event = self.event(thread_id=thread_id)
        captured: dict[str, dict] = {}
        self.configure(include_task_link=True, include_task_link_action=False, include_message=False)

        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertEqual(payload["click"], expected_url)
                self.assertNotIn("actions", payload)
                self.assertNotIn(thread_id, payload["title"])
                self.assertNotIn(thread_id, payload["message"])
                captured[implementation] = payload
                shutil.rmtree(self.state, ignore_errors=True)

        if "powershell" in captured:
            python_payload = {key: value for key, value in captured["python"].items() if key != "sequence_id"}
            powershell_payload = {key: value for key, value in captured["powershell"].items() if key != "sequence_id"}
            self.assertEqual(powershell_payload, python_payload)

    def test_task_link_action_requires_a_second_opt_in(self) -> None:
        thread_id = "88888888-8888-7888-8888-888888888888"
        expected_url = f"https://chatgpt.com/codex/tasks/{thread_id}"

        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.configure(include_task_link=False, include_task_link_action=True)
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    disabled_payload = self.server.payloads.pop()
                self.assertNotIn("click", disabled_payload)
                self.assertNotIn("actions", disabled_payload)
                shutil.rmtree(self.state, ignore_errors=True)

                self.configure(include_task_link=True, include_task_link_action=True)
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertEqual(payload["click"], expected_url)
                self.assertEqual(
                    payload["actions"],
                    [
                        {
                            "action": "view",
                            "label": "Open task",
                            "url": expected_url,
                            "clear": True,
                        }
                    ],
                )
                shutil.rmtree(self.state, ignore_errors=True)

    def test_invalid_thread_id_omits_task_link_without_blocking_delivery(self) -> None:
        self.configure(include_task_link=True, include_task_link_action=True)

        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event(thread_id="not-a-canonical-uuid")))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertNotIn("click", payload)
                self.assertNotIn("actions", payload)
                shutil.rmtree(self.state, ignore_errors=True)

    def test_compact_payload_unicode_title_and_byte_budget(self) -> None:
        thread_id = "33333333-3333-7333-8333-333333333333"
        turn_id = "44444444-4444-7444-8444-444444444444"
        title = "Attività già pronta " + ("x" * 38) + " 😀"
        other_id = "55555555-5555-7555-8555-555555555555"
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": thread_id, "thread_name": title}, ensure_ascii=False)
            + "\n"
            + json.dumps({"id": other_id, "thread_name": f"not the target {thread_id}"})
            + "\n",
            encoding="utf-8",
        )
        self.configure(include_thread_title=True, max_message_chars=3000, markdown=False)
        captured: dict[str, dict] = {}
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event(thread_id=thread_id, turn_id=turn_id)
                event["last-assistant-message"] = "😀" * 3000
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertEqual(payload["title"], title)
                self.assertEqual(len(payload["title"]), 60)
                self.assertEqual(payload["tags"], ["white_check_mark"])
                self.assertLessEqual(len(payload["message"].encode("utf-8")), 3500)
                self.assertNotIn("�", payload["message"])
                self.assertTrue(payload["message"].endswith("perfect notifier · test-host · #33333333"))
                captured[implementation] = payload
                shutil.rmtree(self.state, ignore_errors=True)
        if "powershell" in captured and "python" in captured:
            self.assertEqual(captured["powershell"], captured["python"])

    def test_plain_text_payload_compacts_markdown_with_unicode_and_redaction(self) -> None:
        self.configure(markdown=False, max_message_chars=1000)
        thread_id = "66666666-6666-7666-8666-666666666666"
        turn_id = "77777777-7777-7777-8777-777777777777"
        message = (
            "## Risultato **finale**\n"
            "- `Più` già, è ✅\n"
            "- Vedi [guida](https://example.test/private)\n"
            "- `last_assistant_message` `__init__` `a*b*c`\n"
            "- [Riferimento][ref] e <https://example.test/ref>\n"
            "- **outer *inner* text**\n"
            "- \\*letterale\\*\n"
            "- \ue0000\ue001 and `safe_code`\n"
            "```python\n"
            "snake_case = __init__ * a*b*c\n"
            "```\n"
            "| Campo | Valore |\n"
            "| --- | --- |\n"
            "| Stato | pronto |\n"
            "- token=top-secret-value\n"
            "[ref]: https://example.test/reference"
        )
        expected = (
            "Risultato finale · Più già, è ✅ · Vedi guida · "
            "last_assistant_message __init__ a*b*c · "
            "Riferimento e https://example.test/ref · outer inner text · *letterale* · "
            "\ue0000\ue001 and safe_code · "
            "snake_case = __init__ * a*b*c · Campo · Valore · "
            "Stato · pronto · token=[REDACTED]"
        )
        captured: dict[str, dict] = {}
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event(thread_id=thread_id, turn_id=turn_id)
                event["last-assistant-message"] = message
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertTrue(payload["message"].startswith(expected + " · "), payload["message"])
                self.assertNotIn("top-secret-value", payload["message"])
                for marker in ("##", "**", "`", "[guida]", "| ---"):
                    self.assertNotIn(marker, payload["message"])
                self.assertNotIn("markdown", payload)
                captured[implementation] = payload
                shutil.rmtree(self.state, ignore_errors=True)
        if "powershell" in captured:
            self.assertEqual(captured["powershell"], captured["python"])

    def test_turn_aborted_uses_stopped_status_without_redundant_body(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event()
                event["completion-event-type"] = "turn_aborted"
                event["last-assistant-message"] = ""
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertEqual(payload["title"], "perfect notifier")
                self.assertNotIn("aborted", payload["message"].lower())
                self.assertNotIn("completed", payload["message"].lower())
                shutil.rmtree(self.state, ignore_errors=True)

    def test_comma_separated_tags_are_normalized_to_first_status_tag(self) -> None:
        self.configure(tags="white_check_mark,robot")
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertEqual(payload["tags"], ["white_check_mark"])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_existing_custom_tag_sets_are_normalized_to_first_status_tag(self) -> None:
        custom_tags = ["white_check_mark", "robot", "computer", "tada"]
        self.configure(tags=custom_tags)
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event()))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertEqual(payload["tags"], [custom_tags[0]])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_exactly_one_status_tag_for_every_terminal_outcome(self) -> None:
        self.configure(tags=["tada", "robot"])
        cases = (
            ("", "task_complete", "tada"),
            ("complete", "task_complete", "tada"),
            ("achieved", "task_complete", "tada"),
            ("active", "task_complete", "warning"),
            ("paused", "task_complete", "warning"),
            ("blocked", "task_complete", "warning"),
            ("usage_limited", "task_complete", "warning"),
            ("budget_limited", "task_complete", "warning"),
            ("", "turn_aborted", "warning"),
        )
        for implementation in self.implementations():
            for goal_status, completion_type, expected in cases:
                with self.subTest(
                    implementation=implementation,
                    goal_status=goal_status,
                    completion_type=completion_type,
                ):
                    event = self.event(thread_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()))
                    event["goal-status"] = goal_status
                    event["completion-event-type"] = completion_type
                    self.run_ok(self.hook_command(implementation, event))
                    self.run_ok(self.worker_command(implementation))
                    with self.server.lock:
                        payload = self.server.payloads.pop()
                    self.assertEqual(payload["tags"], [expected])
                    self.assertEqual(len(payload["tags"]), 1)
                    shutil.rmtree(self.state, ignore_errors=True)

    def test_task_link_is_codex_only_with_legacy_record_compatibility(self) -> None:
        self.configure(include_task_link=True, include_task_link_action=True, include_message=False)
        cases = ((None, True), ("codex", True), ("claude", False), ("audncode", False), ("unknown", False))
        for implementation in self.implementations():
            for provider, expected_link in cases:
                with self.subTest(implementation=implementation, provider=provider):
                    thread_id = str(uuid.uuid4())
                    self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                    record_path = next((self.state / "outbox").glob("*.json"))
                    record = json.loads(record_path.read_text(encoding="utf-8-sig"))
                    if provider is None:
                        record.pop("provider", None)
                    else:
                        record["provider"] = provider
                    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                    self.run_ok(self.worker_command(implementation))
                    with self.server.lock:
                        payload = self.server.payloads.pop()
                    self.assertEqual("click" in payload, expected_link)
                    self.assertEqual("actions" in payload, expected_link)
                    if expected_link:
                        self.assertEqual(payload["click"], f"https://chatgpt.com/codex/tasks/{thread_id}")
                    shutil.rmtree(self.state, ignore_errors=True)

    def test_unicode_title_is_nfc_control_safe_grapheme_safe_and_byte_bounded(self) -> None:
        thread_id = str(uuid.uuid4())
        unsafe_title = "Cafe\u0301 \u202ebad\u2066 \ufeff " + ("👩🏽‍💻" * 40) + "\x01"
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": thread_id, "thread_name": unsafe_title}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.configure(include_thread_title=True)
        captured: dict[str, str] = {}
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    title = str(self.server.payloads.pop()["title"])
                self.assertLessEqual(len(title.encode("utf-8")), 240)
                self.assertIn("Café", title)
                for forbidden in ("\u202e", "\u2066", "\ufeff", "\x01", "�"):
                    self.assertNotIn(forbidden, title)
                self.assertFalse(title.endswith(("\u200c", "\u200d")))
                captured[implementation] = title
                shutil.rmtree(self.state, ignore_errors=True)
        if "powershell" in captured:
            self.assertEqual(captured["powershell"], captured["python"])

    def test_single_oversize_grapheme_title_falls_back_to_project(self) -> None:
        thread_id = str(uuid.uuid4())
        giant_cluster = "a" + ("\u0301" * 1000)
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": thread_id, "thread_name": giant_cluster}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.configure(include_thread_title=True)
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertEqual(payload["title"], "perfect notifier")
                shutil.rmtree(self.state, ignore_errors=True)

    def test_sqlite_blob_titles_use_strict_utf8_or_fall_back(self) -> None:
        self.configure(include_thread_title=True)
        valid_title = "Attività SQLite — 👩🏽‍💻"
        for implementation in self.implementations():
            for raw_title, expected in (
                (valid_title.encode("utf-8"), valid_title),
                (b"invalid-utf8-\xff", "perfect notifier"),
            ):
                with self.subTest(implementation=implementation, expected=expected):
                    thread_id = str(uuid.uuid4())
                    rollout = self.write_session_meta(thread_id, subagent=False)
                    connection = sqlite3.connect(self.state_database)
                    try:
                        connection.execute(
                            "INSERT OR REPLACE INTO threads(id, rollout_path, source, thread_source, title) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (thread_id, str(rollout), "vscode", "user", sqlite3.Binary(raw_title)),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                    self.run_ok(self.worker_command(implementation))
                    with self.server.lock:
                        payload = self.server.payloads.pop()
                    self.assertEqual(payload["title"], expected)
                    self.assertNotIn("b'", payload["title"])
                    self.assertNotIn("�", payload["title"])
                    shutil.rmtree(self.state, ignore_errors=True)

    def test_json_body_special_characters_do_not_become_headers(self) -> None:
        message = "Risultato: già ‘ok’ \\\"quoted\\\" \\\\ path\r\nX-Ntfy-Injection: no 👩🏽‍💻"
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event(thread_id=str(uuid.uuid4()))
                event["last-assistant-message"] = message
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                    raw = self.server.raw_bodies.pop()
                    headers = self.server.request_headers.pop()
                self.assertEqual(json.loads(raw.decode("utf-8", errors="strict")), payload)
                self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                self.assertNotIn("x-ntfy-injection", headers)
                self.assertIn("già", payload["message"])
                self.assertNotIn("�", payload["message"])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_auth_header_injection_is_rejected_before_local_publish(self) -> None:
        self.configure(token="safe\r\nX-Ntfy-Injection: yes", max_attempts=1)
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event(thread_id=str(uuid.uuid4()))))
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                    self.assertEqual(self.server.request_headers, [])
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

                process = subprocess.Popen(
                    self.continuous_worker_command(implementation),
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    time.sleep(0.35)
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    self.append_rollout(rollout, "user_message", message="Automatic continuation")
                    self.append_rollout(rollout, "task_complete", turn_id=final_turn, message="Final")
                    time.sleep(0.02)
                    self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id, turn_id=final_turn)))
                    payloads = self.wait_for_payloads(1, timeout=45)
                    self.assertEqual(len(payloads), 1)
                finally:
                    stdout, stderr = self.stop_continuous_worker(process)
                    self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")

                suppressed = [json.loads(path.read_text(encoding="utf-8-sig")) for path in (self.state / "suppressed").glob("*.json")]
                self.assertTrue(any(receipt.get("reason") == "superseded" for receipt in suppressed))
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows PowerShell test")
    def test_coalesce_retries_after_legacy_revision_migration_without_double_delivery(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=True,
        )
        thread_id = str(uuid.uuid4())
        old_turn = "00000000-0000-7000-8000-000000000101"
        new_turn = "00000000-0000-7000-8000-000000000102"
        rollout = self.write_session_meta(thread_id, subagent=False)
        for turn_id, message in (
            (old_turn, "LEGACY INTERMEDIATE"),
            (new_turn, "MODERN FINAL"),
        ):
            self.append_rollout(rollout, "task_started", turn_id=turn_id)
            self.append_rollout(rollout, "user_message", message="Continue")
            self.append_rollout(
                rollout,
                "task_complete",
                turn_id=turn_id,
                message=message,
            )
            event = self.event(thread_id=thread_id, turn_id=turn_id)
            event["last-assistant-message"] = message
            self.run_ok(self.hook_command("powershell", event))

        pending_by_turn: dict[str, tuple[Path, dict]] = {}
        for path in (self.state / "pending").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            pending_by_turn[str(record["turn_id"])] = (path, record)
        self.assertEqual(set(pending_by_turn), {old_turn, new_turn}, self.state_debug())
        old_path, old_record = pending_by_turn[old_turn]
        new_path, new_record = pending_by_turn[new_turn]
        self.assertRegex(new_record["candidate_revision"], r"^[0-9a-f]{32}$")
        old_record.pop("candidate_revision", None)
        old_path.write_text(
            json.dumps(old_record, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )

        # The one-shot worker must retry coalescing after it atomically adds the
        # legacy token; only the reread snapshot may suppress the predecessor.
        self.run_ok(self.worker_command("powershell"), timeout=60)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("MODERN FINAL", payloads[0]["message"])
        self.assertNotIn("LEGACY INTERMEDIATE", payloads[0]["message"])
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

        suppressed_paths = list((self.state / "suppressed").glob("*.json"))
        sent_paths = list((self.state / "sent").glob("*.json"))
        self.assertEqual(len(suppressed_paths), 1, self.state_debug())
        self.assertEqual(len(sent_paths), 1, self.state_debug())
        suppression = json.loads(
            suppressed_paths[0].read_text(encoding="utf-8-sig")
        )
        sent = json.loads(sent_paths[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(suppression["schema"], 2, suppression)
        self.assertEqual(suppression["reason"], "superseded", suppression)
        self.assertEqual(suppression["key"], old_record["key"], suppression)
        self.assertRegex(suppression["candidate_revision"], r"^[0-9a-f]{32}$")
        self.assertEqual(sent["key"], new_record["key"], sent)
        self.assertFalse(old_path.exists(), self.state_debug())
        self.assertFalse(new_path.exists(), self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "dead").glob("*.json")), self.state_debug())

        log = (self.state / "notify.log").read_text(
            encoding="utf-8-sig", errors="replace"
        )
        migration_entry = (
            "migrated legacy queue candidate revision "
            f"key={str(old_record['key'])[:12]}"
        )
        superseded_entry = (
            f"superseded idle candidate key={str(old_record['key'])[:12]}"
        )
        self.assertEqual(log.count(migration_entry), 1, log)
        # A premature declaration on the migration attempt would yield a second
        # superseded line before the sole durable receipt is eventually written.
        self.assertEqual(log.count(superseded_entry), 1, log)
        self.assertLess(log.index(migration_entry), log.index(superseded_entry), log)
        self.assertNotIn(
            f"superseded idle candidate key={str(new_record['key'])[:12]}",
            log,
        )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_question_intervention_uses_session_then_record_lock_order(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Prepare the lock-order question")
        )
        question_text = "Confermi il deploy concorrente?"
        question = self.append_audncode_question(event, question=question_text)
        session_state_path, session_state = self.read_audncode_session_state(session_id)
        session_state["prompt_id"] = question["root_uuid"]
        session_state_path.write_text(
            json.dumps(session_state, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        permission = self.audncode_idle_event(
            event, notification_type="permission_prompt"
        )

        # Seed the exact intervention record, then place it back in pending to
        # model a worker promotion racing a duplicate permission_prompt for the
        # same proof/key.
        self.run_audncode_hook(permission)
        outbox_paths = list((self.state / "outbox").glob("*.json"))
        self.assertEqual(len(outbox_paths), 1, self.state_debug())
        intervention = json.loads(outbox_paths[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(intervention["candidate_kind"], "audncode_intervention")
        pending_path = self.state / "pending" / outbox_paths[0].name
        os.replace(outbox_paths[0], pending_path)

        record_lock = (
            self.state
            / "mutation-locks"
            / f"{str(intervention['key'])[:2]}.lock"
        )
        session_key = hashlib.sha256(
            f"codex-ntfy/v1|claude-session|{session_id}".encode("utf-8")
        ).hexdigest()
        session_lock = self.state / "claude-sessions" / f"{session_key}.lock"
        holder_marker = self.temp / "question-record-lock-held.marker"
        holder_release = self.temp / "question-record-lock.release"
        final_gate_marker = self.temp / "question-final-gate.marker"
        final_gate_release = self.temp / "question-final-gate.release"
        holder_command = [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            r"""
$stream = $null
try {
  $stream = [IO.File]::Open(
    $env:CODEX_NTFY_TEST_LOCK_PATH,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None
  )
  [IO.File]::WriteAllText(
    $env:CODEX_NTFY_TEST_LOCK_MARKER,
    'locked',
    (New-Object Text.UTF8Encoding($false))
  )
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
  while (-not (Test-Path -LiteralPath $env:CODEX_NTFY_TEST_LOCK_RELEASE -PathType Leaf) -and
      [DateTimeOffset]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 10
  }
} finally {
  if ($null -ne $stream) { $stream.Dispose() }
}
""",
        ]
        holder_env = {
            **self.env,
            "CODEX_NTFY_TEST_LOCK_PATH": str(record_lock),
            "CODEX_NTFY_TEST_LOCK_MARKER": str(holder_marker),
            "CODEX_NTFY_TEST_LOCK_RELEASE": str(holder_release),
        }
        holder: subprocess.Popen[str] | None = None
        worker: subprocess.Popen[str] | None = None
        hook_thread: threading.Thread | None = None
        hook_results: list[subprocess.CompletedProcess[bytes]] = []
        hook_errors: list[BaseException] = []

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        invalid_handle = wintypes.HANDLE(-1).value

        def session_lock_is_held() -> bool:
            handle = create_file(
                str(session_lock),
                0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
                0,
                None,
                4,  # OPEN_ALWAYS
                0x00000080,  # FILE_ATTRIBUTE_NORMAL
                None,
            )
            if handle == invalid_handle:
                return True
            self.assertTrue(close_handle(handle))
            return False

        def launch_permission_hook() -> None:
            try:
                hook_results.append(
                    self.run_audncode_hook(
                        permission,
                        child_started_path=hook_started,
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                hook_errors.append(error)

        hook_started = self.temp / "question-permission-hook.pid"
        try:
            worker = subprocess.Popen(
                self.worker_command("powershell"),
                env={
                    **self.env,
                    "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS": "10000",
                    "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER": str(
                        final_gate_marker
                    ),
                    "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE": str(
                        final_gate_release
                    ),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not final_gate_marker.exists():
                self.assertIsNone(worker.poll(), self.state_debug())
                time.sleep(0.01)
            self.assertTrue(final_gate_marker.exists(), self.state_debug())

            holder = subprocess.Popen(
                holder_command,
                env=holder_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not holder_marker.exists():
                time.sleep(0.01)
            self.assertTrue(holder_marker.exists(), self.state_debug())
            final_gate_release.write_text("continue", encoding="ascii")

            deadline = time.monotonic() + 10
            worker_holds_session = False
            while time.monotonic() < deadline:
                if session_lock_is_held():
                    worker_holds_session = True
                    break
                self.assertIsNone(worker.poll(), self.state_debug())
                time.sleep(0.01)
            self.assertTrue(worker_holds_session, self.state_debug())

            hook_thread = threading.Thread(
                target=launch_permission_hook,
                daemon=True,
            )
            hook_thread.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not hook_started.exists():
                time.sleep(0.01)
            self.assertTrue(hook_started.exists(), self.state_debug())
            time.sleep(0.15)
            self.assertTrue(hook_thread.is_alive(), self.state_debug())
            self.assertTrue(session_lock_is_held(), self.state_debug())
            holder_release.write_text("release", encoding="ascii")

            assert holder is not None
            holder_stdout, holder_stderr = holder.communicate(timeout=10)
            self.assertEqual(
                holder.returncode,
                0,
                f"stdout={holder_stdout}\nstderr={holder_stderr}",
            )
            hook_thread.join(timeout=20)
            self.assertFalse(hook_thread.is_alive(), self.state_debug())
            if hook_errors:
                raise hook_errors[0]
            self.assertEqual(len(hook_results), 1, self.state_debug())
            self.assertEqual(hook_results[0].returncode, 0)
            worker_stdout, worker_stderr = worker.communicate(timeout=30)
            self.assertEqual(
                worker.returncode,
                0,
                f"stdout={worker_stdout}\nstderr={worker_stderr}\n{self.state_debug()}",
            )
        finally:
            if not final_gate_release.exists():
                final_gate_release.write_text("continue", encoding="ascii")
            if not holder_release.exists():
                holder_release.write_text("release", encoding="ascii")
            if holder is not None and holder.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    holder.communicate(timeout=5)
            if hook_thread is not None and hook_thread.is_alive():
                hook_thread.join(timeout=5)
            if worker is not None and worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=10)

        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertEqual(payloads[0]["tags"], ["question"])
        self.assertIn(question_text, payloads[0]["message"])
        sent_paths = list((self.state / "sent").glob("*.json"))
        self.assertEqual(len(sent_paths), 1, self.state_debug())
        sent = json.loads(sent_paths[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(sent["key"], intervention["key"], sent)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "dead").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "suppressed").glob("*.json")), self.state_debug())

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
                self.run_ok(self.worker_command(implementation), timeout=60)
                payloads = self.wait_for_payloads(1)
                debug_state = {
                    name: [path.name for path in (self.state / name).glob("*.json")]
                    for name in ("pending", "outbox", "sent", "suppressed", "dead")
                }
                debug_log = (self.state / "notify.log").read_text(encoding="utf-8-sig", errors="replace")
                self.assertEqual(len(payloads), 1, msg=f"state={debug_state}\nlog={debug_log}")
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

    def test_multiple_pending_candidates_share_a_rollout_without_losing_the_oldest(self) -> None:
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
                turns = [
                    "00000000-0000-7000-8000-000000000013",
                    "00000000-0000-7000-8000-000000000014",
                    "00000000-0000-7000-8000-000000000015",
                ]
                messages = ["FIRST INTERMEDIATE", "SECOND INTERMEDIATE", "ONLY FINAL"]
                rollout = self.write_session_meta(thread_id, subagent=False)
                for turn_id, message in zip(turns, messages, strict=True):
                    self.append_rollout(rollout, "task_started", turn_id=turn_id)
                    self.append_rollout(rollout, "user_message", message="Continue")
                    self.append_rollout(rollout, "task_complete", turn_id=turn_id, message=message)
                for turn_id, message in zip(turns[:2], messages[:2], strict=True):
                    event = self.event(thread_id=thread_id, turn_id=turn_id)
                    event["last-assistant-message"] = message
                    self.run_ok(self.hook_command(implementation, event))
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 2)
                self.run_ok(self.worker_command(implementation), timeout=60)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertIn("ONLY FINAL", payloads[0]["message"])
                self.assertNotIn("INTERMEDIATE", payloads[0]["message"])
                suppressed = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertGreaterEqual(sum(receipt.get("reason") == "superseded" for receipt in suppressed), 2)
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
                    payloads = self.wait_for_payloads(1, timeout=45)
                    self.assertEqual(len(payloads), 1)
                    self.assertIn("NEWEST", payloads[0]["message"])
                    self.assertNotIn("STALE", payloads[0]["message"])
                    time.sleep(0.2)
                    with self.server.lock:
                        self.assertEqual(len(self.server.payloads), 1)
                finally:
                    stdout, stderr = self.stop_continuous_worker(process)
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
                self.assertEqual(
                    {payload["message"].split(" · ", 1)[0] for payload in payloads},
                    {"FIRST", "SECOND"},
                )
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

                self.run_ok(self.worker_command(implementation), timeout=60)
                payloads = self.wait_for_payloads(2)
                self.assertEqual(len(payloads), 2)
                messages = {payload["message"].split(" · ", 1)[0] for payload in payloads}
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
                self.run_ok(self.worker_command(implementation), timeout=60)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                receipts = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertTrue(any(receipt.get("reason") == "technical-turn" for receipt in receipts))
                shutil.rmtree(self.state, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows PowerShell test")
    def test_legacy_queue_candidate_revision_migrates_before_terminal_disposition(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )

        def enqueue_technical_candidate(label: str) -> Path:
            thread_id = str(uuid.uuid4())
            turn_id = str(uuid.uuid4())
            rollout = self.write_session_meta(thread_id, subagent=False)
            self.append_rollout(rollout, "task_started", turn_id=turn_id)
            # No user_message: the strict gate deterministically classifies it
            # as a technical turn once it sees the terminal rollout event.
            self.append_rollout(
                rollout,
                "task_complete",
                turn_id=turn_id,
                message=label,
            )
            event = self.event(thread_id=thread_id, turn_id=turn_id)
            event["last-assistant-message"] = label
            self.run_ok(self.hook_command("powershell", event))
            candidates = list((self.state / "pending").glob("*.json"))
            self.assertEqual(len(candidates), 1, self.state_debug())
            return candidates[0]

        pending_path = enqueue_technical_candidate("LEGACY REVISION MIGRATION")
        legacy = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        legacy.pop("candidate_revision", None)
        pending_path.write_text(
            json.dumps(legacy, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )

        migration = subprocess.run(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_EXIT_AFTER_CANDIDATE_REVISION_MIGRATION": "1",
            },
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            migration.returncode,
            94,
            f"stdout={migration.stdout}\nstderr={migration.stderr}\n{self.state_debug()}",
        )
        migrated = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        revision = migrated.pop("candidate_revision")
        self.assertRegex(revision, r"^[0-9a-f]{32}$")
        self.assertEqual(migrated, legacy)
        self.assertTrue(pending_path.exists(), self.state_debug())
        self.assertFalse(
            list((self.state / "suppressed").glob("*.json")),
            self.state_debug(),
        )
        self.assertFalse(list(pending_path.parent.glob("*.tmp")), self.state_debug())
        migration_log = (self.state / "notify.log").read_text(
            encoding="utf-8-sig", errors="replace"
        )
        self.assertIn("migrated legacy queue candidate revision", migration_log)
        self.assertNotIn("suppressed technical turn", migration_log)

        # The next pass rereads the durable revision and may now apply the
        # technical-turn disposition. It must terminate rather than spin.
        second_started = time.monotonic()
        self.run_ok(self.worker_command("powershell"), timeout=30)
        self.assertLess(time.monotonic() - second_started, 15)
        self.assertFalse(pending_path.exists(), self.state_debug())
        receipt_path = self.state / "suppressed" / pending_path.name
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(receipt["schema"], 2, receipt)
        self.assertEqual(receipt["reason"], "technical-turn", receipt)
        self.assertEqual(receipt["candidate_revision"], revision, receipt)
        log = (self.state / "notify.log").read_text(
            encoding="utf-8-sig", errors="replace"
        )
        self.assertEqual(log.count("migrated legacy queue candidate revision"), 1)

        # A nonblank malformed revision is corruption, not a legacy record.
        # Assert-QueuedRecord must dead-letter it and let the one-shot worker exit.
        receipt_path.unlink()
        malformed_path = enqueue_technical_candidate("MALFORMED REVISION")
        malformed = json.loads(malformed_path.read_text(encoding="utf-8-sig"))
        malformed["candidate_revision"] = "not-a-hex-revision"
        malformed_path.write_text(
            json.dumps(malformed, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        malformed_started = time.monotonic()
        self.run_ok(self.worker_command("powershell"), timeout=30)
        self.assertLess(time.monotonic() - malformed_started, 15)
        self.assertFalse(malformed_path.exists(), self.state_debug())
        dead_path = self.state / "dead" / malformed_path.name
        dead = json.loads(dead_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(dead["last_error"], "invalid queue JSON", dead)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    def test_escaped_whitespace_is_not_a_final_message(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000042"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="User request")
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="\n\t")
                event = self.event(thread_id=thread_id, turn_id=turn_id)
                event["last-assistant-message"] = "\n\t"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertIn("STOP AUTHORITATIVE", payloads[0]["message"])
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_modern_stop_infers_an_aborted_rollout(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=True,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000044"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="Stop this task")
                self.append_rollout(rollout, "turn_aborted", turn_id=turn_id)
                stop = {
                    "hook_event_name": "Stop",
                    "session_id": thread_id,
                    "turn_id": turn_id,
                    "cwd": "C:\\work\\perfect notifier",
                    "last_assistant_message": "",
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
                self.run_ok(self.worker_command(implementation), timeout=60)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(payloads[0]["title"], "perfect notifier")
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_same_turn_complete_then_abort_preserves_the_completion_candidate(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            suppress_technical_turns=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0000-7000-8000-000000000045"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                self.append_rollout(rollout, "user_message", message="Finish, then stop")
                self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="FINAL ANSWER")
                self.append_rollout(rollout, "turn_aborted", turn_id=turn_id)
                event = self.event(thread_id=thread_id, turn_id=turn_id)
                event["last-assistant-message"] = "FINAL ANSWER"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=60)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1)
                self.assertIn("FINAL ANSWER", payloads[0]["message"])
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
                payloads = self.wait_for_payloads(1)
                self.assertEqual(len(payloads), 1, msg=json.dumps(payloads, ensure_ascii=False, indent=2))
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                    deadline = time.monotonic() + 60
                    observed_wait = False
                    observed_reasons: set[str] = set()
                    while time.monotonic() < deadline and not observed_wait:
                        for pending_path in (self.state / "pending").glob("*.json"):
                            try:
                                pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                            except (OSError, json.JSONDecodeError):
                                continue
                            reason = str(pending_record.get("idle_reason") or pending_record.get("gate_reason") or "")
                            if reason:
                                observed_reasons.add(reason)
                            observed_wait = reason in {
                                "candidate-task-complete-not-observed",
                                "probe-incomplete",
                                "rollout-changing",
                                "turn-active",
                            }
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertTrue(
                        observed_wait,
                        f"worker did not inspect the incomplete JSONL record; reasons={sorted(observed_reasons)} "
                        f"exit={process.poll()}",
                    )
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    with rollout.open("ab") as handle:
                        handle.write(terminal_line[split:])
                    self.assert_worker_ok(process, timeout=60)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                payloads = self.wait_for_payloads(1)
                diagnostic = {
                    str(path.relative_to(self.state)): path.read_text(encoding="utf-8-sig", errors="replace")[-2000:]
                    for path in self.state.rglob("*")
                    if path.is_file() and path.stat().st_size < 2_000_000
                }
                self.assertEqual(len(payloads), 1, json.dumps(diagnostic, indent=2))
                persisted = "\n".join(
                    path.read_text(encoding="utf-8-sig", errors="replace")
                    for path in self.state.rglob("*.json")
                )
                self.assertNotIn("LARGE-PRIVATE-", persisted)
                shutil.rmtree(self.state, ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_malformed_terminal_cannot_close_a_later_open_turn(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            idle_probe_grace_seconds=2,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                completed_turn = "00000000-0000-7000-8000-000000000072"
                open_turn = "00000000-0000-7000-8000-000000000073"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=completed_turn)
                self.append_rollout(rollout, "user_message", message="Initial request")
                self.append_rollout(rollout, "task_complete", turn_id=completed_turn, message="INTERMEDIATE")
                self.append_rollout(rollout, "task_started", turn_id=open_turn)
                with rollout.open("a", encoding="utf-8") as handle:
                    handle.write(
                        '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"'
                        + open_turn
                        + '"}} trailing garbage\n'
                    )
                event = self.event(thread_id=thread_id, turn_id=completed_turn)
                event["last-assistant-message"] = "INTERMEDIATE"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=120)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                records = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for directory in ("pending", "suppressed")
                    for path in (self.state / directory).glob("*.json")
                ]
                self.assertTrue(records, self.state_debug())
                self.assertTrue(
                    any(
                        (record.get("gate_reason") or record.get("reason"))
                        in {"rollout-probe-failed", "unverifiable"}
                        for record in records
                    ),
                    self.state_debug(),
                )
                shutil.rmtree(self.state, ignore_errors=True)

    def test_unicode_escaped_lifecycle_start_blocks_intermediate_completion(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            idle_probe_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                completed_turn = "00000000-0000-7000-8000-000000000076"
                open_turn = "00000000-0000-7000-8000-000000000077"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=completed_turn)
                self.append_rollout(rollout, "user_message", message="Initial request")
                self.append_rollout(rollout, "task_complete", turn_id=completed_turn, message="INTERMEDIATE")
                with rollout.open("a", encoding="utf-8") as handle:
                    handle.write(
                        '{"type":"event_msg","payload":{"type":"task_\\u0073tarted","turn_id":"'
                        + open_turn
                        + '"}}\n'
                    )
                self.index_thread_rollout(thread_id, rollout, subagent=False)
                event = self.event(thread_id=thread_id, turn_id=completed_turn)
                event["last-assistant-message"] = "INTERMEDIATE"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=60)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_partial_later_turn_cannot_release_completed_candidate(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                completed_turn = "00000000-0000-7000-8000-000000000074"
                later_turn = "00000000-0000-7000-8000-000000000075"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=completed_turn)
                self.append_rollout(rollout, "user_message", message="Initial request")
                self.append_rollout(rollout, "task_complete", turn_id=completed_turn, message="COMPLETED A")
                later_line = (
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": later_turn}})
                    + "\n"
                ).encode("utf-8")
                split = len(later_line) // 2
                with rollout.open("ab") as handle:
                    handle.write(later_line[:split])

                event = self.event(thread_id=thread_id, turn_id=completed_turn)
                event["last-assistant-message"] = "COMPLETED A"
                self.run_ok(self.hook_command(implementation, event))
                process = self.start_worker(implementation)
                try:
                    deadline = time.monotonic() + 90
                    observed_reason = ""
                    while time.monotonic() < deadline and observed_reason != "probe-incomplete":
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
                    self.assertEqual(observed_reason, "probe-incomplete", self.state_debug())
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    with rollout.open("ab") as handle:
                        handle.write(later_line[split:])
                    self.assert_worker_ok(process, timeout=120)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                receipts = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertTrue(any(receipt.get("reason") == "superseded" for receipt in receipts), self.state_debug())
                shutil.rmtree(self.state, ignore_errors=True)

    def test_invalid_later_start_cannot_release_completed_candidate(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="balanced",
            idle_grace_seconds=0,
            idle_probe_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                completed_turn = "00000000-0000-7000-8000-000000000076"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=completed_turn)
                self.append_rollout(rollout, "user_message", message="Initial request")
                self.append_rollout(rollout, "task_complete", turn_id=completed_turn, message="COMPLETED A")
                with rollout.open("a", encoding="utf-8") as handle:
                    handle.write('{"type":"event_msg","payload":"task_started"}\n')

                event = self.event(thread_id=thread_id, turn_id=completed_turn)
                event["last-assistant-message"] = "must never be sent"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=120)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
                receipts = list((self.state / "suppressed").glob("*.json"))
                self.assertEqual(len(receipts), 1, self.state_debug())
                receipt = json.loads(receipts[0].read_text(encoding="utf-8-sig"))
                self.assertEqual(receipt.get("reason"), "unverifiable")
                shutil.rmtree(self.state, ignore_errors=True)

    def test_invalid_descendant_lifecycle_cannot_release_root_in_balanced_mode(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="balanced",
            idle_grace_seconds=0,
            idle_probe_grace_seconds=0,
            goal_poll_seconds=0.05,
            subagent_orphan_seconds=60,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                root_id = str(uuid.uuid4())
                child_id = str(uuid.uuid4())
                root_turn = "00000000-0000-7000-8000-000000000077"
                child_turn = "00000000-0000-7000-8000-000000000078"
                root_rollout = self.write_session_meta(root_id, subagent=False)
                child_rollout = self.write_session_meta(child_id, subagent=True)
                self.append_rollout(root_rollout, "task_started", turn_id=root_turn)
                self.append_rollout(root_rollout, "user_message", message="Wait for the child")
                self.append_rollout(root_rollout, "task_complete", turn_id=root_turn, message="ROOT PRIVATE")
                self.append_rollout(child_rollout, "task_started", turn_id=child_turn)
                self.append_rollout(child_rollout, "task_complete", turn_id=child_turn, message="CHILD PRIVATE")
                with child_rollout.open("a", encoding="utf-8") as handle:
                    handle.write('{"type":"event_msg","payload":"task_started"}\n')
                database = self.create_state_database(root_id, root_rollout, child_id, child_rollout)

                event = self.event(thread_id=root_id, turn_id=root_turn)
                event["last-assistant-message"] = "must never be sent"
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=120)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
                receipts = list((self.state / "suppressed").glob("*.json"))
                self.assertEqual(len(receipts), 1, self.state_debug())
                receipt = json.loads(receipts[0].read_text(encoding="utf-8-sig"))
                self.assertEqual(receipt.get("reason"), "unverifiable")

                shutil.rmtree(self.state, ignore_errors=True)
                database.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows lifecycle summary test")
    def test_powershell_fast_rollout_summary_has_constant_cardinality(self) -> None:
        notifier_source = POWERSHELL_NOTIFIER.read_text(encoding="utf-8")
        self.assertNotIn("lifecycleLines", notifier_source)
        self.assertNotIn("$lineIndex = 22", notifier_source)
        incremental_probe_source = notifier_source.split("function Update-RolloutProbe {", 1)[1].split(
            "function Get-FastRolloutProbe {", 1
        )[0]
        latest_probe_source = notifier_source.split("function Get-FastRolloutLatestProbe {", 1)[1].split(
            "function New-GateResult {", 1
        )[0]
        self.assertNotIn("$combined = New-Object byte[] ($carry.Length + $count)", incremental_probe_source)
        self.assertIn("$MaxFallbackRolloutLineBytes", incremental_probe_source)
        self.assertIn("$state.terminalTurns[$latestTurn] = $latestSequence", latest_probe_source)
        self.assertIn("$state.terminalEventTypes[$latestTurn] = $latestType", latest_probe_source)

        thread_id = str(uuid.uuid4())
        candidate_turn = "00000000-0000-7000-8000-00000000c001"
        open_turn = "00000000-0000-7000-8000-00000000c002"
        private_prefix = "PRIVATE_SUMMARY_SENTINEL"
        boundary_message = private_prefix + ("A" * (8_191 - len(private_prefix))) + "😀TAIL"
        rollout = self.write_session_meta(thread_id, subagent=False)

        def lifecycle(payload: dict[str, object]) -> str:
            return json.dumps(
                {"type": "event_msg", "payload": payload},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"

        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "text": '{"type":"event_msg","payload":{"type":"task_complete"}} HISTORY_SENTINEL',
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"kind": "event_msg", "state": "task_complete"},
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            for index in range(4_000):
                historical_turn = f"00000000-0000-7000-8000-{index:012x}"
                handle.write(lifecycle({"type": "task_started", "turn_id": historical_turn}))
                handle.write(lifecycle({"type": "user_message", "message": "HISTORY_SENTINEL"}))
                handle.write(
                    lifecycle(
                        {
                            "type": "task_complete",
                            "turn_id": historical_turn,
                            "last_agent_message": "HISTORY_SENTINEL",
                        }
                    )
                )
            handle.write(lifecycle({"type": "task_started", "turnId": candidate_turn}))
            handle.write(lifecycle({"type": "user_message", "message": "Final request"}))
            handle.write(
                lifecycle(
                    {
                        "type": "task_complete",
                        "turnId": candidate_turn,
                        "last_agent_message": boundary_message,
                    }
                )
            )
            handle.write(lifecycle({"type": "task_started", "turnId": open_turn}))
            handle.write(lifecycle({"type": "thread_goal_updated", "goal": {"status": "active"}}))

        script = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$notifier = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_NOTIFIER)
$pattern = "(?s)Add-Type\s+-ReferencedAssemblies\s+'System\.Web\.Extensions'\s+-TypeDefinition\s+@'\r?\n(?<code>.*?)\r?\n'@"
$match = [regex]::Match($notifier, $pattern)
if (-not $match.Success) { throw 'embedded lifecycle scanner source unavailable' }
Add-Type -ReferencedAssemblies 'System.Web.Extensions' -TypeDefinition $match.Groups['code'].Value
$values = [CodexNtfyWinSqlite]::ScanLifecycleSummary(
  $env:CODEX_NTFY_TEST_ROLLOUT,
  $env:CODEX_NTFY_TEST_CANDIDATE
)
$privateValues = [CodexNtfyWinSqlite]::ScanLifecycleSummary(
  $env:CODEX_NTFY_TEST_ROLLOUT,
  $env:CODEX_NTFY_TEST_CANDIDATE,
  $false
)
[IO.File]::AppendAllText($env:CODEX_NTFY_TEST_ROLLOUT, '{"type":"event_msg","payload":{"type":"task_started"')
$partialValues = [CodexNtfyWinSqlite]::ScanLifecycleSummary(
  $env:CODEX_NTFY_TEST_ROLLOUT,
  $env:CODEX_NTFY_TEST_CANDIDATE,
  $false
)
[pscustomobject]@{
  count = $values.Count
  candidate_type = [string]$values[0]
  candidate_has_user = [string]$values[2]
  open_later_turn = [string]$values[9]
  goal_status = [string]$values[11]
  contains_history = (($values -join "`n") -like '*HISTORY_SENTINEL*')
  candidate_message_utf16_length = ([string]$values[1]).Length
  candidate_message_has_replacement = ([string]$values[1]).Contains([char]0xFFFD)
  private_count = $privateValues.Count
  private_contains_message = ((@($privateValues[1], $privateValues[7], $privateValues[20], $privateValues[21]) -join "`n") -like '*PRIVATE_SUMMARY_SENTINEL*')
  partial_count = $partialValues.Count
  partial_incomplete = [string]$partialValues[22]
} | ConvertTo-Json -Compress
"""
        env = {
            **self.env,
            "CODEX_NTFY_TEST_NOTIFIER": str(POWERSHELL_NOTIFIER),
            "CODEX_NTFY_TEST_ROLLOUT": str(rollout),
            "CODEX_NTFY_TEST_CANDIDATE": candidate_turn,
        }
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        summary = json.loads(result.stdout)
        self.assertEqual(summary["count"], 23)
        self.assertEqual(summary["candidate_type"], "task_complete")
        self.assertEqual(summary["candidate_has_user"], "1")
        self.assertEqual(summary["open_later_turn"], open_turn)
        self.assertEqual(summary["goal_status"], "active")
        self.assertFalse(summary["contains_history"])
        self.assertEqual(summary["candidate_message_utf16_length"], 8_191)
        self.assertFalse(summary["candidate_message_has_replacement"])
        self.assertEqual(summary["private_count"], 23)
        self.assertFalse(summary["private_contains_message"])
        self.assertEqual(summary["partial_count"], 23)
        self.assertEqual(summary["partial_incomplete"], "1")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows incremental privacy test")
    def test_powershell_incremental_probe_redacts_messages_when_disabled(self) -> None:
        turn_id = "00000000-0000-7000-8000-00000000c004"
        private_message = "PRIVATE_INCREMENTAL_SENTINEL — già ✅"
        rollout = self.write_session_meta(str(uuid.uuid4()), subagent=False)
        self.append_rollout(rollout, "task_started", turn_id=turn_id)
        self.append_rollout(rollout, "task_complete", turn_id=turn_id, message=private_message)

        script = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$notifier = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_NOTIFIER)
$start = $notifier.IndexOf('function New-RolloutProbeState {', [StringComparison]::Ordinal)
$end = $notifier.IndexOf('function Get-FastRolloutProbe {', [StringComparison]::Ordinal)
if ($start -lt 0 -or $end -le $start) { throw 'incremental probe source unavailable' }
function Get-ObjectValue {
  param([object]$Object, [string]$Name, [object]$Default = $null)
  if ($null -eq $Object) { return $Default }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) { return $Default }
  return $property.Value
}
function Get-FirstObjectValue {
  param([object]$Object, [string[]]$Names)
  foreach ($name in $Names) {
    $property = $Object.PSObject.Properties[$name]
    if ($null -ne $property) { return $property.Value }
  }
  return $null
}
function Sanitize-NotificationText {
  param([string]$Text, [int]$MaxLength = 4000, [switch]$PreserveLines)
  if ($null -eq $Text) { return '' }
  return $Text.Substring(0, [Math]::Min($Text.Length, $MaxLength))
}
function ConvertFrom-StrictJsonText {
  param([Parameter(Mandatory = $true)][string]$Text)
  return ($Text | ConvertFrom-Json -ErrorAction Stop)
}
$script:RolloutProbeCache = @{}
$MaxFallbackRolloutLineBytes = 8 * 1024 * 1024
$Utf8StrictNoBom = [Text.UTF8Encoding]::new($false, $true)
Invoke-Expression $notifier.Substring($start, $end - $start)
$privateProbe = Update-RolloutProbe -Path $env:CODEX_NTFY_TEST_ROLLOUT -IncludeMessage $false
$privateState = $privateProbe.state
$privateSerialized = $privateState | ConvertTo-Json -Depth 8 -Compress
$publicProbe = Update-RolloutProbe -Path $env:CODEX_NTFY_TEST_ROLLOUT -IncludeMessage $true
$publicState = $publicProbe.state
[pscustomobject]@{
  private_ok = [bool]$privateProbe.ok
  private_has_final = [bool]$privateState.finalMessageTurns.ContainsKey($env:CODEX_NTFY_TEST_TURN)
  private_message = [string]$privateState.terminalMessages[$env:CODEX_NTFY_TEST_TURN]
  private_contains_sentinel = $privateSerialized.Contains('PRIVATE_INCREMENTAL_SENTINEL')
  public_ok = [bool]$publicProbe.ok
  public_message = [string]$publicState.terminalMessages[$env:CODEX_NTFY_TEST_TURN]
} | ConvertTo-Json -Compress
"""
        env = {
            **self.env,
            "CODEX_NTFY_TEST_NOTIFIER": str(POWERSHELL_NOTIFIER),
            "CODEX_NTFY_TEST_ROLLOUT": str(rollout),
            "CODEX_NTFY_TEST_TURN": turn_id,
        }
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        summary = json.loads(result.stdout)
        self.assertTrue(summary["private_ok"])
        self.assertTrue(summary["private_has_final"])
        self.assertEqual(summary["private_message"], "")
        self.assertFalse(summary["private_contains_sentinel"])
        self.assertTrue(summary["public_ok"])
        self.assertEqual(summary["public_message"], private_message)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows strict rollout UTF-8 test")
    def test_powershell_invalid_utf8_rollout_fails_closed(self) -> None:
        self.configure(
            include_message=False,
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            idle_probe_grace_seconds=300,
            goal_poll_seconds=0.05,
            watch_rollouts=False,
            suppress_technical_turns=False,
        )
        thread_id = str(uuid.uuid4())
        turn_id = "00000000-0000-7000-8000-00000000c003"
        rollout = self.write_session_meta(thread_id, subagent=False)
        self.append_rollout(rollout, "task_started", turn_id=turn_id)
        self.append_rollout(rollout, "user_message", message="Strict UTF-8")
        terminal = (
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "last_agent_message": "INVALID_UTF8_MARKER",
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        ).replace(b"INVALID_UTF8_MARKER", b"\x80")
        with rollout.open("ab") as handle:
            handle.write(terminal)

        event = self.event(thread_id=thread_id, turn_id=turn_id)
        event["last-assistant-message"] = "must never be sent"
        self.run_ok(self.hook_command("powershell", event))
        process = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 90
            reason = ""
            while time.monotonic() < deadline and reason != "rollout-probe-failed":
                for pending_path in (self.state / "pending").glob("*.json"):
                    try:
                        pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    reason = str(pending_record.get("gate_reason") or "")
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(reason, "rollout-probe-failed", self.state_debug())
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [])
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(record.get("gate_reason"), "rollout-probe-failed")

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
                    deadline = time.monotonic() + 30
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
                    self.assert_worker_ok(process, timeout=60)
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
            watch_rollouts=False,
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
                process = subprocess.Popen(
                    self.continuous_worker_command(implementation),
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                worker_output: tuple[str, str] | None = None
                try:
                    deadline = time.monotonic() + 60
                    observed_reason = ""
                    active_descendants = 0
                    while time.monotonic() < deadline and (
                        observed_reason != "subagents-active" or active_descendants != 1
                    ):
                        with self.server.lock:
                            self.assertEqual(
                                self.server.payloads,
                                [],
                                "root notification arrived while its descendant was still active",
                            )
                        for pending_path in (self.state / "pending").glob("*.json"):
                            try:
                                pending_record = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                            except (OSError, json.JSONDecodeError):
                                continue
                            observed_reason = str(
                                pending_record.get("idle_reason") or pending_record.get("gate_reason") or ""
                            )
                            active_descendants = int(pending_record.get("active_descendants") or 0)
                        if process.poll() is not None:
                            worker_output = process.communicate()
                            self.fail(
                                "continuous worker exited before observing the active descendant: "
                                f"returncode={process.returncode}\n"
                                f"stdout={worker_output[0]}\nstderr={worker_output[1]}"
                            )
                        time.sleep(0.05)
                    self.assertEqual(observed_reason, "subagents-active", "worker never observed the active descendant")
                    self.assertEqual(active_descendants, 1, "worker did not persist the active descendant count")
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [])
                    self.append_rollout(child_rollout, "task_complete", turn_id=child_turn, message="Child done")
                    payloads = self.wait_for_payloads(1, timeout=60)
                    self.assertEqual(len(payloads), 1)
                finally:
                    if worker_output is None:
                        worker_output = self.stop_continuous_worker(process)
                    self.assertIn(
                        process.returncode,
                        (0, 1, -15),
                        msg=f"stdout={worker_output[0]}\nstderr={worker_output[1]}",
                    )
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
                    self.assert_worker_ok(process, timeout=60)
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
                    deadline = time.monotonic() + 60
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
                    self.assert_worker_ok(process, timeout=60)
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

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_stdin_is_strict_utf8_independent_of_console_code_page(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        message = "Fatto — più già; è ✅ 👩🏽‍💻; 中文"
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        event["last_assistant_message"] = message

        self.run_claude_hook(event, input_encoding=850)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(record["event"]["last-assistant-message"], message)

        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn(message, payloads[0]["message"])
        for mojibake in ("ÔÇ", "├", "Γ£", "≡ƒ", "�"):
            self.assertNotIn(mojibake, payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_invalid_utf8_stdin_fails_closed(self) -> None:
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        event["last_assistant_message"] = "INVALID_UTF8_BYTE"
        encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
        invalid = encoded.replace(b"INVALID_UTF8_BYTE", b"\x80")
        cases = {
            "invalid-utf8": invalid,
            "invalid-after-utf8-bom": b"\xef\xbb\xbf" + invalid,
            "utf16-bom": json.dumps(event, ensure_ascii=False).encode("utf-16"),
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                result = subprocess.run(
                    self.claude_hook_command(input_encoding=850),
                    input=raw,
                    env=self.env,
                    capture_output=True,
                    timeout=60,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                self.assertEqual(result.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
                self.assertEqual(stdout.strip(), "{}")
                for directory in ("pending", "outbox", "sent", "suppressed", "dead"):
                    self.assertFalse(list((self.state / directory).glob("*.json")), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_valid_utf8_bom_stdin_is_supported_without_code_page_fallback(self) -> None:
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        message = "BOM UTF-8 valido — già ✅"
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        event["last_assistant_message"] = message
        raw = b"\xef\xbb\xbf" + json.dumps(event, ensure_ascii=False).encode("utf-8")
        result = subprocess.run(
            self.claude_hook_command(input_encoding=850),
            input=raw,
            env=self.env,
            capture_output=True,
            timeout=60,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
        self.assertEqual(stdout.strip(), "{}")
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(record["event"]["last-assistant-message"], message)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_final_stop_is_strong_deduplicated_and_has_no_codex_link(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            include_thread_title=True,
            include_task_link=True,
            include_task_link_action=True,
        )
        session_id = str(uuid.uuid4())
        prompt_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text(
            json.dumps({"type": "ai-title", "sessionId": session_id, "aiTitle": "Generated title"})
            + "\n"
            + json.dumps({"type": "custom-title", "sessionId": session_id, "customTitle": "Claude conversation"})
            + "\n",
            encoding="utf-8",
        )
        event = self.claude_event(session_id=session_id, prompt_id=prompt_id, transcript_path=transcript)
        self.run_claude_hook(event)
        self.run_claude_hook(event)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(record["provider"], "claude")
        self.assertFalse(record["weak_identity"])
        self.assertEqual(record["thread_id"], session_id)
        self.assertEqual(record["turn_id"], prompt_id)
        self.assertTrue(record["sequence_id"].startswith("claude-"))

        self.run_ok(self.worker_command("powershell"))
        self.assertEqual(len(self.wait_for_payloads(1)), 1, self.state_debug())
        self.run_claude_hook(self.claude_idle_event(event, include_prompt_id=True))
        self.run_ok(self.worker_command("powershell"))
        self.run_claude_hook(event)
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            payloads = list(self.server.payloads)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertEqual(payloads[0]["title"], "Claude conversation")
        self.assertIn("Claude final response.", payloads[0]["message"])
        self.assertNotIn("click", payloads[0])
        self.assertNotIn("actions", payloads[0])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_same_prompt_refresh_wins_promotion_race(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        first = {**event, "last_assistant_message": "STALE candidate."}
        self.run_claude_hook(first)

        marker = self.temp / "before-promote.marker"
        release = self.temp / "before-promote.release"
        worker_env = {
            **self.env,
            "CODEX_NTFY_TEST_BEFORE_PROMOTE_MS": "10000",
            "CODEX_NTFY_TEST_BEFORE_PROMOTE_MARKER": str(marker),
            "CODEX_NTFY_TEST_BEFORE_PROMOTE_RELEASE": str(release),
        }
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env=worker_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        self.assertTrue(marker.exists(), self.state_debug())

        latest = {**event, "last_assistant_message": "Latest final result."}
        self.run_claude_hook(latest)
        release.write_text("release", encoding="utf-8")
        self.assert_worker_ok(worker, timeout=45)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Latest final result.", payloads[0]["message"])
        self.assertNotIn("STALE candidate.", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_promotion_rechecks_session_epoch(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.run_claude_hook(event)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        candidate = json.loads(pending[0].read_text(encoding="utf-8-sig"))

        marker = self.temp / "before-session-epoch-promote.marker"
        worker_env = {
            **self.env,
            "CODEX_NTFY_TEST_BEFORE_PROMOTE_MS": "2000",
            "CODEX_NTFY_TEST_BEFORE_PROMOTE_MARKER": str(marker),
        }
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env=worker_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        self.assertTrue(marker.exists(), self.state_debug())

        # This is the atomic state transition performed at the start of a new
        # prompt, isolated from its subsequent best-effort pending-file cleanup.
        session_files = list((self.state / "claude-sessions").glob("*.json"))
        self.assertEqual(len(session_files), 1, self.state_debug())
        session_state = json.loads(session_files[0].read_text(encoding="utf-8-sig"))
        session_state["epoch"] += 1
        session_state["state"] = "busy"
        session_state["prompt_id"] = str(uuid.uuid4())
        session_state["busy_unix_ms"] = int(time.time() * 1000)
        session_files[0].write_text(json.dumps(session_state), encoding="utf-8")

        self.assert_worker_ok(worker, timeout=45)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        for directory in ("pending", "outbox", "sent", "dead"):
            self.assertFalse(list((self.state / directory).glob("*.json")), self.state_debug())
        self.assert_content_free_suppression_receipt(candidate, "stale-session")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_unverifiable_session_state_is_terminal_even_during_refresh_race(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        cases = (("missing", False), ("corrupt", False), ("missing", True))
        for state_mode, refresh in cases:
            with self.subTest(state_mode=state_mode, refresh=refresh):
                session_id = str(uuid.uuid4())
                transcript = self.temp / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                event = self.claude_event(session_id=session_id, transcript_path=transcript)
                self.run_claude_hook(self.claude_prompt_event(event))
                self.run_claude_hook(event)

                marker = self.temp / f"before-unverifiable-{state_mode}-{refresh}.marker"
                release = self.temp / f"release-unverifiable-{state_mode}-{refresh}.marker"
                worker_env = {
                    **self.env,
                    "CODEX_NTFY_TEST_BEFORE_PROMOTE_MS": "10000",
                    "CODEX_NTFY_TEST_BEFORE_PROMOTE_MARKER": str(marker),
                    "CODEX_NTFY_TEST_BEFORE_PROMOTE_RELEASE": str(release),
                }
                worker = subprocess.Popen(
                    self.worker_command("powershell"),
                    env=worker_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.time() + 30
                    while time.time() < deadline and not marker.exists():
                        time.sleep(0.05)
                    self.assertTrue(marker.exists(), self.state_debug())

                    session_files = list((self.state / "claude-sessions").glob("*.json"))
                    self.assertEqual(len(session_files), 1, self.state_debug())
                    if state_mode == "missing":
                        session_files[0].unlink()
                    else:
                        session_files[0].write_text("{not-json", encoding="utf-8")
                    expected_suppressed_revision = marker.read_text(encoding="utf-8")
                    if refresh:
                        pending = list((self.state / "pending").glob("*.json"))
                        self.assertEqual(len(pending), 1, self.state_debug())
                        canonical = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                        self.assertEqual(canonical["candidate_revision"], expected_suppressed_revision)
                        expected_suppressed_revision = uuid.uuid4().hex
                        canonical["candidate_revision"] = expected_suppressed_revision
                        canonical["event"]["last-assistant-message"] = "New canonical revision must stay suppressed."
                        replacement = pending[0].with_suffix(".refresh.tmp")
                        replacement.write_text(json.dumps(canonical, ensure_ascii=False), encoding="utf-8")
                        os.replace(replacement, pending[0])
                    release.write_text("continue", encoding="ascii")

                    self.assert_worker_ok(worker, timeout=60)
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [], self.state_debug())
                    for directory in ("pending", "outbox", "sent", "dead"):
                        self.assertFalse(list((self.state / directory).glob("*.json")), self.state_debug())
                    suppressed = list((self.state / "suppressed").glob("*.json"))
                    self.assertEqual(len(suppressed), 1, self.state_debug())
                    receipt = json.loads(suppressed[0].read_text(encoding="utf-8-sig"))
                    self.assertEqual(receipt["reason"], "claude-session-unverifiable")
                    self.assertEqual(receipt["candidate_revision"], expected_suppressed_revision)

                    if refresh:
                        # A duplicate Stop and another worker cannot revive this
                        # terminal, non-technical suppression.
                        self.run_claude_hook(event)
                        self.run_ok(self.worker_command("powershell"), timeout=30)
                        with self.server.lock:
                            self.assertEqual(self.server.payloads, [], self.state_debug())
                        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
                        self.assertEqual(len(list((self.state / "suppressed").glob("*.json"))), 1)
                finally:
                    with contextlib.suppress(OSError):
                        release.write_text("continue", encoding="ascii")
                    if worker.poll() is None:
                        try:
                            worker.communicate(timeout=15)
                        except subprocess.TimeoutExpired:
                            worker.terminate()
                            worker.communicate(timeout=10)
                    shutil.rmtree(self.state, ignore_errors=True)
                    with self.server.lock:
                        self.server.payloads.clear()

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_maintenance_preserves_claude_session_state_referenced_by_pending(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0, sent_retention_days=1)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.run_claude_hook(event)
        session_files = list((self.state / "claude-sessions").glob("*.json"))
        self.assertEqual(len(session_files), 1, self.state_debug())
        old = time.time() - (3 * 24 * 60 * 60)
        os.utime(session_files[0], (old, old))

        maintenance = [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_NOTIFIER),
            "-Maintenance",
        ]
        self.run_ok(maintenance)
        self.assertTrue(session_files[0].exists(), self.state_debug())

        for pending in (self.state / "pending").glob("*.json"):
            pending.unlink()
        os.utime(session_files[0], (old, old))
        self.run_ok(maintenance)
        self.assertFalse(session_files[0].exists(), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_large_transcript_scan_and_title_do_not_use_slow_tail_path(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            include_thread_title=True,
        )
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        title = json.dumps(
            {"type": "custom-title", "sessionId": session_id, "customTitle": "Large Claude session"},
            separators=(",", ":"),
        )
        filler = json.dumps(
            {"type": "progress", "payload": "x" * 180},
            separators=(",", ":"),
        ) + "\n"
        filler_count = max(1, (12 * 1024 * 1024) // len(filler))
        with transcript.open("w", encoding="utf-8") as stream:
            stream.write(title + "\n")
            for _ in range(filler_count):
                stream.write(filler)

        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        started = time.monotonic()
        self.run_claude_hook(event)
        self.run_ok(self.worker_command("powershell"), timeout=20)
        elapsed = time.monotonic() - started
        payloads = self.wait_for_payloads(1)
        self.assertEqual(payloads[0]["title"], "Large Claude session")
        self.assertLess(elapsed, 12.0, f"large Claude transcript path took {elapsed:.2f}s")
        notifier_source = POWERSHELL_NOTIFIER.read_text(encoding="utf-8")
        claude_source = notifier_source[
            notifier_source.index("function Get-ClaudeThreadTitle") : notifier_source.index("function Get-CompletionLabel")
        ]
        self.assertNotIn(" -Tail ", claude_source)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_title_tail_preserves_utf8_across_every_four_byte_chunk_split(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            include_thread_title=True,
        )
        for split_offset in (1, 2, 3):
            with self.subTest(split_offset=split_offset):
                session_id = str(uuid.uuid4())
                transcript = self.temp / f"{session_id}.jsonl"
                title = f"Attività — 👩🏽‍💻 pronta 中文 {split_offset}"
                title_line = (
                    json.dumps(
                        {"type": "custom-title", "sessionId": session_id, "customTitle": title},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                self.write_reverse_boundary_transcript(
                    transcript,
                    prefix=b"{}\n" * 501,
                    line=title_line,
                    target="👩".encode("utf-8"),
                    split_offset=split_offset,
                )
                try:
                    event = self.claude_event(session_id=session_id, transcript_path=transcript)
                    self.run_claude_hook(event)
                    self.run_ok(self.worker_command("powershell"), timeout=60)
                    payloads = self.wait_for_payloads(1)
                    self.assertEqual(len(payloads), 1, self.state_debug())
                    self.assertEqual(payloads[0]["title"], title)
                    self.assertNotIn("�", payloads[0]["title"])
                finally:
                    shutil.rmtree(self.state, ignore_errors=True)
                    with self.server.lock:
                        self.server.payloads.clear()

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_goal_tail_preserves_utf8_marker_across_chunk_split(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        marker = "goal-👩🏽‍💻-è-中文"
        goal_line = (
            json.dumps(
                {
                    "type": "attachment",
                    "uuid": marker,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "attachment": {"type": "goal_status", "met": False, "sentinel": True},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self.write_reverse_boundary_transcript(
            transcript,
            prefix=b"{}\n" * 10,
            line=goal_line,
            target="👩".encode("utf-8"),
            split_offset=2,
        )
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(event)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(record["claude_goal_state"], "active")
        self.assertEqual(record["claude_goal_marker"], marker)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_prompt_baseline_and_huge_line_scans_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        active_marker = self.append_claude_goal_status(transcript, met=False, sentinel=True)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "progress", "payload": "x" * (2 * 1024 * 1024)}))
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))

        session_files = list((self.state / "claude-sessions").glob("*.json"))
        self.assertEqual(len(session_files), 1, self.state_debug())
        session_state = json.loads(session_files[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(session_state["goal_baseline_state"], "unknown")
        self.assertFalse(session_state["goal_baseline_captured"])

        # The synchronous Stop path stays byte-bounded and records unknown when
        # the marker is outside its window. The detached worker then performs
        # the unrestricted reverse scan and must recover the active anchor
        # before any idle fallback can release the candidate.
        self.run_claude_hook(event)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(record["claude_goal_state"], "unknown")

        worker = self.start_worker("powershell")
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if pending:
                    try:
                        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError):
                        time.sleep(0.05)
                        continue
                    if record.get("claude_goal_state") == "active":
                        break
                time.sleep(0.05)
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        self.assertEqual(record["claude_goal_state"], "active")
        self.assertEqual(record["claude_goal_marker"], active_marker)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_oversize_goal_token_record_cannot_be_released_by_idle(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        self.append_claude_goal_status(transcript, met=False, sentinel=True)
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(
                '{"type":"progress","goal_status":"not-a-lifecycle-record","payload":"'
                + ("x" * (2 * 1024 * 1024))
                + '"}'
            )

        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.run_claude_hook(event)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        # The 1 MiB synchronous window begins inside the oversized record, so it
        # can prove only unknown. The worker's unrestricted bounded-memory scan
        # sees the token and upgrades the disposition to unverifiable.
        self.assertEqual(record["claude_goal_state"], "unknown")

        # Even a perfectly correlated idle accelerator cannot turn an unparsed
        # token-bearing record into evidence that the older active goal ended.
        self.run_claude_hook(self.claude_idle_event(event, include_prompt_id=True))
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertEqual(len(list((self.state / "suppressed").glob("*.json"))), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_active_work_is_ignored_then_same_prompt_can_finish(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        prompt_id = str(uuid.uuid4())
        event = self.claude_event(session_id=session_id, prompt_id=prompt_id)

        busy = dict(event)
        busy["background_tasks"] = [{"id": "task-1", "type": "shell", "status": "running"}]
        self.run_claude_hook(busy)
        scheduled = dict(event)
        scheduled["session_crons"] = [{"id": "cron-1", "schedule": "* * * * *", "recurring": False}]
        self.run_claude_hook(scheduled)
        for directory in ("pending", "outbox", "sent", "suppressed", "dead"):
            self.assertFalse(list((self.state / directory).glob("*.json")))

        self.run_claude_hook(event)
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
        self.run_ok(self.worker_command("powershell"))
        self.assertEqual(len(self.wait_for_payloads(1)), 1)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_repeated_goal_stops_wait_for_terminal_marker_and_use_latest_result(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.append_claude_goal_status(transcript, met=False, sentinel=True)

        intermediate = dict(event)
        intermediate["last_assistant_message"] = "Intermediate goal result."
        self.run_claude_hook(intermediate)
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.append_claude_goal_status(transcript, met=False, reason="continue")
        later_intermediate = dict(event)
        later_intermediate["last_assistant_message"] = "Still working on the goal."
        self.run_claude_hook(later_intermediate)
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.append_claude_goal_status(transcript, met=True, reason="all checks pass")
        final = dict(event)
        final["last_assistant_message"] = "Goal is now complete."
        self.run_claude_hook(final)
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Goal is now complete.", payloads[0]["message"])
        self.assertNotIn("Intermediate goal result.", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_failed_goal_notifies_as_warning(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.append_claude_goal_status(transcript, met=False, sentinel=True)
        self.run_claude_hook({**event, "last_assistant_message": "Trying another route."})
        self.append_claude_goal_status(transcript, met=False, failed=True, reason="impossible")
        self.run_claude_hook({**event, "last_assistant_message": "The goal cannot be completed."})
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(payloads[0].get("tags"), ["warning"])
        self.assertIn("cannot be completed", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_cleared_goal_is_discarded_and_does_not_poison_next_prompt(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        # /goal clear is a new prompt. UserPromptSubmit snapshots the still-active
        # marker before Claude appends its terminal clear sentinel.
        self.append_claude_goal_status(transcript, met=False, sentinel=True)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.append_claude_goal_status(transcript, met=True, sentinel=True)
        self.run_claude_hook({**event, "last_assistant_message": "Goal cleared."})
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        candidate = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        for directory in ("pending", "outbox", "sent", "dead"):
            self.assertFalse(list((self.state / directory).glob("*.json")), self.state_debug())
        self.assert_content_free_suppression_receipt(candidate, "goal-cancelled")

        next_event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(next_event))
        self.run_claude_hook({**next_event, "last_assistant_message": "Ordinary turn complete."})
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertIn("Ordinary turn complete.", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_clear_after_unknown_prompt_baseline_is_not_notified(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text(
            '{"type":"attachment","attachment":{"type":"goal_status"',
            encoding="utf-8",
        )
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))

        transcript.write_text("", encoding="utf-8")
        self.append_claude_goal_status(transcript, met=True, sentinel=True)
        self.run_claude_hook({**event, "last_assistant_message": "Goal cleared."})
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        candidate = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.run_ok(self.worker_command("powershell"))

        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        for directory in ("pending", "outbox", "sent", "dead"):
            self.assertFalse(list((self.state / directory).glob("*.json")), self.state_debug())
        self.assert_content_free_suppression_receipt(candidate, "goal-cancelled")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_historical_terminal_markers_do_not_label_later_ordinary_turns(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        cases = (
            {"met": True, "reason": "done"},
            {"met": False, "failed": True, "reason": "impossible"},
            {"met": True, "sentinel": True},
        )
        for index, marker_kwargs in enumerate(cases):
            with self.subTest(marker=marker_kwargs):
                session_id = str(uuid.uuid4())
                transcript = self.temp / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                self.append_claude_goal_status(transcript, **marker_kwargs)
                event = self.claude_event(session_id=session_id, transcript_path=transcript)
                self.run_claude_hook(self.claude_prompt_event(event))
                self.run_claude_hook({**event, "last_assistant_message": f"Ordinary result {index}."})
                self.run_ok(self.worker_command("powershell"))
                payloads = self.wait_for_payloads(1)
                self.assertIn(f"Ordinary result {index}.", payloads[0]["message"])
                self.assertNotEqual(payloads[0].get("tags"), ["warning"])
                with self.server.lock:
                    self.server.payloads.clear()

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_resumed_active_goal_fails_closed_without_prompt_state(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        self.append_claude_goal_status(transcript, met=False, sentinel=True)
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook({**event, "last_assistant_message": "Resumed intermediate result."})
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.append_claude_goal_status(transcript, met=True, reason="resumed goal done")
        self.run_claude_hook({**event, "last_assistant_message": "Resumed goal complete."})
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertIn("Resumed goal complete.", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_idle_before_stop_and_new_prompt_cancellation_are_race_safe(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        first = self.claude_event()
        self.run_claude_hook(self.claude_prompt_event(first))
        self.run_claude_hook(self.claude_idle_event(first, include_prompt_id=True))
        self.run_claude_hook(first)
        self.run_ok(self.worker_command("powershell"))
        self.assertEqual(len(self.wait_for_payloads(1)), 1, self.state_debug())

        with self.server.lock:
            self.server.payloads.clear()
        second = self.claude_event(session_id=first["session_id"])
        self.run_claude_hook(self.claude_prompt_event(second))
        self.run_claude_hook(second)

        third_prompt = str(uuid.uuid4())
        self.run_claude_hook(self.claude_prompt_event(second, prompt_id=third_prompt))
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        # A late idle receipt for the previous prompt cannot resurrect a removed
        # candidate or notify while the session is working on its follow-up.
        self.run_claude_hook(self.claude_idle_event(second, include_prompt_id=True))
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_late_async_stop_after_new_prompt_is_ignored(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        first = self.claude_event()
        self.run_claude_hook(self.claude_prompt_event(first))

        next_prompt = self.claude_event(session_id=first["session_id"])
        self.run_claude_hook(self.claude_prompt_event(next_prompt))
        # The async process for the previous Stop can be scheduled only after
        # UserPromptSubmit has already made the next prompt authoritative.
        self.run_claude_hook(first)
        self.run_ok(self.worker_command("powershell"))

        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        for directory in ("pending", "outbox", "sent", "suppressed", "dead"):
            self.assertFalse(list((self.state / directory).glob("*.json")), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_idle_fallback_requires_matching_prompt_id(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "attachment",
                    "uuid": str(uuid.uuid4()),
                    "attachment": {"type": "goal_status", "met": "false"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.run_claude_hook(event)
        self.run_claude_hook(self.claude_idle_event(event, include_prompt_id=False))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.run_claude_hook(self.claude_idle_event(event, include_prompt_id=True))
        self.run_ok(self.worker_command("powershell"))
        self.assertEqual(len(self.wait_for_payloads(1)), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_transient_partial_transcript_recovers_without_idle_notification(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text('{"type":"progress","label":"goal_status"', encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.run_claude_hook({**event, "last_assistant_message": "Recovered final result."})
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write("}\n")

        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertIn("Recovered final result.", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_unknown_reconciles_to_persisted_active_anchor(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.temp / f"{session_id}.jsonl"
        transcript.write_text('{"type":"attachment","attachment":{"type":"goal_status"', encoding="utf-8")
        event = self.claude_event(session_id=session_id, transcript_path=transcript)
        self.run_claude_hook(self.claude_prompt_event(event))
        self.run_claude_hook({**event, "last_assistant_message": "Intermediate result."})

        transcript.write_text("", encoding="utf-8")
        active_marker = self.append_claude_goal_status(transcript, met=False, sentinel=True)
        worker = self.start_worker("powershell")
        try:
            deadline = time.time() + 10
            record: dict = {}
            while time.time() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if pending:
                    record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                    if record.get("claude_goal_state") == "active":
                        break
                time.sleep(0.05)
            self.assertEqual(record["claude_goal_state"], "active")
            self.assertEqual(record["claude_goal_marker"], active_marker)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        shutil.rmtree(self.state / "claude-sessions", ignore_errors=True)
        self.append_claude_goal_status(transcript, met=True, sentinel=True)
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_unverifiable_and_subagent_stops_fail_closed(self) -> None:
        cases: list[dict] = []
        no_background = self.claude_event()
        no_background.pop("background_tasks")
        cases.append(no_background)
        no_crons = self.claude_event()
        no_crons.pop("session_crons")
        cases.append(no_crons)
        null_background = self.claude_event()
        null_background["background_tasks"] = None
        cases.append(null_background)
        null_crons = self.claude_event()
        null_crons["session_crons"] = None
        cases.append(null_crons)
        object_background = self.claude_event()
        object_background["background_tasks"] = {"status": "unknown"}
        cases.append(object_background)
        string_crons = self.claude_event()
        string_crons["session_crons"] = "none"
        cases.append(string_crons)
        no_prompt = self.claude_event()
        no_prompt.pop("prompt_id")
        cases.append(no_prompt)
        subagent = self.claude_event()
        subagent["hook_event_name"] = "SubagentStop"
        subagent["agent_id"] = "agent-1"
        cases.append(subagent)
        defensive_subagent = self.claude_event()
        defensive_subagent["agent_id"] = "agent-2"
        cases.append(defensive_subagent)

        for index, event in enumerate(cases):
            with self.subTest(index=index):
                self.run_claude_hook(event)
        for directory in ("pending", "outbox", "sent", "suppressed", "dead"):
            self.assertFalse(list((self.state / directory).glob("*.json")))
        with self.server.lock:
            self.assertEqual(self.server.payloads, [])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Claude Code Windows test")
    def test_claude_sessions_do_not_collide_and_stop_failure_is_terminal(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        shared_prompt = str(uuid.uuid4())
        first = self.claude_event(session_id=str(uuid.uuid4()), prompt_id=shared_prompt)
        second = self.claude_event(session_id=str(uuid.uuid4()), prompt_id=shared_prompt)
        failure = self.claude_event(session_id=str(uuid.uuid4()), prompt_id=str(uuid.uuid4()))
        failure["hook_event_name"] = "StopFailure"
        failure.pop("background_tasks")
        failure.pop("session_crons")
        failure["error"] = "rate_limit"
        failure["last_assistant_message"] = "API Error: Rate limit reached"

        for event in (first, second, failure):
            self.run_claude_hook(event)
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 3)
        self.run_claude_hook(self.claude_idle_event(first, include_prompt_id=True))
        self.run_claude_hook(
            self.claude_idle_event(second, notification_type="agent_completed", include_prompt_id=True)
        )
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(3)
        self.assertEqual(len(payloads), 3)
        self.assertEqual(len({payload["sequence_id"] for payload in payloads}), 3)
        self.assertTrue(any("Rate limit reached" in payload["message"] for payload in payloads))
        failed_payload = next(payload for payload in payloads if "Rate limit reached" in payload["message"])
        self.assertEqual(failed_payload["tags"], ["warning"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_permission_prompt_is_exact_once_across_resume_and_new_question(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            include_thread_title=True,
        )
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        title = "Scelta deploy — già pronta 中文"
        transcript.write_text(
            json.dumps(
                {"type": "custom-title", "sessionId": session_id, "customTitle": title},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Prepara il deploy"))
        first_text = "Confermi l’opzione ‘Prima’ — sì/no? 中文"
        self.append_audncode_question(event, question=first_text)
        permission = self.audncode_idle_event(event, notification_type="permission_prompt")

        self.run_audncode_hooks_concurrently([permission, permission])
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertEqual(payloads[0]["title"], title)
        self.assertEqual(payloads[0]["tags"], ["question"])
        self.assertIn(first_text, payloads[0]["message"])
        self.assertNotIn("Done", payloads[0]["title"])
        self.assertNotIn("Qwen", payloads[0]["title"])
        self.assertNotIn("❓", payloads[0]["title"])
        for mojibake in ("ÔÇ", "├", "Γ£", "≡ƒ", "�"):
            self.assertNotIn(mojibake, payloads[0]["title"])
            self.assertNotIn(mojibake, payloads[0]["message"])

        self.run_audncode_hook(self.audncode_session_start_event(event, source="resume"))
        self.run_audncode_hook(permission)
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Annulla la scelta precedente e chiedine una nuova")
        )
        second_text = "Quale ambiente vuoi usare adesso?"
        self.append_audncode_question(event, question=second_text)
        self.run_audncode_hook(permission)
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(2)
        self.assertEqual(len(payloads), 2, self.state_debug())
        self.assertEqual(payloads[1]["tags"], ["question"])
        self.assertIn(second_text, payloads[1]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_permission_prompt_rejects_untrusted_or_non_root_question_shapes(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        scenarios = (
            "spoof-only",
            "bash-tool",
            "sidechain",
            "nested-agent",
            "multiple-tools",
            "answered",
            "provider-error",
            "invalid-utf8",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                session_id = str(uuid.uuid4())
                transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                event = self.audncode_event(session_id=session_id, transcript_path=transcript)
                self.run_audncode_hook(self.audncode_prompt_event(event, prompt=f"Scenario {scenario}"))
                if scenario == "bash-tool":
                    self.append_audncode_question(event, tool_name="Bash")
                elif scenario == "sidechain":
                    self.append_audncode_question(event, sidechain=True)
                elif scenario == "nested-agent":
                    self.append_audncode_question(event, agent_id="a12345678")
                elif scenario == "multiple-tools":
                    self.append_audncode_question(event, multiple_tool_calls=True)
                elif scenario == "answered":
                    question = self.append_audncode_question(event)
                    self.append_audncode_question_answer(event, question)
                elif scenario == "provider-error":
                    self.append_audncode_question(event)
                    self.append_audncode_stop_failure_proof(
                        event,
                        error="ProviderError",
                        message="Provider retry exhausted in this transcript tail.",
                    )
                elif scenario == "invalid-utf8":
                    self.append_audncode_question(event)
                    with transcript.open("ab") as stream:
                        stream.write(b'{"type":"user","bad":"\xff"}\n')
                permission = self.audncode_idle_event(
                    event, notification_type="permission_prompt"
                )
                permission["message"] = "AskUserQuestion spoof in Notification text"
                self.run_audncode_hook(permission)
                self.assertEqual(
                    list((self.state / "outbox").glob("*.json")),
                    [],
                    self.state_debug(),
                )
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_question_answer_opens_one_epoch_before_one_final_completion(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            message="Risposta finale dopo la scelta.",
        )
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Chiedi e completa"))
        question = self.append_audncode_question(event, question="Procedo con la prima opzione?")
        permission = self.audncode_idle_event(event, notification_type="permission_prompt")
        self.run_audncode_hook(permission)
        self.run_ok(self.worker_command("powershell"))
        self.assertEqual(len(self.wait_for_payloads(1)), 1, self.state_debug())
        _state_path, before_answer = self.read_audncode_session_state(session_id)
        old_epoch = int(before_answer["epoch"])

        answer = self.append_audncode_question_answer(event, question)
        self.run_audncode_hooks_concurrently([answer, answer])
        _state_path, after_concurrent = self.read_audncode_session_state(session_id)
        self.assertEqual(int(after_concurrent["epoch"]), old_epoch + 1, self.state_debug())
        self.assertEqual(
            after_concurrent["audncode_question_answer_tool_use_id"],
            question["tool_use_id"],
        )
        self.run_audncode_hook(answer)
        _state_path, after_delayed = self.read_audncode_session_state(session_id)
        self.assertEqual(int(after_delayed["epoch"]), old_epoch + 1, after_delayed)

        self.run_audncode_hook(permission)
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(2)
        self.assertEqual(len(payloads), 2, self.state_debug())
        self.assertEqual(payloads[0]["tags"], ["question"])
        self.assertNotEqual(payloads[1]["tags"], ["question"])
        self.assertIn("Risposta finale dopo la scelta.", payloads[1]["message"])

        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 2, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_intermediate_stops_wait_for_idle_and_latest_utf8_result_wins(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            include_thread_title=True,
        )
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        title = "Attività — è già pronta 👩🏽‍💻 中文"
        transcript.write_text(
            json.dumps(
                {"type": "custom-title", "sessionId": session_id, "customTitle": title},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        self.run_audncode_hook(self.audncode_prompt_event(event))
        self.run_audncode_hook({**event, "last_assistant_message": "Intermediate result — do not send."})
        worker = self.start_worker("powershell")
        try:
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        final_message = "Fatto — più già; è ✅ 👩🏽‍💻; 中文"
        self.run_audncode_hook({**event, "last_assistant_message": final_message})
        self.run_audncode_hook(self.audncode_idle_event(event, notification_type="agent_completed"))
        worker = self.start_worker("powershell")
        try:
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        time.sleep(0.2)
        self.run_audncode_hook(self.audncode_idle_event(event))
        time.sleep(0.6)
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertEqual(payloads[0]["title"], title)
        self.assertIn(final_message, payloads[0]["message"])
        self.assertNotIn("Intermediate result", payloads[0]["message"])
        for mojibake in ("ÔÇ", "├", "Γ£", "≡ƒ", "�"):
            self.assertNotIn(mojibake, payloads[0]["title"])
            self.assertNotIn(mojibake, payloads[0]["message"])

        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_ultrareview_claim_is_sticky_until_exact_remote_terminal_proof(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        task_id = "r1234abcd"
        sidecar = self.write_audncode_remote_agent_sidecar(event, task_id=task_id)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt=" /ultrareview 42 "))
        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))
        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    _state_path, observed_state = self.read_audncode_session_state(session_id)
                except AssertionError:
                    time.sleep(0.1)
                    continue
                observed_claims = observed_state.get("audncode_remote_claims", [])
                if observed_claims and observed_claims[0].get("task_id") == task_id:
                    break
                time.sleep(0.1)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        _state_path, bound_state = self.read_audncode_session_state(session_id)
        bound_claims = bound_state["audncode_remote_claims"]
        self.assertEqual(len(bound_claims), 1)
        self.assertEqual(bound_claims[0]["task_id"], task_id, self.state_debug())

        # Deletion, a later prompt/epoch, and an unrelated remote terminal are
        # not proof that this exact CCR task stopped. The claim survives all of
        # them (and therefore also survives --resume, which reuses this state).
        sidecar.unlink()
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Continue locally"))
        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))
        wrong_terminal = """<task-notification>
<task-id>rdeadbeef</task-id>
<task-type>remote_agent</task-type>
<status>completed</status>
<summary>Different remote task completed</summary>
</task-notification>"""
        self.append_audncode_task_notification(event, wrong_terminal)
        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        exact_terminal = f"""<task-notification>
<task-id>{task_id}</task-id>
<task-type>remote_agent</task-type>
<status>completed</status>
<summary>Remote review completed</summary>
</task-notification>"""
        self.append_audncode_task_notification_attachment(event, exact_terminal)
        worker = self.start_worker("powershell")
        try:
            payloads = self.wait_for_payloads(1, timeout=10)
            self.assertEqual(len(payloads), 1, self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        _state_path, terminal_state = self.read_audncode_session_state(session_id)
        self.assertEqual(terminal_state["audncode_remote_claims"], [])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_fresh_subagent_start_does_not_claim_background_work(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Run a synchronous skill"))
        self.run_audncode_hook(
            self.audncode_subagent_start_event(event, agent_id="a1234abcd", agent_type="Skill")
        )
        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        runtime = self.read_json_retry(runtime_paths[0])
        self.assertEqual(runtime.get("background_ids"), [], runtime)
        self.assertEqual(runtime.get("background_open_counts"), [], runtime)
        self.assertFalse(runtime.get("local_agent_ui_uncertain"), runtime)

        self.run_audncode_hook({**event, "last_assistant_message": "The synchronous skill returned."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("synchronous skill returned", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_public_fresh_async_agent_releases_on_counted_terminal(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        agent_id = "a1234abcd"

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Launch an async local agent"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": agent_id},
                "tool_use_id": "tool-use-fresh-async-agent",
            }
        )
        terminal = f"""<task-notification>
<task-id>{agent_id}</task-id>
<task-type>local_agent</task-type>
<status>completed</status>
<summary>The async agent emitted its terminal record</summary>
</task-notification>"""
        self.append_audncode_task_notification(event, terminal)
        self.run_audncode_hook(
            {**event, "last_assistant_message": "The leader returned after the agent terminal."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        time.sleep(1.4)
        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        runtime = self.read_json_retry(runtime_paths[0])
        self.assertEqual(runtime.get("background_ids"), [], runtime)
        self.assertFalse(runtime.get("local_agent_ui_uncertain"), runtime)

        # The public external build does not expose the internal coordinator UI
        # steering path. A counted trusted terminal is therefore sufficient
        # unless SendMessage or a same-ID overlap set the sticky uncertainty.
        self.exit_audncode_host(session_id)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_resumed_subagent_incarnations_and_ui_queue_stay_fail_closed_after_exit(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        agent_id = "a1234abcd"
        sidechain = transcript.parent / session_id / "subagents" / f"agent-{agent_id}.jsonl"
        sidechain.parent.mkdir(parents=True)
        sidechain.write_text('{"type":"assistant","uuid":"old"}\n', encoding="utf-8")

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Resume the viewed agent"))
        resume_event = self.audncode_subagent_start_event(event, agent_id=agent_id)
        self.run_audncode_hook(resume_event)
        # A retry of the same native event against the unchanged transcript is
        # idempotent. A later resume after transcript growth is a distinct open
        # incarnation even when AudnCode reuses the same a-ID.
        self.run_audncode_hook(resume_event)
        sidechain.write_text(
            sidechain.read_text(encoding="utf-8")
            + '{"type":"assistant","uuid":"new"}\n',
            encoding="utf-8",
        )
        self.run_audncode_hook(resume_event)

        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        runtime_path = runtime_paths[0]
        runtime = self.read_json_retry(runtime_path)
        self.assertEqual(runtime.get("background_ids"), [agent_id], runtime)
        self.assertEqual(
            runtime.get("background_open_counts"), [{"id": agent_id, "count": 2}], runtime
        )
        self.assertTrue(runtime.get("local_agent_ui_uncertain"), runtime)

        self.run_audncode_hook(
            {**event, "last_assistant_message": "The leader stopped while a resume is active."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        first_terminal = f"""<task-notification>
<task-id>{agent_id}</task-id>
<task-type>local_agent</task-type>
<status>completed</status>
<summary>The delayed old lifecycle completed</summary>
</task-notification>"""
        self.append_audncode_task_notification(event, first_terminal)
        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 30
            runtime: dict = {}
            while time.monotonic() < deadline:
                runtime = self.read_json_retry(runtime_path)
                if runtime.get("background_open_counts") == [{"id": agent_id, "count": 1}]:
                    break
                time.sleep(0.1)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        self.assertEqual(
            runtime.get("background_open_counts"), [{"id": agent_id, "count": 1}], runtime
        )

        # Even if a second terminal happens to be emitted, it cannot prove that
        # hookless RAM-only input was consumed by the running local agent.
        second_terminal = first_terminal.replace(
            "The delayed old lifecycle completed", "The resumed lifecycle completed"
        )
        self.append_audncode_task_notification_attachment(event, second_terminal)
        worker = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 30
            runtime = {}
            while time.monotonic() < deadline:
                runtime = self.read_json_retry(runtime_path)
                if runtime.get("background_ids") == []:
                    break
                time.sleep(0.1)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        self.assertEqual(runtime.get("background_ids"), [], runtime)
        self.assertEqual(runtime.get("background_open_counts"), [], runtime)
        self.assertTrue(runtime.get("local_agent_ui_uncertain"), runtime)

        self.exit_audncode_host(session_id)
        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_send_message_success_stays_fail_closed_after_exit_but_failure_is_safe(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        failed_id = str(uuid.uuid4())
        failed_transcript = self.audncode_home / "projects" / f"{failed_id}.jsonl"
        failed_transcript.write_text("{}\n", encoding="utf-8")
        failed = self.audncode_event(session_id=failed_id, transcript_path=failed_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(failed, prompt="Try an unavailable recipient"))
        self.run_audncode_hook(
            self.audncode_send_message_event(
                failed,
                success=False,
                message="No agent found with name 'missing'",
                recipient="missing",
            )
        )
        self.run_audncode_hook({**failed, "last_assistant_message": "No message was queued."})
        self.run_audncode_hook(self.audncode_idle_event(failed))
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())

        success_id = str(uuid.uuid4())
        success_transcript = self.audncode_home / "projects" / f"{success_id}.jsonl"
        success_transcript.write_text("{}\n", encoding="utf-8")
        success = self.audncode_event(session_id=success_id, transcript_path=success_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(success, prompt="Message the viewed agent"))
        self.run_audncode_hook(
            self.audncode_send_message_event(
                success,
                success=True,
                message="Message queued for delivery at its next tool round",
                # A name shaped like an a-ID can shadow a different actual ID;
                # the response does not expose the resolved identity.
                recipient="a1234abcd",
            )
        )
        self.run_audncode_hook(
            {**success, "last_assistant_message": "The leader returned before delivery was proven."}
        )
        self.run_audncode_hook(self.audncode_idle_event(success))
        misleading_terminal = """<task-notification>
<task-id>a1234abcd</task-id>
<task-type>local_agent</task-type>
<status>completed</status>
<summary>A different named task completed</summary>
</task-notification>"""
        self.append_audncode_task_notification(success, misleading_terminal)
        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(len(self.server.payloads), 1, self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        success_state_path, success_state = self.read_audncode_session_state(success_id)
        runtime_key = success_state["audncode_runtime_key"]
        runtime_path = success_state_path.parent / f"audn-runtime-{runtime_key}.json"
        runtime = self.read_json_retry(runtime_path)
        self.assertTrue(runtime.get("registry_valid"), runtime)
        self.assertTrue(runtime.get("local_agent_ui_uncertain"), runtime)
        self.assertEqual(runtime.get("background_ids"), [], runtime)

        self.exit_audncode_host(success_id)
        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(len(self.server.payloads), 1, self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_new_prompt_cancels_old_candidate_and_late_idle_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="First prompt"))
        self.run_audncode_hook({**event, "last_assistant_message": "Old result."})
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)

        delayed_idle = subprocess.Popen(
            self.audncode_hook_command(expected_event="Notification"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        time.sleep(0.1)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Second prompt"))
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.run_audncode_hook({**event, "last_assistant_message": "New final result."})
        delayed_stdout, delayed_stderr = delayed_idle.communicate(
            input=json.dumps(self.audncode_idle_event(event), ensure_ascii=False).encode("utf-8"),
            timeout=60,
        )
        self.assertEqual(
            delayed_idle.returncode,
            0,
            msg=f"stdout={delayed_stdout!r}\nstderr={delayed_stderr!r}",
        )
        self.assertEqual(delayed_stdout.decode("utf-8").strip(), "{}")
        worker = self.start_worker("powershell")
        try:
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("New final result.", payloads[0]["message"])
        self.assertNotIn("Old result.", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_first_prompt_accepts_transcript_created_after_submit(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        event = self.audncode_event()
        transcript = Path(event["transcript_path"])
        self.assertFalse(transcript.exists())

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="First prompt in a new chat"))
        transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook({**event, "last_assistant_message": "First chat result."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        # AudnCode's foreground-idle signal is followed by a mandatory 1.25 s
        # background settle floor. The first eligible worker then records an
        # external-state fingerprint, which must remain stable for 150 ms before
        # a later worker may promote the candidate.
        time.sleep(1.4)
        payloads: list[dict] = []
        for _attempt in range(3):
            self.run_ok(self.worker_command("powershell"), timeout=90)
            payloads = self.wait_for_payloads(1, timeout=0.1)
            if payloads:
                break
            time.sleep(0.25)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("First chat result.", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_session_start_direct_normal_failure_and_no_query(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        normal = self.audncode_event(message="Direct initial plan completed.")
        normal_transcript = Path(normal["transcript_path"])
        self.assertFalse(normal_transcript.exists())
        self.run_audncode_hook(self.audncode_session_start_event(normal, source="startup"))
        _normal_state_path, normal_state = self.read_audncode_session_state(normal["session_id"])
        self.assertEqual(normal_state["state"], "busy")
        self.assertEqual(normal_state["epoch"], 1)
        self.assertFalse(normal_state["audncode_prompt_prearm_pending"])
        self.assertFalse(normal_state["audncode_stop_failure_cursor_file_existed"])
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        normal_transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook(normal)
        self.run_audncode_hook(self.audncode_idle_event(normal))

        failure = self.audncode_event(message="API Error: authentication failed")
        failure_transcript = Path(failure["transcript_path"])
        failure_transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook(self.audncode_session_start_event(failure, source="resume"))
        failure_hook, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            failure,
            error="authentication_failed",
            message="API Error: authentication failed",
        )
        self.run_audncode_hook(failure_hook)

        no_query = self.audncode_event()
        self.run_audncode_hook(self.audncode_session_start_event(no_query, source="clear"))
        _no_query_state_path, no_query_state = self.read_audncode_session_state(
            no_query["session_id"]
        )
        self.assertEqual(no_query_state["state"], "busy")
        self.assertEqual(no_query_state["epoch"], 1)
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 2)

        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(2)
        self.assertEqual(len(payloads), 2, self.state_debug())
        messages = [payload["message"] for payload in payloads]
        self.assertTrue(any("Direct initial plan completed." in message for message in messages))
        self.assertTrue(any("authentication failed" in message for message in messages))

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_user_prompt_supersedes_session_start_placeholder(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        self.run_audncode_hook(self.audncode_session_start_event(event))
        _state_path, placeholder = self.read_audncode_session_state(session_id)
        placeholder_prompt = placeholder["prompt_id"]
        self.assertEqual(placeholder["epoch"], 1)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Normal initial prompt"))
        _state_path, submitted = self.read_audncode_session_state(session_id)
        self.assertEqual(submitted["epoch"], 2)
        self.assertNotEqual(submitted["prompt_id"], placeholder_prompt)
        self.assertGreater(submitted["busy_hook_start_ticks"], placeholder["busy_hook_start_ticks"])
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())

        # A terminal hook that started under the placeholder cannot bind itself
        # to the newer prompt merely because AudnCode exposes no prompt_id.
        self.run_audncode_hook(
            {**event, "last_assistant_message": "Delayed placeholder result."},
            env_overrides={
                "CODEX_NTFY_TEST_HOOK_START_TICKS": str(placeholder["busy_hook_start_ticks"])
            },
        )
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        current_message = "Normal submitted prompt completed."
        self.run_audncode_hook({**event, "last_assistant_message": current_message})
        self.run_audncode_hook(self.audncode_idle_event(event))
        time.sleep(0.6)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn(current_message, payloads[0]["message"])
        self.assertNotIn("Delayed placeholder", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_session_start_kill_and_malformed_ingress_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        killed_event = self.audncode_event()
        killed_transcript = Path(killed_event["transcript_path"])
        killed_transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook(self.audncode_prompt_event(killed_event, prompt="Old prompt"))
        self.run_audncode_hook({**killed_event, "last_assistant_message": "Old result."})
        self.run_audncode_hook(self.audncode_idle_event(killed_event))
        killed_state_path, old_state = self.read_audncode_session_state(killed_event["session_id"])
        killed = self.run_audncode_hook(
            self.audncode_session_start_event(killed_event, source="resume"),
            kill_when_prearm_path=killed_state_path,
            expect_success=False,
        )
        self.assertNotEqual(killed.returncode, 0)
        _state_path, killed_state = self.read_audncode_session_state(killed_event["session_id"])
        self.assertEqual(killed_state["epoch"], old_state["epoch"] + 1)
        self.assertTrue(killed_state["audncode_prompt_prearm_pending"])

        malformed_event = self.audncode_event()
        malformed_transcript = Path(malformed_event["transcript_path"])
        malformed_transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook(self.audncode_prompt_event(malformed_event, prompt="Pending result"))
        self.run_audncode_hook({**malformed_event, "last_assistant_message": "Must remain blocked."})
        self.run_audncode_hook(self.audncode_idle_event(malformed_event))
        malformed_host = self.audncode_hosts[malformed_event["session_id"]]
        self.run_audncode_raw_hook(
            b'{"hook_event_name":"SessionStart","source":',
            expected_event="SessionStart",
            host=malformed_host,
        )

        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        _killed_guard_path, killed_guard = self.read_audncode_ingress_guard(
            self.audncode_hosts[killed_event["session_id"]][0].pid
        )
        _malformed_guard_path, malformed_guard = self.read_audncode_ingress_guard(
            malformed_host[0].pid
        )
        self.assertTrue(killed_guard["lost"] or killed_guard["pending_tokens"])
        self.assertTrue(malformed_guard["lost"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_observed_host_registration_rejects_lifetime_lost_during_locked_refresh(
        self,
    ) -> None:
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        state_path = self.temp / "observed-host-registration-state.json"
        host_pid = 4242
        host_started_unix_ms = 1_700_000_000_123
        script = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$AudnCodeSessionMaxHostLifetimes = 8
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_NOTIFIER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Get-ObjectValue',
    'Get-AudnCodeSessionHostLifetimeState',
    'Register-AudnCodeObservedHostLifetime',
    'Set-RecordValue'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing notifier function: $name" }
  Invoke-Expression $definition.Extent.Text
}

$canonicalHome = [IO.Path]::GetFullPath($env:CODEX_NTFY_TEST_AUDNCODE_HOME)
$canonicalTranscript = [IO.Path]::GetFullPath($env:CODEX_NTFY_TEST_TRANSCRIPT)
$hostPid = [int]$env:CODEX_NTFY_TEST_HOST_PID
$hostStarted = [int64]$env:CODEX_NTFY_TEST_HOST_STARTED
$script:HostProbeCount = 0
$script:TargetProbeCount = 0
$script:DisappearAfterPrecheck = $true
$script:Writes = 0
$script:PersistedState = $null

function New-TestSessionState {
  return [pscustomobject]@{
    session_id = $env:CODEX_NTFY_TEST_SESSION_ID
    state = 'busy'
    transcript_path = $canonicalTranscript
    audncode_prompt_prearm_pending = $false
    audncode_host_lifetimes = [object[]]@()
    audncode_host_pid = [int]$hostPid
    audncode_host_started_unix_ms = [int64]$hostStarted
    audncode_home = $canonicalHome
    audncode_multi_host_conflict = $false
  }
}

function Get-AudnCodeHostSession {
  param(
    [string]$SessionId,
    [string]$HomePath,
    [int]$ExpectedHostPid,
    [int64]$ExpectedHostStartedUnixMs,
    [switch]$AllowExitedHost
  )
  $script:HostProbeCount++
  if ($ExpectedHostPid -eq $hostPid -and $ExpectedHostStartedUnixMs -eq $hostStarted) {
    $script:TargetProbeCount++
  }
  $isTarget = $ExpectedHostPid -eq $hostPid -and $ExpectedHostStartedUnixMs -eq $hostStarted
  $stillLive = -not ($isTarget -and $script:DisappearAfterPrecheck -and $script:TargetProbeCount -gt 1)
  return [pscustomobject]@{
    ok = $true
    live = [bool]$stillLive
    pid = [int]$ExpectedHostPid
    started_unix_ms = [int64]$ExpectedHostStartedUnixMs
  }
}

function Get-ClaudeSessionStateInfo {
  param([string]$SessionId)
  return [pscustomobject]@{ path = $env:CODEX_NTFY_TEST_STATE_PATH }
}

function Invoke-WithClaudeSessionLock {
  param(
    [object]$Info,
    [scriptblock]$Action,
    [object[]]$Arguments = @()
  )
  return (& $Action @Arguments)
}

function Read-JsonFile {
  param([string]$Path)
  return $script:State
}

function Write-JsonAtomic {
  param(
    [string]$Path,
    [object]$Value
  )
  $script:Writes++
  $script:PersistedState = $Value
}

$script:State = New-TestSessionState
$missingResult = Register-AudnCodeObservedHostLifetime `
  -SessionId $env:CODEX_NTFY_TEST_SESSION_ID `
  -TranscriptPath $canonicalTranscript `
  -HomePath $canonicalHome `
  -HostPid $hostPid `
  -HostStartedUnixMs $hostStarted
if ([bool]$missingResult) { throw 'registration succeeded after the observed lifetime disappeared' }
if ($script:HostProbeCount -ne 2) { throw "expected two host probes, got $script:HostProbeCount" }
if ($script:Writes -ne 0) { throw 'registration persisted state after the observed lifetime disappeared' }
$missingProperty = $script:State.PSObject.Properties['audncode_host_lifetimes']
if ($null -eq $missingProperty -or $missingProperty.Value -isnot [array] -or
    @($missingProperty.Value).Count -ne 0) {
  throw 'failed registration mutated the persisted host-lifetime array'
}

# The exact observed target must still fail its locked recheck when a different
# current owner remains live and would otherwise make the aggregate state look
# like a normal two-host conflict.
$otherPid = 4343
$otherStarted = [int64]1700000000456
$script:State = New-TestSessionState
$script:State.audncode_host_pid = [int]$otherPid
$script:State.audncode_host_started_unix_ms = [int64]$otherStarted
$script:State.audncode_multi_host_conflict = $true
$script:State.audncode_host_lifetimes = [object[]]@(
  [pscustomobject]@{ home = $canonicalHome; pid = [int]$hostPid; started_unix_ms = [int64]$hostStarted },
  [pscustomobject]@{ home = $canonicalHome; pid = [int]$otherPid; started_unix_ms = [int64]$otherStarted }
)
$script:HostProbeCount = 0
$script:TargetProbeCount = 0
$script:DisappearAfterPrecheck = $true
$script:Writes = 0
$script:PersistedState = $null
$multiHostMissing = Register-AudnCodeObservedHostLifetime `
  -SessionId $env:CODEX_NTFY_TEST_SESSION_ID `
  -TranscriptPath $canonicalTranscript `
  -HomePath $canonicalHome `
  -HostPid $hostPid `
  -HostStartedUnixMs $hostStarted
if ([bool]$multiHostMissing) { throw 'multi-host registration accepted an exited observed target' }
if ($script:TargetProbeCount -ne 2) { throw "expected two target probes, got $script:TargetProbeCount" }
if ($script:Writes -ne 0) { throw 'multi-host failed registration persisted state' }
if (@($script:State.audncode_host_lifetimes).Count -ne 2) {
  throw 'multi-host failed registration mutated the persisted lifetime envelope'
}

$script:State = New-TestSessionState
$script:HostProbeCount = 0
$script:TargetProbeCount = 0
$script:DisappearAfterPrecheck = $false
$script:Writes = 0
$script:PersistedState = $null
$registeredResult = Register-AudnCodeObservedHostLifetime `
  -SessionId $env:CODEX_NTFY_TEST_SESSION_ID `
  -TranscriptPath $canonicalTranscript `
  -HomePath $canonicalHome `
  -HostPid $hostPid `
  -HostStartedUnixMs $hostStarted
if (-not [bool]$registeredResult) { throw 'live exact host lifetime was not registered' }
if ($script:HostProbeCount -ne 2) { throw "expected two live host probes, got $script:HostProbeCount" }
if ($script:Writes -ne 1 -or $null -eq $script:PersistedState) {
  throw 'successful registration did not persist exactly once'
}
$persistedProperty = $script:PersistedState.PSObject.Properties['audncode_host_lifetimes']
if ($null -eq $persistedProperty -or $persistedProperty.Value -isnot [array]) {
  throw 'successful registration did not persist an array'
}
[object[]]$persistedLifetimes = @($persistedProperty.Value)
if ($persistedLifetimes.Count -ne 1) {
  throw "expected one persisted host lifetime, got $($persistedLifetimes.Count)"
}
$persisted = $persistedLifetimes[0]
if (-not [string]::Equals([string]$persisted.home, $canonicalHome, [StringComparison]::OrdinalIgnoreCase) -or
    [int]$persisted.pid -ne $hostPid -or
    [int64]$persisted.started_unix_ms -ne $hostStarted) {
  throw 'successful registration persisted a different host lifetime'
}
'ok'
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **self.env,
                "CODEX_NTFY_TEST_NOTIFIER": str(POWERSHELL_NOTIFIER),
                "CODEX_NTFY_TEST_AUDNCODE_HOME": str(self.audncode_home),
                "CODEX_NTFY_TEST_TRANSCRIPT": str(transcript),
                "CODEX_NTFY_TEST_STATE_PATH": str(state_path),
                "CODEX_NTFY_TEST_SESSION_ID": session_id,
                "CODEX_NTFY_TEST_HOST_PID": str(host_pid),
                "CODEX_NTFY_TEST_HOST_STARTED": str(host_started_unix_ms),
            },
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertEqual(result.stdout.strip(), "ok")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_user_prompt_during_session_start_prearm_preserves_host_lifetimes(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        for same_start_ticks in (False, True):
            with self.subTest(same_start_ticks=same_start_ticks):
                session_id = str(uuid.uuid4())
                transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                session_start_host = self.start_audncode_host(session_id)
                prompt_host = self.start_audncode_host(session_id)
                self.audncode_hosts[session_id] = prompt_host
                self.audncode_hosts[f"{session_id}-session-start"] = session_start_host
                event = self.audncode_event(session_id=session_id, transcript_path=transcript)
                base_ticks = int(time.time() * 10_000_000) + 621355968000000000
                prearmed_marker = self.temp / f"session-start-prearmed-{session_id}"
                release_marker = self.temp / f"session-start-release-{session_id}"
                session_start_errors: list[BaseException] = []

                def run_session_start() -> None:
                    try:
                        self.run_audncode_hook(
                            self.audncode_session_start_event(event),
                            host=session_start_host,
                            env_overrides={
                                "CODEX_NTFY_TEST_HOOK_START_TICKS": str(base_ticks),
                                "CODEX_NTFY_TEST_AFTER_AUDNCODE_PREARM_MS": "15000",
                                "CODEX_NTFY_TEST_AFTER_AUDNCODE_PREARM_MARKER": str(
                                    prearmed_marker
                                ),
                                "CODEX_NTFY_TEST_AFTER_AUDNCODE_PREARM_RELEASE": str(
                                    release_marker
                                ),
                            },
                        )
                    except BaseException as exc:
                        session_start_errors.append(exc)

                session_start_thread = threading.Thread(target=run_session_start)
                session_start_thread.start()
                try:
                    deadline = time.time() + 15
                    while not prearmed_marker.exists() and time.time() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(prearmed_marker.exists(), "SessionStart did not reach pre-arm")
                    prompt_ticks = base_ticks if same_start_ticks else base_ticks + 1
                    self.run_audncode_hook(
                        self.audncode_prompt_event(event, prompt="Prompt during SessionStart pre-arm"),
                        host=prompt_host,
                        env_overrides={"CODEX_NTFY_TEST_HOOK_START_TICKS": str(prompt_ticks)},
                    )
                finally:
                    release_marker.write_text("continue", encoding="ascii")
                    session_start_thread.join(timeout=30)
                self.assertFalse(session_start_thread.is_alive(), "SessionStart hook did not finish")
                if session_start_errors:
                    raise session_start_errors[0]

                _state_path, state = self.read_audncode_session_state(session_id)
                self.assertFalse(state["audncode_prompt_prearm_pending"])
                self.assertTrue(state["audncode_multi_host_conflict"], state)
                self.assertEqual(
                    {item["pid"] for item in state["audncode_host_lifetimes"]},
                    {session_start_host[0].pid, prompt_host[0].pid},
                    state,
                )
                # The submitted query must own the session even when Windows
                # reports the same process-start tick for both hook processes.
                # The displaced placeholder lifetime remains a hard terminal
                # gate until that exact host exits or supplies Stop + idle.
                self.assertEqual(state["epoch"], 2)
                self.assertEqual(state["audncode_busy_event_rank"], 2)
                self.assertEqual(state["audncode_host_pid"], prompt_host[0].pid)
                self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
                self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_session_start_waits_for_marker_or_leaves_sticky_loss(self) -> None:
        def process_started_unix_ms(process_id: int) -> int:
            observation = self.run_ok(
                [
                    str(WINDOWS_POWERSHELL),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        f"$processRecord = Get-Process -Id {process_id} -ErrorAction Stop; "
                        "([DateTimeOffset]$processRecord.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()"
                    ),
                ]
            )
            return int(observation.stdout.strip().splitlines()[-1])

        delayed = self.audncode_event()
        delayed_host = self.audncode_hosts[delayed["session_id"]]
        delayed_marker = self.audncode_home / "sessions" / f"{delayed_host[0].pid}.json"
        delayed_marker_bytes = delayed_marker.read_bytes()
        delayed_marker_record = json.loads(delayed_marker_bytes.decode("utf-8"))
        delayed_marker_record["startedAt"] = process_started_unix_ms(delayed_host[0].pid) - 1
        delayed_marker.write_text(json.dumps(delayed_marker_record), encoding="utf-8")
        restore = threading.Timer(0.4, lambda: delayed_marker.write_bytes(delayed_marker_bytes))
        restore.start()
        try:
            self.run_audncode_hook(self.audncode_session_start_event(delayed))
        finally:
            restore.join(timeout=5)
            if not delayed_marker.exists():
                delayed_marker.write_bytes(delayed_marker_bytes)
        _state_path, delayed_state = self.read_audncode_session_state(delayed["session_id"])
        self.assertEqual(delayed_state["state"], "busy")
        self.assertFalse(delayed_state["audncode_prompt_prearm_pending"])

        missing = self.audncode_event()
        missing_host = self.audncode_hosts[missing["session_id"]]
        missing_marker = self.audncode_home / "sessions" / f"{missing_host[0].pid}.json"
        missing_marker_bytes = missing_marker.read_bytes()
        missing_marker_record = json.loads(missing_marker_bytes.decode("utf-8"))
        missing_marker_record["startedAt"] = process_started_unix_ms(missing_host[0].pid) - 30_000
        missing_marker.write_text(json.dumps(missing_marker_record), encoding="utf-8")
        try:
            self.run_audncode_hook(self.audncode_session_start_event(missing))
        finally:
            missing_marker.write_bytes(missing_marker_bytes)
        session_states = []
        fallback_states = []
        for path in (self.state / "claude-sessions").glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("session_id") == missing["session_id"] and "state" in state:
                session_states.append(state)
            if state.get("kind") == "audncode-ingress-fallback":
                fallback_states.append(state)
        self.assertEqual(len(session_states), 1, self.state_debug())
        self.assertTrue(session_states[0]["audncode_prompt_prearm_pending"])
        self.assertEqual(session_states[0]["audncode_host_pid"], 0)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertTrue(
            any(int(state.get("lost_hook_start_ticks", 0)) > 0 for state in fallback_states),
            self.state_debug(),
        )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_user_prompt_prearm_commits_once_on_success(self) -> None:
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        host_process, _host_started_at = self.audncode_hosts[session_id]

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="First prompt"))
        _state_path, first = self.read_audncode_session_state(session_id)
        self.assertEqual(first["state"], "busy")
        self.assertEqual(first["epoch"], 1)
        self.assertFalse(first["audncode_prompt_prearm_pending"])
        self.assertEqual(first["audncode_prompt_prearm_token"], "")
        self.assertGreater(first["busy_hook_start_ticks"], 0)
        self.assertEqual(first["audncode_host_pid"], host_process.pid)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Second prompt"))
        _state_path, second = self.read_audncode_session_state(session_id)
        self.assertEqual(second["state"], "busy")
        self.assertEqual(second["epoch"], 2)
        self.assertFalse(second["audncode_prompt_prearm_pending"])
        self.assertEqual(second["audncode_prompt_prearm_token"], "")
        self.assertGreater(second["busy_hook_start_ticks"], first["busy_hook_start_ticks"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_killed_user_prompt_prearm_stays_busy_and_blocks_completion(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Old prompt"))
        self.run_audncode_hook({**event, "last_assistant_message": "Old result must not escape."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        state_path, old_state = self.read_audncode_session_state(session_id)
        self.assertEqual(old_state["state"], "idle")
        self.assertEqual(old_state["epoch"], 1)
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)

        host_process, _host_started_at = self.audncode_hosts[session_id]
        marker_path = self.audncode_home / "sessions" / f"{host_process.pid}.json"
        marker_bytes = marker_path.read_bytes()
        marker = json.loads(marker_bytes.decode("utf-8"))
        marker["sessionId"] = str(uuid.uuid4())
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        try:
            killed = self.run_audncode_hook(
                self.audncode_prompt_event(event, prompt="New prompt whose hook is killed"),
                kill_when_prearm_path=state_path,
                expect_success=False,
            )
        finally:
            marker_path.write_bytes(marker_bytes)
        self.assertNotEqual(killed.returncode, 0)

        _state_path, prearmed = self.read_audncode_session_state(session_id)
        self.assertEqual(prearmed["state"], "busy")
        self.assertEqual(prearmed["epoch"], 2)
        self.assertTrue(prearmed["audncode_prompt_prearm_pending"])
        self.assertRegex(prearmed["audncode_prompt_prearm_token"], r"^[0-9a-f]{32}$")
        self.assertEqual(prearmed["audncode_host_pid"], 0)

        # Neither a Stop nor idle_prompt may turn an incomplete pre-arm into a
        # final candidate, even after host correlation becomes available again.
        self.run_audncode_hook({**event, "last_assistant_message": "False final result."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        _state_path, after = self.read_audncode_session_state(session_id)
        self.assertTrue(after["audncode_prompt_prearm_pending"])
        self.assertEqual(after["epoch"], 2)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_older_delayed_user_prompt_cannot_replace_newer_prearm(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        newer_host = self.start_audncode_host(session_id)
        older_host = self.start_audncode_host(session_id)
        self.audncode_hosts[session_id] = newer_host
        self.audncode_hosts[f"{session_id}-older"] = older_host
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        equal_hook_ticks = str(int(time.time() * 10_000_000) + 621355968000000000)
        child_started = self.temp / "older-prompt-child-started"
        newer_prearmed = self.temp / "newer-prompt-prearmed"
        release_newer = self.temp / "release-newer-prompt"
        older_results: list[subprocess.CompletedProcess[bytes]] = []
        older_errors: list[BaseException] = []
        newer_results: list[subprocess.CompletedProcess[bytes]] = []
        newer_errors: list[BaseException] = []

        def run_older() -> None:
            try:
                older_results.append(
                    self.run_audncode_hook(
                        self.audncode_prompt_event(event, prompt="Older delayed prompt"),
                        host=older_host,
                        delay_input_ms=10000,
                        child_started_path=child_started,
                        env_overrides={"CODEX_NTFY_TEST_HOOK_START_TICKS": equal_hook_ticks},
                    )
                )
            except BaseException as exc:  # propagate assertion failures to the test thread
                older_errors.append(exc)

        older_thread = threading.Thread(target=run_older)
        older_thread.start()
        deadline = time.time() + 10
        while not child_started.exists() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(child_started.exists(), "older AudnCode hook child did not start")

        def run_newer() -> None:
            try:
                newer_results.append(
                    self.run_audncode_hook(
                        self.audncode_prompt_event(
                            event, prompt="Newer prompt reaches pre-arm first"
                        ),
                        host=newer_host,
                        env_overrides={
                            "CODEX_NTFY_TEST_HOOK_START_TICKS": equal_hook_ticks,
                            "CODEX_NTFY_TEST_AFTER_AUDNCODE_PREARM_MS": "15000",
                            "CODEX_NTFY_TEST_AFTER_AUDNCODE_PREARM_MARKER": str(
                                newer_prearmed
                            ),
                            "CODEX_NTFY_TEST_AFTER_AUDNCODE_PREARM_RELEASE": str(
                                release_newer
                            ),
                        },
                    )
                )
            except BaseException as exc:
                newer_errors.append(exc)

        newer_thread = threading.Thread(target=run_newer)
        newer_thread.start()
        try:
            deadline = time.time() + 15
            while not newer_prearmed.exists() and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(newer_prearmed.exists(), "newer hook did not reach pre-arm barrier")

            older_thread.join(timeout=30)
            self.assertFalse(older_thread.is_alive(), "older AudnCode hook did not finish")
            if older_errors:
                raise older_errors[0]
            self.assertEqual(len(older_results), 1)
            _state_path, while_newer_prearmed = self.read_audncode_session_state(session_id)
            self.assertTrue(while_newer_prearmed["audncode_prompt_prearm_pending"])
            self.assertEqual(
                {
                    item["pid"]
                    for item in while_newer_prearmed[
                        "audncode_prompt_previous_host_lifetimes"
                    ]
                },
                {newer_host[0].pid, older_host[0].pid},
                while_newer_prearmed,
            )
        finally:
            release_newer.write_text("continue", encoding="ascii")
            newer_thread.join(timeout=30)
        self.assertFalse(newer_thread.is_alive(), "newer AudnCode hook did not finish")
        if newer_errors:
            raise newer_errors[0]
        self.assertEqual(len(newer_results), 1)
        _state_path, after_older = self.read_audncode_session_state(session_id)
        self.assertEqual(after_older["epoch"], 1)
        self.assertEqual(after_older["busy_hook_start_ticks"], int(equal_hook_ticks))
        self.assertFalse(after_older["audncode_prompt_prearm_pending"])
        self.assertEqual(after_older["audncode_host_pid"], newer_host[0].pid)
        self.assertTrue(after_older["audncode_multi_host_conflict"], after_older)
        self.assertEqual(
            {item["pid"] for item in after_older["audncode_host_lifetimes"]},
            {newer_host[0].pid, older_host[0].pid},
            after_older,
        )

        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="Newer prompt failed while the delayed window remains live.",
        )
        self.run_audncode_hook(failure, host=newer_host)
        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            deadline = time.time() + 30
            gate_reason = ""
            while time.time() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if len(pending) == 1:
                    try:
                        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                        gate_reason = str(record.get("gate_reason", ""))
                    except (OSError, json.JSONDecodeError):
                        gate_reason = ""
                    if gate_reason == "audncode-multi-host-session-active":
                        break
                time.sleep(0.05)
            self.assertEqual(
                gate_reason,
                "audncode-multi-host-session-active",
                self.state_debug(),
            )
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

        self.exit_audncode_host(f"{session_id}-older")
        self.run_audncode_hook(failure, host=newer_host)
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Newer prompt failed", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_losing_prompt_handoff_after_winner_idle_stays_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        winner = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            message="Winner completed before losing discovery returned.",
        )
        winner_host = self.audncode_hosts[session_id]
        loser_host = self.start_audncode_host(session_id)
        self.audncode_hosts[f"{session_id}:late-loser"] = loser_host
        equal_hook_ticks = str(int(time.time() * 10_000_000) + 621355968000000000)
        self.run_audncode_hook(
            self.audncode_prompt_event(winner, prompt="Winner reaches session first"),
            host=winner_host,
            env_overrides={"CODEX_NTFY_TEST_HOOK_START_TICKS": equal_hook_ticks},
        )
        loser_started = self.temp / "winner-idle-loser-started"
        loser_results: list[subprocess.CompletedProcess[bytes]] = []
        loser_errors: list[BaseException] = []

        def run_loser() -> None:
            try:
                loser_results.append(
                    self.run_audncode_hook(
                        self.audncode_prompt_event(winner, prompt="Losing delayed query"),
                        host=loser_host,
                        # Stay below the production stdin safety deadline while
                        # leaving enough room for the winner's prompt/Stop/idle.
                        delay_input_ms=18000,
                        child_started_path=loser_started,
                        env_overrides={
                            "CODEX_NTFY_TEST_HOOK_START_TICKS": equal_hook_ticks
                        },
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                loser_errors.append(exc)

        loser_thread = threading.Thread(target=run_loser)
        loser_thread.start()
        deadline = time.time() + 10
        loser_guard_pending = False
        while time.time() < deadline:
            try:
                _guard_path, loser_guard = self.read_audncode_ingress_guard(
                    loser_host[0].pid
                )
                loser_guard_pending = len(loser_guard.get("pending_tokens", [])) == 1
            except AssertionError:
                loser_guard_pending = False
            if loser_started.exists() and loser_guard_pending:
                break
            time.sleep(0.02)
        self.assertTrue(loser_started.exists(), "losing hook child did not start")
        self.assertTrue(loser_guard_pending, self.state_debug())

        self.run_audncode_hook(winner, host=winner_host)
        self.run_audncode_hook(self.audncode_idle_event(winner), host=winner_host)
        _state_path, winner_idle = self.read_audncode_session_state(session_id)
        self.assertEqual(winner_idle["state"], "idle", winner_idle)
        self.assertEqual(winner_idle["audncode_host_pid"], winner_host[0].pid, winner_idle)
        self.assertFalse(winner_idle["audncode_multi_host_conflict"], winner_idle)
        self.assertTrue(loser_thread.is_alive(), "loser returned before winner became idle")
        _guard_path, pending_loser_guard = self.read_audncode_ingress_guard(
            loser_host[0].pid
        )
        self.assertEqual(len(pending_loser_guard["pending_tokens"]), 1, pending_loser_guard)

        loser_thread.join(timeout=45)
        self.assertFalse(loser_thread.is_alive(), "losing delayed hook did not finish")
        if loser_errors:
            raise loser_errors[0]
        self.assertEqual(len(loser_results), 1)
        _guard_path, loser_guard = self.read_audncode_ingress_guard(loser_host[0].pid)
        self.assertFalse(loser_guard["lost"], loser_guard)
        self.assertEqual(loser_guard["pending_tokens"], [], loser_guard)
        self.assertTrue(loser_guard["safe_to_finalize"], loser_guard)

        # The winner candidate is retained but gated by the exact late losing
        # lifetime until that host supplies its own ordered terminal pair.
        _state_path, handed_off = self.read_audncode_session_state(session_id)
        self.assertEqual(handed_off["state"], "idle", handed_off)
        self.assertEqual(handed_off["audncode_host_pid"], winner_host[0].pid, handed_off)
        self.assertTrue(handed_off["audncode_multi_host_conflict"], handed_off)
        self.assertEqual(
            {item["pid"] for item in handed_off["audncode_host_lifetimes"]},
            {winner_host[0].pid, loser_host[0].pid},
            handed_off,
        )
        loser_background_runtimes = []
        for runtime_path in (self.state / "claude-sessions").glob("audn-runtime-*.json"):
            runtime = self.read_json_retry(runtime_path)
            if (
                runtime.get("host_pid") == loser_host[0].pid
                and runtime.get("host_started_unix_ms") == loser_host[1]
            ):
                loser_background_runtimes.append(runtime)
        self.assertEqual(len(loser_background_runtimes), 1, self.state_debug())
        loser_background = loser_background_runtimes[0]
        self.assertTrue(loser_background.get("registry_valid"), loser_background)
        loser_background_lineage = [
            item
            for item in loser_background.get("sessions", [])
            if item.get("session_id") == session_id
            and os.path.normcase(item.get("transcript_path", ""))
            == os.path.normcase(str(transcript))
        ]
        self.assertEqual(
            len(loser_background_lineage),
            1,
            loser_background,
        )
        loser_cron_runtimes = []
        for runtime_path in (self.state / "claude-sessions").glob("audn-cron-*.json"):
            runtime = self.read_json_retry(runtime_path)
            if (
                runtime.get("host_pid") == loser_host[0].pid
                and runtime.get("host_started_unix_ms") == loser_host[1]
            ):
                loser_cron_runtimes.append(runtime)
        self.assertEqual(len(loser_cron_runtimes), 1, self.state_debug())
        loser_cron = loser_cron_runtimes[0]
        self.assertTrue(loser_cron.get("registry_valid"), loser_cron)
        self.assertEqual(
            [item.get("session_id") for item in loser_cron.get("sessions", [])],
            [session_id],
            loser_cron,
        )
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

        loser_stop = {
            **winner,
            "last_assistant_message": "Losing host must never replace the winner.",
        }
        self.run_audncode_hook(loser_stop, host=loser_host)
        self.run_audncode_hook(self.audncode_idle_event(loser_stop), host=loser_host)
        _state_path, retired = self.read_audncode_session_state(session_id)
        self.assertFalse(retired["audncode_multi_host_conflict"], retired)
        self.assertEqual(
            [item["pid"] for item in retired["audncode_host_lifetimes"]],
            [winner_host[0].pid],
            retired,
        )
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Winner completed", payloads[0]["message"])
        self.assertNotIn("Losing host", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_losing_prompt_handoff_active_background_and_cron_stays_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "losing-prompt-active-runtime"
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            message="Winner stays blocked by losing host runtime.",
            project_root=project_root,
        )
        winner_host = self.audncode_hosts[session_id]
        loser_host = self.start_audncode_host(session_id, cwd=event["cwd"])
        self.audncode_hosts[f"{session_id}:active-loser"] = loser_host
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Arm losing host cron runtime"),
            host=loser_host,
        )
        cron_id = "a1b2c3d4"
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                event,
                action="create",
                cron_id=cron_id,
                durable=False,
            ),
            host=loser_host,
        )
        equal_hook_ticks = str(int(time.time() * 10_000_000) + 621355968000000000)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Winner reaches session first"),
            host=winner_host,
            env_overrides={"CODEX_NTFY_TEST_HOOK_START_TICKS": equal_hook_ticks},
        )
        loser_started = self.temp / "active-runtime-loser-started"
        loser_errors: list[BaseException] = []

        def run_loser() -> None:
            try:
                self.run_audncode_hook(
                    self.audncode_prompt_event(event, prompt="Losing delayed active query"),
                    host=loser_host,
                    delay_input_ms=18000,
                    child_started_path=loser_started,
                    env_overrides={"CODEX_NTFY_TEST_HOOK_START_TICKS": equal_hook_ticks},
                )
            except BaseException as error:  # surfaced in the main test thread below
                loser_errors.append(error)

        loser_thread = threading.Thread(target=run_loser)
        loser_thread.start()
        deadline = time.monotonic() + 10
        loser_guard_pending = False
        while time.monotonic() < deadline:
            try:
                _guard_path, loser_guard = self.read_audncode_ingress_guard(loser_host[0].pid)
                loser_guard_pending = len(loser_guard.get("pending_tokens", [])) == 1
            except AssertionError:
                loser_guard_pending = False
            if loser_started.exists() and loser_guard_pending:
                break
            time.sleep(0.02)
        self.assertTrue(loser_started.exists(), "losing hook child did not start")
        self.assertTrue(loser_guard_pending, self.state_debug())

        self.run_audncode_hook(event, host=winner_host)
        self.run_audncode_hook(self.audncode_idle_event(event), host=winner_host)
        self.assertTrue(loser_thread.is_alive(), "loser returned before winner became idle")
        loser_thread.join(timeout=45)
        self.assertFalse(loser_thread.is_alive(), self.state_debug())
        if loser_errors:
            raise loser_errors[0]

        _state_path, handed_off = self.read_audncode_session_state(session_id)
        self.assertTrue(handed_off["audncode_multi_host_conflict"], handed_off)
        self.assertEqual(
            {item["pid"] for item in handed_off["audncode_host_lifetimes"]},
            {winner_host[0].pid, loser_host[0].pid},
            handed_off,
        )

        agent_id = "agent-losing-runtime"
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": agent_id},
                "tool_use_id": "tool-losing-runtime-agent",
            },
            host=loser_host,
        )
        loser_stop = {**event, "last_assistant_message": "Losing host reached idle."}
        self.run_audncode_hook(loser_stop, host=loser_host)
        self.run_audncode_hook(self.audncode_idle_event(loser_stop), host=loser_host)
        _state_path, background_blocked = self.read_audncode_session_state(session_id)
        self.assertTrue(background_blocked["audncode_multi_host_conflict"], background_blocked)
        self.assertEqual(len(background_blocked["audncode_host_lifetimes"]), 2, background_blocked)
        self.assertEqual(
            len(background_blocked["audncode_superseded_host_stop_proofs"]),
            1,
            background_blocked,
        )

        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "TaskStop",
                "tool_input": {"task_id": agent_id},
                "tool_response": {
                    "message": f"Stopped task {agent_id}",
                    "task_id": agent_id,
                    "task_type": "local_agent",
                },
                "tool_use_id": "tool-stop-losing-runtime-agent",
            },
            host=loser_host,
        )
        loser_background = [
            self.read_json_retry(path)
            for path in (self.state / "claude-sessions").glob("audn-runtime-*.json")
        ]
        loser_background = [
            runtime
            for runtime in loser_background
            if runtime.get("host_pid") == loser_host[0].pid
            and runtime.get("host_started_unix_ms") == loser_host[1]
        ]
        self.assertEqual(len(loser_background), 1, self.state_debug())
        self.assertEqual(loser_background[0].get("background_ids"), [], loser_background[0])

        # With the background agent closed, the still-live session-only cron is
        # independently sufficient to keep the superseded lifetime hard-gated.
        self.run_audncode_hook(self.audncode_idle_event(loser_stop), host=loser_host)
        _state_path, cron_blocked = self.read_audncode_session_state(session_id)
        self.assertTrue(cron_blocked["audncode_multi_host_conflict"], cron_blocked)
        self.assertEqual(len(cron_blocked["audncode_host_lifetimes"]), 2, cron_blocked)
        self.assertEqual(len(cron_blocked["audncode_superseded_host_stop_proofs"]), 1, cron_blocked)
        loser_crons = [
            self.read_json_retry(path)
            for path in (self.state / "claude-sessions").glob("audn-cron-*.json")
        ]
        loser_crons = [
            runtime
            for runtime in loser_crons
            if runtime.get("host_pid") == loser_host[0].pid
            and runtime.get("host_started_unix_ms") == loser_host[1]
        ]
        self.assertEqual(len(loser_crons), 1, self.state_debug())
        self.assertEqual(
            [item.get("id") for item in loser_crons[0].get("crons", [])],
            [cron_id],
            loser_crons[0],
        )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_delayed_stop_and_stop_failure_cannot_cross_prompt_epochs(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Prompt A"))
        old_failure, _old_user_uuid, _old_assistant_uuid = (
            self.append_audncode_stop_failure_proof(
                event,
                error="rate_limit",
                message="Late API failure from A.",
            )
        )
        delayed_stop = subprocess.Popen(
            self.audncode_hook_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        time.sleep(1.0)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Prompt B"))
        delayed_stdout, delayed_stderr = delayed_stop.communicate(
            input=json.dumps({**event, "last_assistant_message": "Late result from A."}).encode("utf-8"),
            timeout=60,
        )
        self.assertEqual(
            delayed_stop.returncode,
            0,
            msg=f"stdout={delayed_stdout!r}\nstderr={delayed_stderr!r}",
        )
        self.assertEqual(delayed_stdout.decode("utf-8").strip(), "{}")
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        self.run_audncode_hook(old_failure)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        self.run_audncode_hook({**event, "last_assistant_message": "Current result from B."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Current result from B.", payloads[0]["message"])
        self.assertNotIn("Late result", payloads[0]["message"])
        self.assertNotIn("Late API failure", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_notifies_without_idle_prompt_for_supported_api_errors(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        cases = (
            ("authentication_failed", "Not logged in - run /login", None),
            ("rate_limit", "API Error: Rate limit reached", "retry after 60 seconds"),
            ("invalid_request", "Prompt is too long", "actual: 210000, maximum: 200000"),
        )
        for index, (error, message, details) in enumerate(cases):
            session_id = str(uuid.uuid4())
            transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
            transcript.write_text("", encoding="utf-8")
            event = self.audncode_event(session_id=session_id, transcript_path=transcript)
            self.run_audncode_hook(
                self.audncode_prompt_event(event, prompt=f"Failure case {index}")
            )
            failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
                event,
                error=error,
                message=message,
                error_details=details,
            )
            self.run_audncode_hook(failure)
            self.run_ok(self.worker_command("powershell"), timeout=120)
            self.wait_for_payloads(index + 1)
            self.exit_audncode_host(session_id)

        payloads = self.wait_for_payloads(len(cases))
        self.assertEqual(len(payloads), len(cases), self.state_debug())
        combined = "\n".join(payload["message"] for payload in payloads)
        for _error, message, _details in cases:
            self.assertIn(message, combined)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_suppresses_transient_and_recovered_failures(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        _event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("transient then recovered")
        )
        marker_env = {"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)}
        self.run_audncode_hook(failure, env_overrides=marker_env)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        queued = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(queued["audncode_recovery_managed"], queued)
        self.assertEqual(queued["audncode_recovery_initial_revision"], 1, queued)
        self.assertEqual(queued["audncode_recovery_observed_revision"], 1, queued)
        self.assertEqual(queued["audncode_recovery_terminal_hash"], "", queued)

        worker = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 30
            active: dict[str, object] = {}
            while time.monotonic() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if len(pending) == 1:
                    try:
                        active = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError):
                        active = {}
                    if active.get("gate_reason") == "audncode-managed-recovery-active":
                        break
                time.sleep(0.05)
            self.assertEqual(
                active.get("gate_reason"), "audncode-managed-recovery-active", active
            )
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        recovered = self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="recovered",
            reason="same-session-retry-scheduled",
            failure_record_uuid=failure_uuid,
        )
        time.sleep(0.35)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        for directory in ("pending", "outbox", "sent", "dead"):
            self.assertFalse(list((self.state / directory).glob("*.json")), self.state_debug())
        suppressed = list((self.state / "suppressed").glob("*.json"))
        self.assertEqual(len(suppressed), 1, self.state_debug())
        self.assertRegex(suppressed[0].name, r"^r-[0-9a-f]{64}\.json$")
        receipt = json.loads(suppressed[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(receipt["schema"], 1, receipt)
        self.assertEqual(receipt["reason"], "audncode-managed-recovery-succeeded", receipt)
        self.assertEqual(receipt["key"], queued["key"], receipt)
        self.assertEqual(receipt["thread_id"], queued["thread_id"], receipt)
        self.assertEqual(receipt["turn_id"], queued["turn_id"], receipt)
        self.assertEqual(receipt["candidate_identity"], queued["candidate_identity"], receipt)
        self.assertRegex(receipt["candidate_revision"], r"^[0-9a-f]{32}$")
        self.assertEqual(receipt["audncode_stop_failure_uuid"], failure_uuid, receipt)
        self.assertEqual(receipt["audncode_recovery_binding"], queued["audncode_recovery_binding"], receipt)
        self.assertRegex(receipt["audncode_recovery_terminal_hash"], r"^[0-9a-f]{64}$")
        self.assertIsNone(receipt["successor"], receipt)
        self.assertEqual(receipt["successor_hash"], "", receipt)
        receipt_bytes = suppressed[0].read_bytes()

        # Recovery tombstones are not ordinary suppressed receipts. Even an old
        # valid tombstone must survive maintenance because age cannot prove that
        # a delayed StopFailure replay is safe. A replay that lost the launcher's
        # marker environment must still fail closed against the same tombstone.
        stale = time.time() - (90 * 24 * 60 * 60)
        os.utime(suppressed[0], (stale, stale))
        self.run_ok(self.worker_command("powershell"), timeout=120)
        self.assertTrue(suppressed[0].exists(), self.state_debug())
        self.assertEqual(suppressed[0].read_bytes(), receipt_bytes)
        self.run_audncode_hook(failure)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

        # A duplicate asynchronous StopFailure can be replayed after the
        # canonical pending record was removed. Even an adversarial rev2 rewrite
        # from recovered to exhausted must hit the durable suppression receipt.
        rewritten = {
            **recovered,
            "state": "exhausted",
            "reason": "launcher-unrecoverable",
            "updated_unix_ms": int(recovered["updated_unix_ms"]) + 1,
        }
        self.write_audncode_recovery_marker(marker_path, rewritten)
        self.run_audncode_hook(failure, env_overrides=marker_env)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertEqual(suppressed[0].read_bytes(), receipt_bytes)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_recovered_tombstone_allows_successful_stop_and_blocks_old_failure_replays(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("recovered then successful Stop")
        )
        marker_env = {"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)}
        self.run_audncode_hook(failure, env_overrides=marker_env)
        self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="recovered",
            reason="runtime-completed",
            failure_record_uuid=failure_uuid,
        )
        self.run_ok(self.worker_command("powershell"), timeout=120)

        tombstones = list((self.state / "suppressed").glob("r-*.json"))
        self.assertEqual(len(tombstones), 1, self.state_debug())
        tombstone_bytes = tombstones[0].read_bytes()
        success = {
            **event,
            "last_assistant_message": "Recovered provider attempt completed successfully.",
        }
        self.run_audncode_hook(success)

        # Replay the exact old asynchronous failure while the successful Stop is
        # pending. Its random candidate_revision must not replace that Stop.
        self.run_audncode_hook(failure, env_overrides=marker_env)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        successful_record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(successful_record["candidate_kind"], "audncode_stop", successful_record)
        cleared_tombstone = json.loads(tombstones[0].read_text(encoding="utf-8-sig"))
        self.assertIsNone(
            cleared_tombstone["successor"],
            {"tombstone": cleared_tombstone, "state": self.state_debug()},
        )
        self.assertEqual(cleared_tombstone["successor_hash"], "", cleared_tombstone)
        self.assertNotIn("Recovered provider attempt completed successfully.", tombstones[0].read_text(encoding="utf-8-sig"))
        self.run_audncode_hook(self.audncode_idle_event(success))
        self.run_ok(self.worker_command("powershell"), timeout=120)

        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Recovered provider attempt completed successfully.", payloads[0]["message"])
        self.assertNotIn("Managed provider failure", payloads[0]["message"])
        self.assertEqual(tombstones[0].read_bytes(), tombstone_bytes)

        # Once the completion is sent, the sent receipt and the identity-specific
        # recovery tombstone independently prevent the old failure from reviving.
        self.run_audncode_hook(failure, env_overrides=marker_env)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertEqual(len(list((self.state / "sent").glob("*.json"))), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_successful_stop_linearizes_recovery_tombstone_before_worker(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("Stop races recovery tombstone")
        )
        marker_env = {"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)}
        self.run_audncode_hook(failure, env_overrides=marker_env)
        self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="recovered",
            reason="same-session-retry-scheduled",
            failure_record_uuid=failure_uuid,
        )

        # No worker has observed rev2 yet. Upgrade-PendingRecordFromStop must
        # publish the tombstone and replace the old failure under one per-key lock.
        success = {
            **event,
            "last_assistant_message": "Successful Stop won the recovery race.",
        }
        self.run_audncode_hook(success)
        tombstones = list((self.state / "suppressed").glob("r-*.json"))
        self.assertEqual(len(tombstones), 1, self.state_debug())
        tombstone_bytes = tombstones[0].read_bytes()
        pending_path = next((self.state / "pending").glob("*.json"))
        pending = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(pending["candidate_kind"], "audncode_stop", pending)
        cleared_tombstone = json.loads(tombstones[0].read_text(encoding="utf-8-sig"))
        self.assertIsNone(cleared_tombstone["successor"], cleared_tombstone)
        self.assertEqual(cleared_tombstone["successor_hash"], "", cleared_tombstone)
        self.assertNotIn("Successful Stop won the recovery race.", tombstones[0].read_text(encoding="utf-8-sig"))

        self.run_audncode_hook(failure, env_overrides=marker_env)
        after_replay = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(after_replay["candidate_revision"], pending["candidate_revision"])
        self.assertEqual(after_replay["candidate_kind"], "audncode_stop", after_replay)
        self.assertEqual(tombstones[0].read_bytes(), tombstone_bytes)

        self.run_audncode_hook(self.audncode_idle_event(success))
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Successful Stop won the recovery race.", payloads[0]["message"])
        self.run_audncode_hook(failure, env_overrides=marker_env)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

    @unittest.skipUnless(
        os.name == "nt"
        and WINDOWS_POWERSHELL.exists()
        and POWERSHELL_7 is not None
        and POWERSHELL_7.exists(),
        "AudnCode cross-PowerShell recovery test",
    )
    def test_audncode_recovery_journal_restores_successful_stop_after_crash_gap(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("journal survives hook crash gap")
        )
        marker_env = {"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)}
        self.run_audncode_hook(failure, env_overrides=marker_env)
        pending_path = next((self.state / "pending").glob("*.json"))
        self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="recovered",
            reason="runtime-completed",
            failure_record_uuid=failure_uuid,
        )

        success = {
            **event,
            "last_assistant_message": "Journal <Qwen & recovery> restored 'successfully'.",
        }
        crashed = self.run_audncode_hook(
            success,
            env_overrides={"CODEX_NTFY_TEST_EXIT_AFTER_RECOVERY_JOURNAL": "1"},
            expect_success=False,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr.decode("utf-8", errors="replace"))
        tombstone_path = next((self.state / "suppressed").glob("r-*.json"))
        tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(tombstone.get("successor"), str, tombstone)
        journaled_successor = json.loads(tombstone["successor"])
        self.assertEqual(journaled_successor["candidate_kind"], "audncode_stop", tombstone)
        self.assertRegex(str(tombstone.get("successor_hash", "")), r"^[0-9a-f]{64}$")
        journal_indexes = list((self.state / "recovery-journals").glob("r-*.json"))
        self.assertEqual(len(journal_indexes), 1, self.state_debug())
        self.assertEqual(journal_indexes[0].name, tombstone_path.name)

        # The PS5 hook exited after the write-ahead commit but before the pending
        # replacement. Force a PS7 crash immediately after restoring the Stop so
        # the second two-file gap is deterministic rather than timing-dependent.
        worker_command = self.worker_command("powershell")
        worker_command[0] = str(POWERSHELL_7)
        restore_env = {
            **self.env,
            "CODEX_NTFY_TEST_EXIT_AFTER_RECOVERY_SUCCESSOR_WRITE": "1",
        }
        restoring = subprocess.run(
            worker_command,
            env=restore_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            restoring.returncode,
            92,
            {"stdout": restoring.stdout, "stderr": restoring.stderr, "state": self.state_debug()},
        )
        restored_text = pending_path.read_text(encoding="utf-8-sig")
        restored = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(
            restored["candidate_kind"],
            "audncode_stop",
            {
                "record": restored,
                "state": self.state_debug(),
                "worker_stdout": restoring.stdout,
                "worker_stderr": restoring.stderr,
            },
        )
        self.assertEqual(
            restored["event"]["last-assistant-message"],
            "Journal <Qwen & recovery> restored 'successfully'.",
            restored,
        )
        still_journaled = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(still_journaled.get("successor"), str, still_journaled)
        self.assertEqual(
            len(list((self.state / "recovery-journals").glob("r-*.json"))),
            1,
            self.state_debug(),
        )

        # Force the third crash gap too: privacy-bearing content is erased first,
        # while the content-free active index is deliberately left for restart.
        clear_env = {
            **self.env,
            "CODEX_NTFY_TEST_EXIT_AFTER_RECOVERY_JOURNAL_CLEAR": "1",
        }
        clearing = subprocess.run(
            worker_command,
            env=clear_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            clearing.returncode,
            93,
            {"stdout": clearing.stdout, "stderr": clearing.stderr, "state": self.state_debug()},
        )
        privacy_cleared = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
        self.assertIsNone(privacy_cleared.get("successor"), privacy_cleared)
        self.assertEqual(privacy_cleared.get("successor_hash"), "", privacy_cleared)
        self.assertEqual(pending_path.read_text(encoding="utf-8-sig"), restored_text)
        self.assertEqual(
            len(list((self.state / "recovery-journals").glob("r-*.json"))),
            1,
            self.state_debug(),
        )

        # A clean PS7 restart removes only the stale content-free index and leaves
        # the durable Stop bytes untouched.
        cleanup_command = self.worker_command("powershell")
        cleanup_command[0] = str(POWERSHELL_7)
        cleanup_worker = subprocess.Popen(
            cleanup_command,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            cleanup_deadline = time.monotonic() + 30
            while time.monotonic() < cleanup_deadline:
                if not list((self.state / "recovery-journals").glob("r-*.json")):
                    break
                time.sleep(0.05)
        finally:
            if cleanup_worker.poll() is None:
                cleanup_worker.terminate()
            cleanup_worker.communicate(timeout=10)
        cleared_tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(pending_path.read_text(encoding="utf-8-sig"), restored_text)
        self.assertIsNone(
            cleared_tombstone["successor"],
            {"tombstone": cleared_tombstone, "state": self.state_debug()},
        )
        self.assertEqual(cleared_tombstone["successor_hash"], "", cleared_tombstone)
        tombstone_text = tombstone_path.read_text(encoding="utf-8-sig")
        self.assertNotIn("Qwen", tombstone_text)
        self.assertNotIn(str(self.audncode_home), tombstone_text)
        self.assertNotIn(str(event["transcript_path"]), tombstone_text)
        self.assertFalse(
            list((self.state / "recovery-journals").glob("r-*.json")),
            self.state_debug(),
        )
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

        self.run_audncode_hook(self.audncode_idle_event(success))
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Journal <Qwen & recovery> restored 'successfully'.", payloads[0]["message"])
        self.run_audncode_hook(failure, env_overrides=marker_env)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_recovery_journal_clears_after_exact_coalescing_suppression(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("journal coalescing cleanup")
        )
        marker_env = {"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)}
        self.run_audncode_hook(failure, env_overrides=marker_env)
        pending_path = next((self.state / "pending").glob("*.json"))
        durable_failure = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="recovered",
            reason="runtime-completed",
            failure_record_uuid=failure_uuid,
        )

        success = {
            **event,
            "last_assistant_message": "Coalesced private recovery journal content.",
        }
        crashed = self.run_audncode_hook(
            success,
            env_overrides={"CODEX_NTFY_TEST_EXIT_AFTER_RECOVERY_JOURNAL": "1"},
            expect_success=False,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr.decode("utf-8", errors="replace"))
        tombstone_path = next((self.state / "suppressed").glob("r-*.json"))
        self.assertEqual(
            len(list((self.state / "recovery-journals").glob("r-*.json"))),
            1,
            self.state_debug(),
        )

        # Reproduce the durable state left when coalescing supersedes the old
        # failure between receipt publication and successor restoration.
        superseded_path = self.state / "suppressed" / f"{durable_failure['key']}.json"
        superseded_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "key": durable_failure["key"],
                    "thread_id": durable_failure["thread_id"],
                    "turn_id": durable_failure["turn_id"],
                    "origin": durable_failure["origin"],
                    "candidate_revision": durable_failure["candidate_revision"],
                    "suppressed_at": "2026-08-30T00:00:00+00:00",
                    "reason": "superseded",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        pending_path.unlink()

        self.run_ok(self.worker_command("powershell"), timeout=120)
        cleared_tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
        self.assertIsNone(cleared_tombstone["successor"], cleared_tombstone)
        self.assertEqual(cleared_tombstone["successor_hash"], "", cleared_tombstone)
        tombstone_text = tombstone_path.read_text(encoding="utf-8-sig")
        self.assertNotIn("Coalesced private recovery journal content", tombstone_text)
        self.assertNotIn(str(self.audncode_home), tombstone_text)
        self.assertNotIn(str(event["transcript_path"]), tombstone_text)
        self.assertFalse(
            list((self.state / "recovery-journals").glob("r-*.json")),
            self.state_debug(),
        )
        self.assertTrue(superseded_path.exists(), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

        self.run_audncode_hook(failure, env_overrides=marker_env)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_recovery_journal_cleans_logical_s2_but_retains_identity_drift(self) -> None:
        matching = self.seed_audncode_recovery_journal_fixture("durable-s2")
        matching_s1 = matching["successor"]
        self.assertIsInstance(matching_s1, dict)
        matching_s2 = json.loads(json.dumps(matching_s1))
        matching_s2["candidate_revision"] = uuid.uuid4().hex
        matching_s2["created_unix_ms"] = int(matching_s2["created_unix_ms"]) + 1
        matching_s2["next_attempt_unix_ms"] = int(time.time() * 1000) + 5000
        matching_s2["attempts"] = 3
        matching_s2["last_error"] = "transient retry after S2"
        matching_s2["event"]["last-assistant-message"] = (
            "newer durable S2 with different private content"
        )
        self.assertNotEqual(
            matching_s2["candidate_revision"], matching_s1["candidate_revision"]
        )

        drifted = self.seed_audncode_recovery_journal_fixture("identity-drift")
        drifted_s1 = drifted["successor"]
        self.assertIsInstance(drifted_s1, dict)
        drifted_s2 = json.loads(json.dumps(drifted_s1))
        drifted_s2["candidate_revision"] = uuid.uuid4().hex
        drifted_s2["origin"] = "audncode-identity-drift"
        drifted_s2["event"]["last-assistant-message"] = "identity drift must retain S1"

        dead_dir = self.state / "dead"
        dead_dir.mkdir(parents=True, exist_ok=True)
        matching_dead = dead_dir / f"{matching['key']}.json"
        drifted_dead = dead_dir / f"{drifted['key']}.json"
        matching_dead.write_text(
            json.dumps(matching_s2, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        drifted_dead.write_text(
            json.dumps(drifted_s2, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        matching_dead_bytes = matching_dead.read_bytes()
        drifted_dead_bytes = drifted_dead.read_bytes()

        self.run_ok(self.worker_command("powershell"), timeout=120)

        matching_tombstone_path = matching["tombstone_path"]
        matching_index_path = matching["index_path"]
        self.assertIsInstance(matching_tombstone_path, Path)
        self.assertIsInstance(matching_index_path, Path)
        matching_tombstone = json.loads(
            matching_tombstone_path.read_text(encoding="utf-8-sig")
        )
        self.assertIsNone(matching_tombstone["successor"], matching_tombstone)
        self.assertEqual(matching_tombstone["successor_hash"], "", matching_tombstone)
        self.assertFalse(matching_index_path.exists(), self.state_debug())
        self.assertNotIn(
            str(matching["private_sentinel"]),
            matching_tombstone_path.read_text(encoding="utf-8-sig"),
        )
        self.assertEqual(matching_dead.read_bytes(), matching_dead_bytes)

        drifted_tombstone_path = drifted["tombstone_path"]
        drifted_index_path = drifted["index_path"]
        self.assertIsInstance(drifted_tombstone_path, Path)
        self.assertIsInstance(drifted_index_path, Path)
        drifted_tombstone = json.loads(
            drifted_tombstone_path.read_text(encoding="utf-8-sig")
        )
        self.assertIsInstance(drifted_tombstone["successor"], str, drifted_tombstone)
        self.assertRegex(drifted_tombstone["successor_hash"], r"^[0-9a-f]{64}$")
        self.assertTrue(drifted_index_path.exists(), self.state_debug())
        self.assertIn(
            str(drifted["private_sentinel"]),
            drifted_tombstone_path.read_text(encoding="utf-8-sig"),
        )
        self.assertEqual(drifted_dead.read_bytes(), drifted_dead_bytes)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_recovery_schema2_suppression_cleanup_matrix(self) -> None:
        terminal_reasons = (
            "subagent",
            "technical-turn",
            "superseded",
            "unverifiable",
            "claude-session-unverifiable",
            "stale-session",
            "goal-cancelled",
        )
        successor_cases: dict[str, dict[str, object]] = {}
        failure_cases: dict[str, dict[str, object]] = {}

        for reason in terminal_reasons:
            successor_fixture = self.seed_audncode_recovery_journal_fixture(
                f"suppressed-s2-{reason}"
            )
            successor_s1 = successor_fixture["successor"]
            self.assertIsInstance(successor_s1, dict)
            successor_s2 = json.loads(json.dumps(successor_s1))
            successor_s2["candidate_revision"] = uuid.uuid4().hex
            successor_s2["created_unix_ms"] = int(successor_s2["created_unix_ms"]) + 1
            successor_s2["event"]["last-assistant-message"] = f"suppressed S2 {reason}"
            suppression_path = self.write_schema2_terminal_suppression(
                successor_fixture, successor_s2, reason
            )
            successor_cases[reason] = {
                "fixture": successor_fixture,
                "suppression_path": suppression_path,
            }

            failure_fixture = self.seed_audncode_recovery_journal_fixture(
                f"suppressed-failure-{reason}"
            )
            journaled_successor = failure_fixture["successor"]
            self.assertIsInstance(journaled_successor, dict)
            failure_record = {
                "key": journaled_successor["key"],
                "provider": "claude",
                "weak_identity": False,
                "sequence_id": journaled_successor["sequence_id"],
                "thread_id": journaled_successor["thread_id"],
                "turn_id": journaled_successor["turn_id"],
                "origin": journaled_successor["origin"],
                "candidate_kind": "audncode_stop_failure",
                "source_event": "StopFailure",
                "completion_event_type": "task_failed",
                "candidate_revision": failure_fixture["failure_revision"],
            }
            failure_suppression_path = self.write_schema2_terminal_suppression(
                failure_fixture, failure_record, reason
            )
            failure_cases[reason] = {
                "fixture": failure_fixture,
                "suppression_path": failure_suppression_path,
            }

        self.run_ok(self.worker_command("powershell"), timeout=120)

        for reason, case in successor_cases.items():
            with self.subTest(kind="successor-s2", reason=reason):
                fixture = case["fixture"]
                self.assertIsInstance(fixture, dict)
                tombstone_path = fixture["tombstone_path"]
                index_path = fixture["index_path"]
                suppression_path = case["suppression_path"]
                self.assertIsInstance(tombstone_path, Path)
                self.assertIsInstance(index_path, Path)
                self.assertIsInstance(suppression_path, Path)
                tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
                suppression = json.loads(
                    suppression_path.read_text(encoding="utf-8-sig")
                )
                self.assertEqual(suppression["schema"], 2, suppression)
                self.assertEqual(suppression["reason"], reason, suppression)
                self.assertIsNone(tombstone["successor"], tombstone)
                self.assertEqual(tombstone["successor_hash"], "", tombstone)
                self.assertFalse(index_path.exists(), self.state_debug())
                self.assertNotIn(
                    str(fixture["private_sentinel"]),
                    tombstone_path.read_text(encoding="utf-8-sig"),
                )

        for reason, case in failure_cases.items():
            with self.subTest(kind="failure", reason=reason):
                fixture = case["fixture"]
                self.assertIsInstance(fixture, dict)
                tombstone_path = fixture["tombstone_path"]
                index_path = fixture["index_path"]
                self.assertIsInstance(tombstone_path, Path)
                self.assertIsInstance(index_path, Path)
                tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
                self.assertIsNone(tombstone["successor"], tombstone)
                self.assertEqual(tombstone["successor_hash"], "", tombstone)
                self.assertFalse(index_path.exists(), self.state_debug())
                self.assertNotIn(
                    str(fixture["private_sentinel"]),
                    tombstone_path.read_text(encoding="utf-8-sig"),
                )
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_recovery_terminal_failure_receipt_wins_crash_state(self) -> None:
        pending_dir = self.state / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        cases: dict[str, dict[str, object]] = {}

        for reason in ("technical-turn", "unverifiable"):
            fixture = self.seed_audncode_recovery_journal_fixture(
                f"pending-failure-{reason}"
            )
            successor = fixture["successor"]
            tombstone_path = fixture["tombstone_path"]
            self.assertIsInstance(successor, dict)
            self.assertIsInstance(tombstone_path, Path)
            tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
            failure = json.loads(json.dumps(successor))
            failure.update(
                {
                    "candidate_kind": "audncode_stop_failure",
                    "source_event": "StopFailure",
                    "completion_event_type": "task_failed",
                    "candidate_revision": fixture["failure_revision"],
                    "candidate_identity": tombstone["candidate_identity"],
                    "audncode_stop_failure_uuid": tombstone[
                        "audncode_stop_failure_uuid"
                    ],
                    "audncode_recovery_managed": True,
                    "audncode_recovery_binding": tombstone[
                        "audncode_recovery_binding"
                    ],
                    "audncode_recovery_observed_revision": 2,
                    "audncode_recovery_terminal_hash": tombstone[
                        "audncode_recovery_terminal_hash"
                    ],
                }
            )
            failure["event"]["last-assistant-message"] = (
                f"terminally suppressed provider failure {reason}"
            )
            pending_path = pending_dir / f"{fixture['key']}.json"
            pending_path.write_text(
                json.dumps(failure, separators=(",", ":"), ensure_ascii=False),
                encoding="utf-8",
            )
            suppression_path = self.write_schema2_terminal_suppression(
                fixture, failure, reason
            )
            cases[reason] = {
                "fixture": fixture,
                "pending_path": pending_path,
                "suppression_path": suppression_path,
            }

        self.run_ok(self.worker_command("powershell"), timeout=120)

        for reason, case in cases.items():
            with self.subTest(reason=reason):
                fixture = case["fixture"]
                pending_path = case["pending_path"]
                suppression_path = case["suppression_path"]
                self.assertIsInstance(fixture, dict)
                self.assertIsInstance(pending_path, Path)
                self.assertIsInstance(suppression_path, Path)
                tombstone_path = fixture["tombstone_path"]
                index_path = fixture["index_path"]
                self.assertIsInstance(tombstone_path, Path)
                self.assertIsInstance(index_path, Path)
                tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
                suppression = json.loads(
                    suppression_path.read_text(encoding="utf-8-sig")
                )
                self.assertEqual(suppression["schema"], 2, suppression)
                self.assertEqual(suppression["candidate_kind"], "audncode_stop_failure")
                self.assertEqual(suppression["reason"], reason, suppression)
                self.assertIsNone(tombstone["successor"], tombstone)
                self.assertEqual(tombstone["successor_hash"], "", tombstone)
                self.assertFalse(index_path.exists(), self.state_debug())
                self.assertFalse(pending_path.exists(), self.state_debug())
                self.assertNotIn(
                    str(fixture["private_sentinel"]),
                    tombstone_path.read_text(encoding="utf-8-sig"),
                )
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "sent").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_legacy_empty_revision_suppression_requires_modern_stop_revision(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Revive legacy suppression")
        )
        _state_path, session_state = self.read_audncode_session_state(session_id)
        prompt_id = str(session_state["prompt_id"])
        key = hashlib.sha256(
            f"codex-ntfy/v1|claude|{session_id}|{prompt_id}".encode("utf-8")
        ).hexdigest()
        suppressed_dir = self.state / "suppressed"
        suppressed_dir.mkdir(parents=True, exist_ok=True)
        suppression_path = suppressed_dir / f"{key}.json"

        def write_legacy_suppression() -> None:
            suppression_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "key": key,
                        "thread_id": session_id,
                        "turn_id": prompt_id,
                        "origin": "audncode",
                        "candidate_revision": "",
                        "suppressed_at": datetime.now(timezone.utc).isoformat(),
                        "reason": "technical-turn",
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

        write_legacy_suppression()
        self.run_audncode_hook(
            {**event, "last_assistant_message": "Modern Stop revives legacy receipt."}
        )
        pending_path = self.state / "pending" / f"{key}.json"
        self.assertTrue(pending_path.exists(), self.state_debug())
        modern = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.assertRegex(modern["candidate_revision"], r"^[0-9a-f]{32}$")
        self.assertFalse(suppression_path.exists(), self.state_debug())

        modern["candidate_revision"] = ""
        pending_path.write_text(
            json.dumps(modern, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        write_legacy_suppression()
        self.run_ok(self.worker_command("powershell"), timeout=120)
        self.assertFalse(pending_path.exists(), self.state_debug())
        self.assertTrue(suppression_path.exists(), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "sent").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_direct_delete_keeps_stop_when_active_journal_index_is_malformed(self) -> None:
        fixture = self.seed_audncode_recovery_journal_fixture("malformed-active-index")
        successor = fixture["successor"]
        index_path = fixture["index_path"]
        tombstone_path = fixture["tombstone_path"]
        self.assertIsInstance(successor, dict)
        self.assertIsInstance(index_path, Path)
        self.assertIsInstance(tombstone_path, Path)

        transcript = self.temp / "direct-delete-malformed-index.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.claude_event(
            session_id=str(successor["thread_id"]),
            prompt_id=str(successor["turn_id"]),
            transcript_path=transcript,
        )
        self.run_claude_hook(self.claude_prompt_event(event))
        pending_dir = self.state / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_path = pending_dir / f"{fixture['key']}.json"
        pending_path.write_text(
            json.dumps(successor, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        pending_bytes = pending_path.read_bytes()
        index_path.write_bytes(b"{malformed-active-index")

        self.run_claude_hook(
            {
                **event,
                "background_tasks": [{"task_id": "still-active"}],
                "session_crons": [],
            }
        )

        self.assertTrue(pending_path.exists(), self.state_debug())
        self.assertEqual(pending_path.read_bytes(), pending_bytes)
        self.assertEqual(index_path.read_bytes(), b"{malformed-active-index")
        tombstone = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(tombstone["successor"], str, tombstone)
        self.assertRegex(tombstone["successor_hash"], r"^[0-9a-f]{64}$")
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_recovery_tombstone_corruption_keeps_canonical_failure(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("corrupt identity receipt")
        )
        marker_env = {"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)}
        self.run_audncode_hook(failure, env_overrides=marker_env)
        pending_path = next((self.state / "pending").glob("*.json"))
        self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="recovered",
            reason="runtime-completed",
            failure_record_uuid=failure_uuid,
        )

        # Exit at the deterministic write-ahead crash point, then corrupt that
        # permanent identity receipt. The worker must keep the old failure and
        # the malformed tombstone quarantined indefinitely.
        crashed = self.run_audncode_hook(
            {**event, "last_assistant_message": "Completion after receipt repair."},
            env_overrides={"CODEX_NTFY_TEST_EXIT_AFTER_RECOVERY_JOURNAL": "1"},
            expect_success=False,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr.decode("utf-8", errors="replace"))
        tombstone_path = next((self.state / "suppressed").glob("r-*.json"))
        corrupt = json.loads(tombstone_path.read_text(encoding="utf-8-sig"))
        corrupt["candidate_identity"] = "f" * 64
        tombstone_path.write_text(json.dumps(corrupt), encoding="utf-8")
        stale = time.time() - (90 * 24 * 60 * 60)
        os.utime(tombstone_path, (stale, stale))
        canonical_bytes = pending_path.read_bytes()

        worker = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 30
            log_text = ""
            last_log_error: OSError | None = None
            expected_log_entry = (
                "kept AudnCode provider failure with unverifiable recovery receipt"
            )
            while time.monotonic() < deadline:
                log_path = self.state / "notify.log"
                try:
                    if log_path.exists():
                        log_text = log_path.read_text(
                            encoding="utf-8-sig", errors="replace"
                        )
                        if expected_log_entry in log_text:
                            break
                except OSError as error:
                    # Write-RuntimeLog may briefly own an incompatible Windows
                    # sharing handle while this live worker appends the entry.
                    # Keep the bounded poll; a persistent ACL error still fails
                    # below and is reported explicitly.
                    last_log_error = error
                time.sleep(0.05)
            self.assertIn(
                expected_log_entry,
                log_text,
                f"last_log_error={last_log_error!r}\n{self.state_debug()}",
            )
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        self.assertTrue(pending_path.exists(), self.state_debug())
        self.assertEqual(pending_path.read_bytes(), canonical_bytes)
        self.assertTrue(tombstone_path.exists(), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_duplicate_reingress_keeps_first_terminal_hash(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        _event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("duplicate terminal reingress")
        )
        marker_env = {"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)}
        self.run_audncode_hook(failure, env_overrides=marker_env)

        recovered = self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="recovered",
            reason="runtime-completed",
            failure_record_uuid=failure_uuid,
        )
        # The duplicate is ingested while the original candidate is still
        # pending. It must atomically attach the first terminal hash to that
        # canonical record instead of replacing its candidate revision.
        pending_path = next((self.state / "pending").glob("*.json"))
        before = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.run_audncode_hook(failure, env_overrides=marker_env)
        first_terminal = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(first_terminal["candidate_revision"], before["candidate_revision"])
        self.assertEqual(first_terminal["audncode_recovery_observed_revision"], 2)
        self.assertRegex(first_terminal["audncode_recovery_terminal_hash"], r"^[0-9a-f]{64}$")

        rewritten = {
            **recovered,
            "state": "exhausted",
            "reason": "rollover-budget-exhausted",
            "updated_unix_ms": int(recovered["updated_unix_ms"]) + 1,
        }
        self.write_audncode_recovery_marker(marker_path, rewritten)
        self.run_audncode_hook(failure, env_overrides=marker_env)
        after_rewrite = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(after_rewrite["candidate_revision"], before["candidate_revision"])
        self.assertEqual(
            after_rewrite["audncode_recovery_terminal_hash"],
            first_terminal["audncode_recovery_terminal_hash"],
        )

        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        suppressed = list((self.state / "suppressed").glob("*.json"))
        self.assertEqual(len(suppressed), 1, self.state_debug())
        receipt = json.loads(suppressed[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(receipt["reason"], "unverifiable", receipt)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_exhausted_persists_and_notifies_exactly_once(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        _event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("terminal exhaustion")
        )
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
        )
        self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="exhausted",
            reason="rollover-budget-exhausted",
            failure_record_uuid=failure_uuid,
        )
        time.sleep(1.4)

        barrier = self.temp / "recovery-before-third-gate"
        release = self.temp / "recovery-before-third-gate-release"
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_BEFORE_PROMOTE_MS": "10000",
                "CODEX_NTFY_TEST_BEFORE_PROMOTE_MARKER": str(barrier),
                "CODEX_NTFY_TEST_BEFORE_PROMOTE_RELEASE": str(release),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not barrier.exists():
                time.sleep(0.05)
            self.assertTrue(barrier.exists(), self.state_debug())
            pending = list((self.state / "pending").glob("*.json"))
            self.assertEqual(len(pending), 1, self.state_debug())
            durable = json.loads(pending[0].read_text(encoding="utf-8-sig"))
            self.assertEqual(durable["audncode_recovery_observed_revision"], 2, durable)
            self.assertRegex(durable["audncode_recovery_terminal_hash"], r"^[0-9a-f]{64}$")
            release.write_text("continue", encoding="ascii")
            self.assert_worker_ok(worker, timeout=120)
        finally:
            release.write_text("continue", encoding="ascii")
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=10)

        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Managed provider failure terminal exhaustion", payloads[0]["message"])
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())
        self.assertEqual(len(list((self.state / "sent").glob("*.json"))), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_accepts_terminal_before_and_during_ingress(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        before_event, before_failure, before_uuid, _unused_path, _unused_marker = (
            self.prepare_audncode_managed_failure("terminal before ingress")
        )
        original_host = self.audncode_hosts[before_event["session_id"]]
        retired_manager = self.start_audncode_host(before_event["session_id"])
        self.audncode_hosts[
            f"{before_event['session_id']}:retired-recovery-manager"
        ] = retired_manager
        before_path, before_marker = self.create_audncode_recovery_marker(
            before_event, host=retired_manager
        )
        self.transition_audncode_recovery_marker(
            before_path,
            before_marker,
            state="recovered",
            reason="runtime-completed",
            failure_record_uuid=before_uuid,
        )
        # The asynchronous hook may not read the marker until the recovery
        # manager has exited. Keep the prompt-owning host stable so this test
        # isolates terminal-safe marker attestation from prompt supersession.
        retired_manager[0].terminate()
        retired_manager[0].communicate(timeout=10)
        (
            self.audncode_home / "sessions" / f"{retired_manager[0].pid}.json"
        ).unlink(missing_ok=True)
        self.run_audncode_hook(
            before_failure,
            host=original_host,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(before_path)},
        )
        before_pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(before_pending), 1, self.state_debug())
        before_record = json.loads(before_pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(before_record["audncode_recovery_initial_revision"], 2, before_record)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        _during_event, during_failure, during_uuid, during_path, during_marker = (
            self.prepare_audncode_managed_failure("terminal during ingress")
        )
        observed = self.temp / "recovery-ingress-observed"
        release = self.temp / "recovery-ingress-release"
        errors: list[BaseException] = []

        def run_during_ingress() -> None:
            try:
                self.run_audncode_hook(
                    during_failure,
                    env_overrides={
                        "CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(during_path),
                        "CODEX_NTFY_TEST_RECOVERY_INGRESS_MARKER": str(observed),
                        "CODEX_NTFY_TEST_RECOVERY_INGRESS_RELEASE": str(release),
                    },
                )
            except BaseException as error:
                errors.append(error)

        hook_thread = threading.Thread(target=run_during_ingress)
        hook_thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not observed.exists():
            time.sleep(0.02)
        self.assertTrue(observed.exists(), self.state_debug())
        self.transition_audncode_recovery_marker(
            during_path,
            during_marker,
            state="recovered",
            reason="rollover-committed",
            failure_record_uuid=during_uuid,
        )
        release.write_text("continue", encoding="ascii")
        hook_thread.join(timeout=60)
        self.assertFalse(hook_thread.is_alive(), self.state_debug())
        if errors:
            raise errors[0]
        during_pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(during_pending), 1, self.state_debug())
        during_record = json.loads(during_pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(during_record["audncode_recovery_initial_revision"], 2, during_record)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_marker_schema_and_identity_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("invalid marker matrix")
        )

        terminal = {
            **marker,
            "state": "exhausted",
            "revision": 2,
            "reason": "launcher-unrecoverable",
            "failure_record_uuid": failure_uuid,
        }
        cases: list[tuple[str, dict[str, object]]] = []
        extra = {**marker, "unexpected": True}
        cases.append(("extra-property", extra))
        missing = dict(marker)
        missing.pop("reason")
        cases.append(("missing-property", missing))
        cases.extend(
            [
                ("uppercase-d-uuid", {**marker, "manager_instance_id": str(marker["manager_instance_id"]).upper()}),
                ("n-uuid", {**marker, "session_id": str(marker["session_id"]).replace("-", "")}),
                ("braced-uuid", {**marker, "operation_id": "{" + str(marker["operation_id"]) + "}"}),
                ("parenthesized-uuid", {**marker, "attempt_id": "(" + str(marker["attempt_id"]) + ")"}),
                ("recovering-space-failure", {**marker, "failure_record_uuid": " "}),
                ("recovering-wrong-reason", {**marker, "reason": "runtime-completed"}),
                ("recovering-wrong-state", {**marker, "state": "recovered"}),
                ("revision-three", {**terminal, "revision": 3}),
                ("recovered-exhausted-reason", {**terminal, "state": "recovered"}),
                ("exhausted-recovered-reason", {**terminal, "reason": "runtime-completed"}),
                ("terminal-empty-failure", {**terminal, "failure_record_uuid": ""}),
                ("terminal-uppercase-failure", {**terminal, "failure_record_uuid": failure_uuid.upper()}),
                (
                    "terminal-live-manager-start-drift",
                    {
                        **terminal,
                        "manager_process_start_utc_ticks": int(marker["manager_process_start_utc_ticks"]) + 1,
                    },
                ),
                (
                    "manager-start-drift",
                    {
                        **marker,
                        "manager_process_start_utc_ticks": int(marker["manager_process_start_utc_ticks"]) + 1,
                    },
                ),
            ]
        )
        for label, invalid_marker in cases:
            with self.subTest(case=label):
                self.write_audncode_recovery_marker(marker_path, invalid_marker)
                self.run_audncode_hook(
                    failure,
                    env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
                )
                self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        marker_path.write_bytes(b'{"schema":1,"kind":')
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
        )
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        marker_path.unlink()
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
        )
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        wrong_parent = (
            self.audncode_home
            / "codex-ntfy-recovery"
            / str(uuid.uuid4())
            / marker_path.name
        )
        wrong_parent.parent.mkdir(parents=True)
        wrong_parent.write_text(json.dumps(marker), encoding="utf-8")
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(wrong_parent)},
        )
        wrong_name = marker_path.with_name(f"{uuid.uuid4()}-{marker['attempt_id']}.json")
        wrong_name.write_text(json.dumps(marker), encoding="utf-8")
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(wrong_name)},
        )
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        self.write_audncode_recovery_marker(marker_path, marker)
        icacls = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "icacls.exe"
        acl_result = subprocess.run(
            [str(icacls), str(marker_path), "/grant", "*S-1-1-0:R"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(acl_result.returncode, 0, acl_result.stdout + acl_result.stderr)
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
        )
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        for label, acl_path in (
            ("unprotected-marker-acl", marker_path),
            ("inherited-recovery-root-ace", marker_path.parent.parent),
        ):
            with self.subTest(case=label):
                self.write_audncode_recovery_marker(marker_path, marker)
                inheritance_result = subprocess.run(
                    [str(icacls), str(acl_path), "/inheritance:e"],
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(
                    inheritance_result.returncode,
                    0,
                    inheritance_result.stdout + inheritance_result.stderr,
                )
                self.run_audncode_hook(
                    failure,
                    env_overrides={
                        "CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)
                    },
                )
                self.assertFalse(
                    list((self.state / "pending").glob("*.json")), self.state_debug()
                )
                self.protect_audncode_recovery_directory(marker_path.parent.parent)
                self.protect_audncode_recovery_directory(marker_path.parent)
                self.protect_audncode_recovery_file(marker_path)

        self.write_audncode_recovery_marker(marker_path, marker)
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
        )
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        valid = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(valid["thread_id"], event["session_id"], valid)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_terminal_rewrite_fails_closed_at_commit(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        _event, failure, failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("terminal rewrite")
        )
        self.run_audncode_hook(
            failure,
            env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
        )
        terminal = self.transition_audncode_recovery_marker(
            marker_path,
            marker,
            state="exhausted",
            reason="launcher-unrecoverable",
            failure_record_uuid=failure_uuid,
        )
        time.sleep(1.4)
        barrier = self.temp / "recovery-after-final-gate"
        release = self.temp / "recovery-after-final-gate-release"
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS": "10000",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER": str(barrier),
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE": str(release),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not barrier.exists():
                time.sleep(0.05)
            self.assertTrue(barrier.exists(), self.state_debug())
            rewritten = {
                **terminal,
                "reason": "rollover-unrecoverable",
                "updated_unix_ms": int(terminal["updated_unix_ms"]) + 1,
            }
            self.write_audncode_recovery_marker(marker_path, rewritten)
            release.write_text("continue", encoding="ascii")
            self.assert_worker_ok(worker, timeout=120)
        finally:
            release.write_text("continue", encoding="ascii")
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=10)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertEqual(len(list((self.state / "suppressed").glob("*.json"))), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_post_attestation_invalidity_never_promotes(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        for mode in ("missing", "corrupt", "identity-drift", "revision-rollback", "manager-exited"):
            with self.subTest(mode=mode):
                event, failure, failure_uuid, marker_path, marker = (
                    self.prepare_audncode_managed_failure(f"post attestation {mode}")
                )
                if mode == "revision-rollback":
                    self.transition_audncode_recovery_marker(
                        marker_path,
                        marker,
                        state="exhausted",
                        reason="seed-budget-exhausted",
                        failure_record_uuid=failure_uuid,
                    )
                self.run_audncode_hook(
                    failure,
                    env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
                )
                self.assertEqual(
                    len(list((self.state / "pending").glob("*.json"))),
                    1,
                    self.state_debug(),
                )

                if mode == "missing":
                    marker_path.unlink()
                elif mode == "corrupt":
                    marker_path.write_bytes(b"{not-json")
                elif mode == "identity-drift":
                    drifted = {
                        **marker,
                        "created_unix_ms": int(marker["created_unix_ms"]) + 1,
                        "updated_unix_ms": int(marker["updated_unix_ms"]) + 1,
                    }
                    self.write_audncode_recovery_marker(marker_path, drifted)
                elif mode == "revision-rollback":
                    self.write_audncode_recovery_marker(marker_path, marker)
                else:
                    self.exit_audncode_host(event["session_id"])

                self.run_ok(self.worker_command("powershell"), timeout=120)
                self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
                self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())

        self.assertEqual(
            len(list((self.state / "suppressed").glob("*.json"))), 5, self.state_debug()
        )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_managed_recovery_reparse_component_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        _event, failure, _failure_uuid, marker_path, marker = (
            self.prepare_audncode_managed_failure("reparse component")
        )
        manager_directory = marker_path.parent
        outside = self.temp / "outside-recovery-manager"
        outside.mkdir()
        outside_marker = outside / marker_path.name
        outside_marker.write_text(json.dumps(marker), encoding="utf-8")
        shutil.rmtree(manager_directory)
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(manager_directory), str(outside)],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if created.returncode != 0:
            self.skipTest(f"directory junction unavailable: {created.stdout}{created.stderr}")
        try:
            self.run_audncode_hook(
                failure,
                env_overrides={"CODEX_NTFY_AUDNCODE_RECOVERY_MARKER": str(marker_path)},
            )
            self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            os.rmdir(manager_directory)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_binds_the_current_root_chain_for_system_prompts(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        canonical_task_prompt = """<task-notification>
<task-id>system-anchor</task-id>
<status>completed</status>
<summary>Resume from a completed background task.</summary>
</task-notification>"""
        cases = (
            ("canonical task", canonical_task_prompt, True, None, False),
            ("plain task", "Ultraplan finished; continue the scheduled review.", True, None, False),
            ("meta prompt", "Run the proactive scheduled check.", False, True, False),
            ("delayed rows", "Current query after delayed transcript flush.", False, None, True),
        )
        expected_messages: list[str] = []
        for index, (label, prompt, task_notification, is_meta, delayed_rows) in enumerate(cases):
            session_id = str(uuid.uuid4())
            transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
            transcript.write_text("", encoding="utf-8")
            event = self.audncode_event(session_id=session_id, transcript_path=transcript)
            self.run_audncode_hook(self.audncode_prompt_event(event, prompt=prompt))
            _state_path, state = self.read_audncode_session_state(session_id)
            busy_ms = int(state["busy_unix_ms"])
            prefix_entries: list[dict[str, object]] = []
            if delayed_rows:
                delayed_user_uuid = str(uuid.uuid4())
                prefix_entries.extend(
                    [
                        {
                            "parentUuid": None,
                            "isSidechain": False,
                            "type": "user",
                            "message": {"role": "user", "content": "Prior query flushed late"},
                            "uuid": delayed_user_uuid,
                            "timestamp": datetime.fromtimestamp(
                                (busy_ms - 2) / 1000, timezone.utc
                            ).isoformat(),
                            "sessionId": session_id,
                        },
                        {
                            "parentUuid": delayed_user_uuid,
                            "isSidechain": False,
                            "type": "assistant",
                            "uuid": str(uuid.uuid4()),
                            "timestamp": datetime.fromtimestamp(
                                (busy_ms - 1) / 1000, timezone.utc
                            ).isoformat(),
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Older unrelated API error"}],
                            },
                            "isApiErrorMessage": True,
                            "error": "authentication_failed",
                            "sessionId": session_id,
                        },
                        {
                            "parentUuid": delayed_user_uuid,
                            "isSidechain": True,
                            "type": "assistant",
                            "uuid": str(uuid.uuid4()),
                            "timestamp": datetime.fromtimestamp(
                                (busy_ms + 1) / 1000, timezone.utc
                            ).isoformat(),
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Current chain-safe failure"}],
                            },
                            "isApiErrorMessage": True,
                            "error": "rate_limit",
                            "agentId": "subagent-delayed",
                            "sessionId": session_id,
                        },
                    ]
                )
            expected_message = f"Current chain-safe failure {index}"
            expected_messages.append(expected_message)
            failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
                event,
                error="rate_limit",
                message=expected_message,
                assistant_timestamp_ms=busy_ms + 5,
                user_content=prompt,
                user_is_meta=is_meta,
                user_task_notification=task_notification,
                prefix_entries=prefix_entries,
            )
            self.run_audncode_hook(failure)

        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=180)
        payloads = self.wait_for_payloads(len(cases))
        self.assertEqual(len(payloads), len(cases), self.state_debug())
        combined = "\n".join(payload["message"] for payload in payloads)
        for expected_message in expected_messages:
            self.assertIn(expected_message, combined)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_treats_multimodal_root_users_as_prompts(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        multimodal_content = [
            {"type": "text", "text": "Describe this image"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
            },
        ]

        current_session = str(uuid.uuid4())
        current_transcript = self.audncode_home / "projects" / f"{current_session}.jsonl"
        current_transcript.write_text("", encoding="utf-8")
        current_event = self.audncode_event(
            session_id=current_session,
            transcript_path=current_transcript,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(current_event, prompt="Describe the attached image")
        )
        current_failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            current_event,
            error="rate_limit",
            message="Multimodal prompt API failure",
            user_content=multimodal_content,
        )
        self.run_audncode_hook(current_failure)

        later_session = str(uuid.uuid4())
        later_transcript = self.audncode_home / "projects" / f"{later_session}.jsonl"
        later_transcript.write_text("", encoding="utf-8")
        later_event = self.audncode_event(session_id=later_session, transcript_path=later_transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(later_event, prompt="Fail before a new image prompt")
        )
        later_failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            later_event,
            error="rate_limit",
            message="Superseded before multimodal query",
            user_after_error=True,
            user_after_error_content=multimodal_content,
        )
        self.run_audncode_hook(later_failure)
        self.assertEqual(
            len(list((self.state / "pending").glob("*.json"))),
            1,
            self.state_debug(),
        )

        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Multimodal prompt API failure", payloads[0]["message"])
        self.assertNotIn("Superseded before multimodal", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_transcript_proof_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Prove the failure"))
        _state_path, session_state = self.read_audncode_session_state(session_id)
        busy_ms = int(session_state["busy_unix_ms"])

        def reset_proof(**kwargs: object) -> dict:
            transcript.write_text("", encoding="utf-8")
            failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
                event,
                error="rate_limit",
                message="API Error: Rate limit reached",
                **kwargs,
            )
            return failure

        # Missing durable assistant evidence.
        failure = reset_proof()
        first_line = transcript.read_text(encoding="utf-8").splitlines()[0]
        transcript.write_text(first_line + "\n", encoding="utf-8")
        self.run_audncode_hook(failure)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        # Every payload component is authenticated by the transcript row.
        failure = reset_proof()
        self.run_audncode_hook({**failure, "error": "authentication_failed"})
        failure = reset_proof(error_details="retry after 60 seconds")
        self.run_audncode_hook({**failure, "error_details": "retry after 10 seconds"})
        failure = reset_proof()
        self.run_audncode_hook({**failure, "last_assistant_message": "different text"})
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        # A later root prompt, a same-millisecond error, and subagent payloads
        # are each independently insufficient terminal evidence.
        failure = reset_proof(user_after_error=True)
        self.run_audncode_hook(failure)
        failure = reset_proof(assistant_timestamp_ms=busy_ms)
        self.run_audncode_hook(failure)
        failure = reset_proof()
        self.run_audncode_hook({**failure, "agent_id": "agent-subtask"})
        failure = reset_proof()
        self.append_audncode_task_notification(
            event,
            """<task-notification>
<task-id>newer-query</task-id>
<status>completed</status>
<summary>Even a canonical root task notification starts a newer query.</summary>
</task-notification>""",
        )
        self.run_audncode_hook(failure)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        # Conflicting proofs are sticky for the prompt epoch. Rewinding the
        # transcript to proof A cannot heal ambiguity observed from proof B.
        first_failure = reset_proof()
        first_transcript = transcript.read_text(encoding="utf-8")
        self.run_audncode_hook(first_failure)
        transcript.write_text("", encoding="utf-8")
        second_failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="authentication_failed",
            message="Not logged in after a conflicting failure",
        )
        self.run_audncode_hook(second_failure)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        ambiguous = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(ambiguous["audncode_stop_failure_ambiguous"], ambiguous)
        transcript.write_text(first_transcript, encoding="utf-8")
        self.run_ok(self.worker_command("powershell"), timeout=90)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        # Only a new prompt epoch resets the sticky ambiguity.
        transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="New epoch after ambiguity"))
        recovered_failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="New epoch has one unambiguous failure",
        )
        self.run_audncode_hook(recovered_failure)
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("New epoch has one unambiguous failure", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_ambiguous_rows_are_sticky_until_new_epoch(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Reject duplicate UUID"))
        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="Duplicate UUID API error",
        )
        single_proof = transcript.read_text(encoding="utf-8")
        with transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(single_proof.splitlines()[-1] + "\n")

        self.run_audncode_hook(failure)
        _state_path, ambiguous_state = self.read_audncode_session_state(session_id)
        self.assertTrue(ambiguous_state["audncode_stop_failure_ambiguous"], ambiguous_state)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        # Rewinding to one formerly valid row cannot heal a UUID collision
        # observed in this epoch, even if the same hook is retried.
        transcript.write_text(single_proof, encoding="utf-8")
        self.run_audncode_hook(failure)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        # A fresh epoch clears only the old ambiguity. Two distinct root errors
        # that both match the new payload become independently sticky.
        transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Reject two matching errors"))
        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="Ambiguous matching API error",
        )
        single_proof = transcript.read_text(encoding="utf-8")
        second_matching_error = json.loads(single_proof.splitlines()[-1])
        second_matching_error["uuid"] = str(uuid.uuid4())
        with transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(second_matching_error, separators=(",", ":")) + "\n")
        self.run_audncode_hook(failure)
        _state_path, ambiguous_state = self.read_audncode_session_state(session_id)
        self.assertTrue(ambiguous_state["audncode_stop_failure_ambiguous"], ambiguous_state)
        transcript.write_text(single_proof, encoding="utf-8")
        self.run_audncode_hook(failure)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        transcript.write_text("", encoding="utf-8")
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="New epoch after ambiguity"))
        recovered, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="Single API error in the new epoch",
        )
        self.run_audncode_hook(recovered)
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Single API error in the new epoch", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_precommit_ambiguity_cannot_heal(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Race the final StopFailure proof")
        )
        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="API failure before final proof race",
        )
        self.run_audncode_hook(failure)
        single_proof = transcript.read_text(encoding="utf-8")
        second_matching_error = json.loads(single_proof.splitlines()[-1])
        second_matching_error["uuid"] = str(uuid.uuid4())

        time.sleep(1.4)
        marker = self.temp / "audn-stop-failure-after-gate.marker"
        release = self.temp / "audn-stop-failure-after-gate.release"
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS": "10000",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER": str(marker),
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE": str(release),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.exists(), self.state_debug())
            with transcript.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(second_matching_error, separators=(",", ":")) + "\n")
            release.write_text("continue", encoding="ascii")
            stdout, stderr = worker.communicate(timeout=60)
            self.assertEqual(worker.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
        finally:
            if worker.poll() is None:
                release.write_text("continue", encoding="ascii")
                worker.terminate()
                worker.communicate(timeout=10)

        _state_path, state = self.read_audncode_session_state(session_id)
        self.assertTrue(state["audncode_stop_failure_ambiguous"], state)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        transcript.write_text(single_proof, encoding="utf-8")
        self.run_audncode_hook(failure)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_killed_during_proof_retries_once_without_downgrade(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Retry one durable failure"))
        state_path, initial_state = self.read_audncode_session_state(session_id)

        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="API Error persisted after interrupted hook",
        )
        proof_lines = transcript.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(proof_lines), 2)
        transcript.write_text(proof_lines[0] + "\n", encoding="utf-8")

        child_started = self.temp / "audn-stop-failure-proof-child.pid"
        hook_results: list[subprocess.CompletedProcess[bytes]] = []
        hook_errors: list[BaseException] = []

        def run_until_killed() -> None:
            try:
                hook_results.append(
                    self.run_audncode_hook(
                        failure,
                        child_started_path=child_started,
                        expect_success=False,
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                hook_errors.append(error)

        hook_thread = threading.Thread(target=run_until_killed, daemon=True)
        hook_thread.start()
        log_path = self.state / "notify.log"
        deadline = time.time() + 30
        waiting_seen = False
        while time.time() < deadline:
            if child_started.exists() and log_path.exists():
                waiting_seen = (
                    "waiting briefly for durable AudnCode StopFailure transcript evidence"
                    in log_path.read_text(encoding="utf-8-sig", errors="replace")
                )
                if waiting_seen:
                    break
            time.sleep(0.01)
        self.assertTrue(waiting_seen, self.state_debug())
        child_pid = int(child_started.read_text(encoding="ascii"))
        kill_result = self.taskkill_tree_if_running(child_pid)
        hook_thread.join(timeout=30)
        self.assertFalse(hook_thread.is_alive(), "killed StopFailure hook did not return")
        if hook_errors:
            raise hook_errors[0]
        self.assertEqual(len(hook_results), 1)
        if kill_result.returncode == 0:
            self.assertNotEqual(hook_results[0].returncode, 0)

        after_kill = json.loads(state_path.read_text(encoding="utf-8-sig"))
        for field in ("epoch", "prompt_id", "busy_unix_ms", "busy_hook_start_ticks"):
            self.assertEqual(after_kill[field], initial_state[field], after_kill)
        self.assertEqual(after_kill["state"], "busy", after_kill)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())

        with transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(proof_lines[1] + "\n")
        self.run_audncode_hook(failure)
        self.run_audncode_hook(failure)
        # A later ordinary Stop for the same prompt must not downgrade the
        # stronger proof-backed candidate to one that needs idle_prompt.
        self.run_audncode_hook({**event, "last_assistant_message": "Weaker later Stop"})
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        pending_record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(pending_record["candidate_kind"], "audncode_stop_failure")

        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("persisted after interrupted hook", payloads[0]["message"])
        self.run_audncode_hook(failure)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and POWERSHELL_7 is not None, "PowerShell 7 is unavailable")
    def test_audncode_stop_failure_accepts_pwsh_datetime_and_normal_assistant_rows(self) -> None:
        assert POWERSHELL_7 is not None
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Parse timestamps in pwsh"),
            powershell_path=POWERSHELL_7,
        )
        _state_path, session_state = self.read_audncode_session_state(session_id)
        busy_ms = int(session_state["busy_unix_ms"])
        normal_assistant = {
            "parentUuid": None,
            "isSidechain": False,
            "type": "assistant",
            "uuid": str(uuid.uuid4()),
            "timestamp": datetime.fromtimestamp((busy_ms + 1) / 1000, timezone.utc).isoformat(),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Tool prelude"}]},
            "sessionId": session_id,
        }
        with transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(normal_assistant, separators=(",", ":")) + "\n")
        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="authentication_failed",
            message="Not logged in - run /login",
            assistant_timestamp_ms=busy_ms + 3,
        )
        self.run_audncode_hook(failure, powershell_path=POWERSHELL_7)
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Not logged in", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_rejects_transcript_under_reparse_ancestor(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        outside = self.temp / "outside-audn-projects"
        outside.mkdir()
        transcript = outside / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        junction = self.audncode_home / "projects" / "escaped"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(f"directory junction unavailable: {created.stdout}{created.stderr}")
        try:
            escaped_transcript = junction / transcript.name
            event = self.audncode_event(
                session_id=session_id,
                transcript_path=escaped_transcript,
            )
            self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Reject junction escape"))
            session_files = list((self.state / "claude-sessions").glob("session-*.json"))
            self.assertEqual(session_files, [], self.state_debug())
            self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        finally:
            os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_still_waits_for_all_external_work_gates(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        self.env["CLAUDE_CODE_TASK_LIST_ID"] = session_id
        self.env["CLAUDE_CODE_TEAM_NAME"] = "notifier-team"
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Fail after all work"))
        launch = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": event["cwd"],
            "tool_name": "Agent",
            "tool_input": {"run_in_background": True},
            "tool_response": {"status": "async_launched", "agentId": "agent-failure-gate"},
            "tool_use_id": "tool-use-failure-gate",
        }
        self.run_audncode_hook(launch)
        self.run_audncode_hook(
            self.audncode_cron_tool_event(event, action="create", cron_id="c0ffee42")
        )
        self.append_audncode_queue_operation(event, "enqueue", content="Queued follow-up")
        team_path = self.write_audncode_team(event, teammate_active=True)
        task_path = self.write_audncode_task(
            event,
            status="in_progress",
            task_list_id=session_id,
        )
        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="API Error while external work remains",
        )
        self.run_audncode_hook(failure)
        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            self.assertEqual(
                len(list((self.state / "pending").glob("*.json"))),
                1,
                self.state_debug(),
            )
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.append_audncode_task_notification_attachment(
            event,
            """<task-notification>
<task-id>agent-failure-gate</task-id>
<status>completed</status>
<summary>Background agent finished after the API error.</summary>
</task-notification>""",
        )
        self.append_audncode_queue_operation(event, "dequeue")
        team = json.loads(team_path.read_text(encoding="utf-8"))
        team["members"][1]["isActive"] = False
        team_path.write_text(json.dumps(team), encoding="utf-8")
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["status"] = "completed"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        delete_event = self.audncode_cron_tool_event(
            event, action="delete", cron_id="c0ffee42"
        )
        self.append_audncode_cron_tool_proof(event, delete_event)
        self.run_audncode_hook(delete_event)
        shutil.rmtree(team_path.parent)
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("API Error while external work remains", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_waits_for_every_live_host_with_the_same_session(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        first_host = self.audncode_hosts[session_id]
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="First window opens the shared chat"),
            host=first_host,
        )

        second_host = self.start_audncode_host(session_id, cwd=event["cwd"])
        self.audncode_hosts[f"{session_id}-second-window"] = second_host
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Second window continues the shared chat"),
            host=second_host,
        )
        _state_path, conflicted = self.read_audncode_session_state(session_id)
        self.assertTrue(conflicted["audncode_multi_host_conflict"], conflicted)
        self.assertEqual(len(conflicted["audncode_host_lifetimes"]), 2, conflicted)

        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="Shared-session API failure",
        )
        self.run_audncode_hook(failure, host=second_host)
        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            deadline = time.time() + 30
            gate_reason = ""
            while time.time() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if len(pending) == 1:
                    try:
                        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                        gate_reason = str(record.get("gate_reason", ""))
                    except (OSError, json.JSONDecodeError):
                        gate_reason = ""
                    if gate_reason == "audncode-multi-host-session-active":
                        break
                time.sleep(0.05)
            self.assertEqual(
                gate_reason,
                "audncode-multi-host-session-active",
                self.state_debug(),
            )
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        # An exact correlated hook from the sole survivor may prune only the
        # exited PID/start tuple. A reused PID cannot impersonate that lifetime.
        self.exit_audncode_host(session_id)
        self.run_audncode_hook(failure, host=second_host)
        _state_path, refreshed = self.read_audncode_session_state(session_id)
        self.assertFalse(refreshed["audncode_multi_host_conflict"], refreshed)
        self.assertEqual(len(refreshed["audncode_host_lifetimes"]), 1, refreshed)
        self.assertEqual(refreshed["audncode_host_lifetimes"][0]["pid"], second_host[0].pid)

        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Shared-session API failure", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_obeys_active_achieved_and_cleared_goal_gates(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        def create_goal_failure(label: str) -> tuple[str, dict, dict]:
            session_id = str(uuid.uuid4())
            transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
            transcript.write_text("", encoding="utf-8")
            event = self.audncode_event(session_id=session_id, transcript_path=transcript)
            self.run_audncode_hook(
                self.audncode_prompt_event(event, prompt=f"Goal failure {label}")
            )
            self.append_audncode_goal_status(event, met=False, sentinel=True)
            failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
                event,
                error="rate_limit",
                message=f"API Error during {label} goal",
            )
            self.run_audncode_hook(failure)
            return session_id, event, failure

        achieved_session, achieved_event, _achieved_failure = create_goal_failure("active")
        worker = self.start_worker("powershell")
        try:
            deadline = time.time() + 30
            gate_reason = ""
            last_pending_error: OSError | json.JSONDecodeError | None = None
            while time.time() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if len(pending) == 1:
                    try:
                        record = json.loads(
                            pending[0].read_text(encoding="utf-8-sig")
                        )
                        gate_reason = str(record.get("gate_reason", ""))
                        if gate_reason == "claude-goal-active":
                            break
                    except (OSError, json.JSONDecodeError) as error:
                        # The live worker can be between an atomic replacement
                        # and its ACL/share completion. Keep the bounded poll;
                        # persistent unreadability still fails below.
                        last_pending_error = error
                time.sleep(0.05)
            self.assertEqual(
                gate_reason,
                "claude-goal-active",
                f"last_pending_error={last_pending_error!r}\n{self.state_debug()}",
            )
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.append_audncode_goal_status(
            achieved_event,
            met=True,
            reason="goal completed after retry",
        )
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("API Error during active goal", payloads[0]["message"])
        self.exit_audncode_host(achieved_session)

        cleared_session, cleared_event, _cleared_failure = create_goal_failure("clear")
        self.append_audncode_goal_status(cleared_event, met=True, sentinel=True)
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.exit_audncode_host(cleared_session)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_keeps_unknown_goal_state_fail_closed_until_terminal(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Goal state must remain fail closed")
        )
        failure, _user_uuid, assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="API Error with an unstable goal marker",
        )
        malformed_goal = {
            "parentUuid": assistant_uuid,
            "isSidechain": False,
            "type": "attachment",
            "uuid": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attachment": {"type": "goal_status", "met": "not-a-boolean"},
            "sessionId": session_id,
        }
        with transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(malformed_goal, separators=(",", ":")) + "\n")
        self.run_audncode_hook(failure)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        self.assertEqual(
            json.loads(pending[0].read_text(encoding="utf-8-sig"))["claude_goal_state"],
            "unknown",
        )

        time.sleep(1.4)
        worker = self.start_worker("powershell")
        try:
            deadline = time.time() + 30
            gate_reason = ""
            while time.time() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if len(pending) == 1:
                    try:
                        record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                        gate_reason = str(record.get("gate_reason", ""))
                    except (OSError, json.JSONDecodeError):
                        gate_reason = ""
                    if gate_reason == "claude-goal-state-unverifiable":
                        break
                time.sleep(0.05)
            self.assertEqual(
                gate_reason,
                "claude-goal-state-unverifiable",
                self.state_debug(),
            )
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.append_audncode_goal_status(event, met=True, reason="goal terminal proven")
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_stop_failure_rechecks_goal_after_the_final_gate(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Goal starts in the commit window")
        )
        failure, _user_uuid, _assistant_uuid = self.append_audncode_stop_failure_proof(
            event,
            error="rate_limit",
            message="API Error before a late goal starts",
        )
        self.run_audncode_hook(failure)
        time.sleep(1.4)

        marker = self.temp / "audn-goal-after-final-gate"
        release = self.temp / "audn-goal-after-final-gate-release"
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS": "10000",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER": str(marker),
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE": str(release),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.exists(), self.state_debug())
            self.append_audncode_goal_status(event, met=False, sentinel=True)
            release.write_text("continue", encoding="ascii")

            deadline = time.time() + 30
            commit_deferred = False
            while time.time() < deadline:
                log_path = self.state / "notify.log"
                try:
                    commit_deferred = (
                        "deferred AudnCode candidate because lifecycle or recovery changed"
                        in log_path.read_text(encoding="utf-8-sig")
                    )
                except OSError:
                    commit_deferred = False
                if commit_deferred:
                    break
                time.sleep(0.05)
            self.assertTrue(commit_deferred, self.state_debug())
        finally:
            release.write_text("continue", encoding="ascii")
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())

        self.append_audncode_goal_status(
            event,
            met=True,
            reason="late goal completed after commit retry",
        )
        time.sleep(1.4)
        self.run_ok(self.worker_command("powershell"), timeout=120)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_waits_for_queue_team_goal_and_background_tool(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        def assert_still_busy() -> None:
            worker = self.start_worker("powershell")
            try:
                time.sleep(0.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)

        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Complete every background task"))

        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {
                    "status": "async_launched",
                    "agentId": "agent-background-1",
                },
                "tool_use_id": "tool-use-1",
            }
        )
        self.append_audncode_queue_operation(event, "enqueue")
        team_config = self.write_audncode_team(event, teammate_active=True)
        task_path = self.write_audncode_task(event, status="in_progress")
        self.run_audncode_hook({**event, "last_assistant_message": "Foreground finished, work remains."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        assert_still_busy()

        self.append_audncode_queue_operation(event, "dequeue")
        team = json.loads(team_config.read_text(encoding="utf-8"))
        team["members"][1]["isActive"] = False
        team_config.write_text(json.dumps(team), encoding="utf-8")
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["status"] = "completed"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        assert_still_busy()

        completion_prompt = """<task-notification>
<task-id>agent-background-1</task-id>
<status>completed</status>
<summary>Agent completed</summary>
</task-notification>"""
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt=completion_prompt))
        self.append_audncode_task_notification(event, completion_prompt)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        team["members"] = [team["members"][0]]
        team_config.write_text(json.dumps(team), encoding="utf-8")
        shutil.rmtree(team_config.parent)
        self.run_audncode_hook({**event, "last_assistant_message": "Everything is now complete."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Everything is now complete.", payloads[0]["message"])
        self.assertNotIn("Foreground finished", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_oversized_queue_team_and_task_evidence_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        self.env["CODEX_NTFY_TEST_AUDNCODE_QUEUE_MAX_BYTES"] = str(1024 * 1024)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Validate bounded local evidence"))
        team_path = self.write_audncode_team(event, teammate_active=False)
        task_path = self.write_audncode_task(event, status="completed")
        self.run_audncode_hook({**event, "last_assistant_message": "All bounded evidence is idle."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        time.sleep(1.4)

        team_bytes = team_path.read_bytes()
        team_path.write_bytes(team_bytes + (b" " * (1024 * 1024)))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            team_path.write_bytes(team_bytes)
            task_bytes = task_path.read_bytes()
            task_path.write_bytes(task_bytes + (b" " * (1024 * 1024)))
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            task_path.write_bytes(task_bytes)
            transcript_bytes = transcript.read_bytes()
            with transcript.open("r+b") as stream:
                stream.truncate((1024 * 1024) + 1)
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            transcript.write_bytes(transcript_bytes)
            shutil.rmtree(team_path.parent)
            payloads = self.wait_for_payloads(1, timeout=8)
            self.assertEqual(len(payloads), 1, self.state_debug())
            self.assertIn("All bounded evidence is idle.", payloads[0]["message"])
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_uncorrelated_background_launch_is_sticky_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Launch hidden work"))
        host_process, _host_started_at = self.audncode_hosts[session_id]
        marker_path = self.audncode_home / "sessions" / f"{host_process.pid}.json"
        marker_bytes = marker_path.read_bytes()
        marker_path.unlink()
        try:
            self.run_audncode_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "cwd": event["cwd"],
                    "tool_name": "Agent",
                    "tool_input": {"run_in_background": True},
                    "tool_response": {
                        "status": "async_launched",
                        "agentId": "agent-lost-launch",
                    },
                    "tool_use_id": "tool-use-lost-launch",
                }
            )
        finally:
            marker_path.write_bytes(marker_bytes)

        guards = list(
            (self.state / "claude-sessions").glob("audn-lifecycle-background-*.json")
        )
        self.assertEqual(len(guards), 1, self.state_debug())
        guard = json.loads(guards[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(guard["lost"], guard)
        self.assertEqual(guard["pending_token"], "", guard)

        def assert_no_payload() -> None:
            worker = self.start_worker("powershell")
            try:
                time.sleep(1.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)

        self.run_audncode_hook(
            {**event, "last_assistant_message": "Lost launch must block this result."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        assert_no_payload()

        # A terminal-shaped event cannot prove whether the lost launch ID was
        # the only work created, so it must never clear the host-wide loss.
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Try to stop hidden work"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "TaskStop",
                "tool_input": {"task_id": "agent-lost-launch"},
                "tool_response": {
                    "message": "Stopped task agent-lost-launch",
                    "task_id": "agent-lost-launch",
                    "task_type": "local_agent",
                },
                "tool_use_id": "tool-use-stop-lost-launch",
            }
        )
        self.run_audncode_hook(
            {**event, "last_assistant_message": "TaskStop cannot heal a lost launch."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        assert_no_payload()

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_monitor_post_tool_use_tracks_canonical_task_id(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Monitor the build"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Monitor",
                "tool_input": {"command": "npm test", "description": "Run tests"},
                "tool_response": {
                    "taskId": "monitor-task-1",
                    "outputFile": str(self.temp / "monitor-task-1.output"),
                },
                "tool_use_id": "tool-use-monitor-1",
            }
        )

        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        runtime = json.loads(runtime_paths[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(runtime.get("registry_valid"), runtime)
        self.assertEqual(runtime.get("background_ids"), ["monitor-task-1"], runtime)

        self.run_audncode_hook({**event, "last_assistant_message": "Monitoring is still active."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_background_shape_without_id_is_sticky_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        cases = (
            (
                "monitor",
                "Monitor",
                {"command": "npm test", "description": "Run tests"},
                {"outputFile": str(self.temp / "monitor-missing-id.output")},
            ),
            (
                "agent-is-async",
                "Agent",
                {"description": "Run audit", "prompt": "Audit the project"},
                {"isAsync": True, "description": "Run audit"},
            ),
            (
                "shell-background-flag",
                "Bash",
                {"command": "npm test"},
                {
                    "stdout": "",
                    "stderr": "",
                    "interrupted": False,
                    "assistantAutoBackgrounded": True,
                },
            ),
        )

        for label, tool_name, tool_input, tool_response in cases:
            with self.subTest(shape=label):
                session_id = str(uuid.uuid4())
                transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                event = self.audncode_event(session_id=session_id, transcript_path=transcript)
                self.run_audncode_hook(
                    self.audncode_prompt_event(event, prompt=f"Exercise {label}")
                )
                guards_before = set(
                    (self.state / "claude-sessions").glob("audn-lifecycle-background-*.json")
                )
                self.run_audncode_hook(
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": session_id,
                        "transcript_path": str(transcript),
                        "cwd": event["cwd"],
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "tool_response": tool_response,
                        "tool_use_id": f"tool-use-{label}",
                    }
                )
                guards_after = set(
                    (self.state / "claude-sessions").glob("audn-lifecycle-background-*.json")
                )
                new_guards = guards_after - guards_before
                self.assertEqual(len(new_guards), 1, self.state_debug())
                guard = json.loads(new_guards.pop().read_text(encoding="utf-8-sig"))
                self.assertTrue(guard.get("lost"), guard)

                self.run_audncode_hook(
                    {**event, "last_assistant_message": f"{label} must remain blocked."}
                )
                self.run_audncode_hook(self.audncode_idle_event(event))

        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_background_lifecycle_guard_commits_valid_launch(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Launch tracked work"))
        launch = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": event["cwd"],
            "tool_name": "Agent",
            "tool_input": {"run_in_background": True},
            "tool_response": {"status": "async_launched", "agentId": "agent-guard-ok"},
            "tool_use_id": "tool-use-guard-ok",
        }
        self.run_audncode_hook(launch)
        guards = list(
            (self.state / "claude-sessions").glob("audn-lifecycle-background-*.json")
        )
        self.assertEqual(len(guards), 1, self.state_debug())
        guard = json.loads(guards[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(guard["schema"], 2, guard)
        self.assertFalse(guard["lost"], guard)
        self.assertEqual(guard["pending_tokens"], [], guard)
        self.assertEqual(guard["pending_token"], "", guard)
        self.assertTrue(guard["operation_committed"], guard)
        self.assertTrue(guard["safe_to_finalize"], guard)

        self.run_audncode_hook(
            {
                **launch,
                "tool_name": "TaskStop",
                "tool_input": {"task_id": "agent-guard-ok"},
                "tool_response": {
                    "message": "Stopped task agent-guard-ok",
                    "task_id": "agent-guard-ok",
                    "task_type": "local_agent",
                },
                "tool_use_id": "tool-use-guard-stop",
            }
        )
        self.run_audncode_hook({**event, "last_assistant_message": "Tracked work stopped."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Tracked work stopped", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_concurrent_background_lifecycle_tokens_commit_independently(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Launch concurrent tracked work")
        )

        marker_dir = self.temp / "audn-lifecycle-arm-markers"
        first_release = self.temp / "audn-lifecycle-arm-first.release"
        second_release = self.temp / "audn-lifecycle-arm-second.release"
        self.env.update(
            {
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_MARKER_DIR": str(marker_dir),
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_WAIT_MS": "150000",
            }
        )
        agent_ids = ("agent-concurrent-a", "agent-concurrent-b")
        launches = [
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": agent_id},
                "tool_use_id": f"tool-use-{agent_id}",
            }
            for agent_id in agent_ids
        ]
        launch_errors: list[BaseException] = []

        def launch_concurrently() -> None:
            try:
                self.run_audncode_hooks_concurrently(
                    launches,
                    env_overrides=[
                        {"CODEX_NTFY_TEST_LIFECYCLE_ARM_RELEASE": str(first_release)},
                        {"CODEX_NTFY_TEST_LIFECYCLE_ARM_RELEASE": str(second_release)},
                    ],
                )
            except BaseException as error:
                launch_errors.append(error)

        launch_thread = threading.Thread(target=launch_concurrently, daemon=True)
        launch_thread.start()
        try:
            deadline = time.time() + 60
            markers: list[Path] = []
            while time.time() < deadline:
                markers = list(marker_dir.glob("background-*.marker"))
                if len(markers) == 2:
                    break
                time.sleep(0.05)
            self.assertEqual(len(markers), 2, self.state_debug())

            guards = list(
                (self.state / "claude-sessions").glob(
                    "audn-lifecycle-background-*.json"
                )
            )
            self.assertEqual(len(guards), 1, self.state_debug())
            guard = self.read_json_retry(guards[0])
            self.assertEqual(guard["schema"], 2, guard)
            self.assertFalse(guard["lost"], guard)
            marker_tokens = {path.stem.split("-")[-1] for path in markers}
            self.assertEqual(set(guard["pending_tokens"]), marker_tokens, guard)
            self.assertFalse(guard["operation_committed"], guard)
            self.assertFalse(guard["safe_to_finalize"], guard)

            _session_path, session = self.read_audncode_session_state(session_id)
            self.assertEqual(
                set(session["audncode_background_lifecycle_pending_tokens"]),
                set(guard["pending_tokens"]),
                session,
            )
            self.assertFalse(session["audncode_background_registry_valid"], session)
            self.assertTrue(
                session["audncode_background_lifecycle_unverifiable"], session
            )
            self.assertEqual(
                session["audncode_background_lifecycle_failure_reason"],
                "audncode-background-lifecycle-pending",
                session,
            )

            # Release only the first hook. Its own mutation must commit even
            # though the second token still makes the host-wide guard unsafe
            # for finality.
            first_release.write_text("continue", encoding="ascii")
            deadline = time.time() + 60
            while time.time() < deadline:
                guard = self.read_json_retry(guards[0])
                _session_path, session = self.read_audncode_session_state(session_id)
                if (
                    len(guard.get("pending_tokens", [])) == 1
                    and len(
                        session.get(
                            "audncode_background_lifecycle_pending_tokens", []
                        )
                    )
                    == 1
                    and agent_ids[0] in session.get("audncode_background_ids", [])
                ):
                    break
                time.sleep(0.05)
            self.assertFalse(guard["lost"], guard)
            self.assertEqual(len(guard["pending_tokens"]), 1, guard)
            self.assertIn(guard["pending_tokens"][0], marker_tokens, guard)
            self.assertTrue(guard["operation_committed"], guard)
            self.assertFalse(guard["safe_to_finalize"], guard)
            self.assertEqual(
                len(session["audncode_background_lifecycle_pending_tokens"]),
                1,
                session,
            )
            self.assertFalse(session["audncode_background_registry_valid"], session)
            self.assertTrue(
                session["audncode_background_lifecycle_unverifiable"], session
            )
            self.assertEqual(
                session["audncode_background_lifecycle_failure_reason"],
                "audncode-background-lifecycle-pending",
                session,
            )
            self.assertEqual(session["audncode_background_ids"], [agent_ids[0]], session)
            second_release.write_text("continue", encoding="ascii")
        finally:
            first_release.write_text("continue", encoding="ascii")
            second_release.write_text("continue", encoding="ascii")
            launch_thread.join(timeout=60)

        self.assertFalse(launch_thread.is_alive(), "concurrent AudnCode hooks hung")
        if launch_errors:
            raise launch_errors[0]

        guard = self.read_json_retry(guards[0])
        self.assertFalse(guard["lost"], guard)
        self.assertEqual(guard["pending_tokens"], [], guard)
        self.assertEqual(guard["pending_token"], "", guard)
        self.assertTrue(guard["operation_committed"], guard)
        self.assertTrue(guard["safe_to_finalize"], guard)

        _session_path, session = self.read_audncode_session_state(session_id)
        self.assertEqual(
            session["audncode_background_lifecycle_pending_tokens"], [], session
        )
        self.assertEqual(session["audncode_background_lifecycle_pending_token"], "", session)
        self.assertTrue(session["audncode_background_registry_valid"], session)
        self.assertFalse(
            session["audncode_background_lifecycle_unverifiable"], session
        )
        self.assertEqual(
            session["audncode_background_lifecycle_failure_reason"], "", session
        )
        self.assertCountEqual(session["audncode_background_ids"], agent_ids, session)

        for launch, agent_id in zip(launches, agent_ids):
            self.run_audncode_hook(
                {
                    **launch,
                    "tool_name": "TaskStop",
                    "tool_input": {"task_id": agent_id},
                    "tool_response": {
                        "message": f"Stopped task {agent_id}",
                        "task_id": agent_id,
                        "task_type": "local_agent",
                    },
                    "tool_use_id": f"tool-use-stop-{agent_id}",
                }
            )
        self.run_audncode_hook(
            {**event, "last_assistant_message": "Concurrent tracked work stopped."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Concurrent tracked work stopped", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_concurrent_uncorrelated_failure_cannot_be_healed_by_valid_commit(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        mismatched_transcript = self.audncode_home / "projects" / f"{uuid.uuid4()}.jsonl"
        mismatched_transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Launch work around a failed hook")
        )

        marker_dir = self.temp / "audn-valid-before-failure"
        release = self.temp / "audn-valid-before-failure.release"
        valid_launch = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": event["cwd"],
            "tool_name": "Agent",
            "tool_input": {"run_in_background": True},
            "tool_response": {
                "status": "async_launched",
                "agentId": "agent-valid-before-failure",
            },
            "tool_use_id": "tool-use-valid-before-failure",
        }
        uncorrelated_launch = {
            **valid_launch,
            "transcript_path": str(mismatched_transcript),
            "tool_response": {
                "status": "async_launched",
                "agentId": "agent-uncorrelated-concurrent",
            },
            "tool_use_id": "tool-use-uncorrelated-concurrent",
        }
        launch_errors: list[BaseException] = []

        def launch_concurrently() -> None:
            try:
                self.run_audncode_hooks_concurrently(
                    [valid_launch, uncorrelated_launch],
                    env_overrides=[
                        {
                            "CODEX_NTFY_TEST_LIFECYCLE_ARM_MARKER_DIR": str(
                                marker_dir
                            ),
                            "CODEX_NTFY_TEST_LIFECYCLE_ARM_RELEASE": str(release),
                            "CODEX_NTFY_TEST_LIFECYCLE_ARM_WAIT_MS": "150000",
                        },
                        {},
                    ],
                    wait_for_paths=[None, marker_dir],
                )
            except BaseException as error:
                launch_errors.append(error)

        launch_thread = threading.Thread(target=launch_concurrently, daemon=True)
        launch_thread.start()
        try:
            deadline = time.time() + 30
            session: dict = {}
            while time.time() < deadline:
                _session_path, session = self.read_audncode_session_state(session_id)
                if (
                    session.get("audncode_background_lifecycle_failure_reason")
                    == "audncode-background-hook-could-not-be-correlated-or-committed"
                ):
                    break
                time.sleep(0.05)
            self.assertEqual(
                session.get("audncode_background_lifecycle_failure_reason"),
                "audncode-background-hook-could-not-be-correlated-or-committed",
                session,
            )
            self.assertEqual(
                len(session["audncode_background_lifecycle_pending_tokens"]),
                1,
                session,
            )
        finally:
            release.write_text("continue", encoding="ascii")
            launch_thread.join(timeout=60)

        self.assertFalse(launch_thread.is_alive(), "concurrent AudnCode hooks hung")
        if launch_errors:
            raise launch_errors[0]

        _session_path, session = self.read_audncode_session_state(session_id)
        self.assertEqual(
            session["audncode_background_lifecycle_pending_tokens"], [], session
        )
        self.assertFalse(session["audncode_background_registry_valid"], session)
        self.assertTrue(
            session["audncode_background_lifecycle_unverifiable"], session
        )
        self.assertEqual(
            session["audncode_background_lifecycle_failure_reason"],
            "audncode-background-hook-could-not-be-correlated-or-committed",
            session,
        )
        guards = list(
            (self.state / "claude-sessions").glob(
                "audn-lifecycle-background-*.json"
            )
        )
        self.assertEqual(len(guards), 1, self.state_debug())
        guard = json.loads(guards[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(guard["lost"], guard)
        self.assertEqual(guard["pending_tokens"], [], guard)
        self.assertFalse(guard["safe_to_finalize"], guard)

        self.run_audncode_hook(
            {
                **valid_launch,
                "tool_name": "TaskStop",
                "tool_input": {"task_id": "agent-valid-before-failure"},
                "tool_response": {
                    "message": "Stopped task agent-valid-before-failure",
                    "task_id": "agent-valid-before-failure",
                    "task_type": "local_agent",
                },
                "tool_use_id": "tool-use-stop-valid-before-failure",
            }
        )
        self.run_audncode_hook(
            {**event, "last_assistant_message": "The lost launch must remain blocked."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_runtime_survives_session_switch_and_validates_terminal_prompt(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        first_session = str(uuid.uuid4())
        first_transcript = self.audncode_home / "projects" / f"{first_session}.jsonl"
        first_transcript.write_text("", encoding="utf-8")
        first = self.audncode_event(session_id=first_session, transcript_path=first_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(first, prompt="Start a background agent"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": first_session,
                "transcript_path": str(first_transcript),
                "cwd": first["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": "agent-resume-1"},
                "tool_use_id": "tool-use-resume-1",
            }
        )
        self.append_audncode_queue_operation(first, "enqueue")
        self.run_audncode_hook({**first, "last_assistant_message": "Foreground result before clear."})
        self.run_audncode_hook(self.audncode_idle_event(first))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        second_session = str(uuid.uuid4())
        second_transcript = self.audncode_home / "projects" / f"{second_session}.jsonl"
        second_transcript.write_text("", encoding="utf-8")
        self.audncode_hosts[second_session] = self.audncode_hosts[first_session]
        second = self.audncode_event(session_id=second_session, transcript_path=second_transcript)
        self.append_audncode_queue_operation(second, "dequeue")
        completion_prompt = """<task-notification>
<task-id>agent-resume-1</task-id>
<status>completed</status>
<summary>Agent completed after resume</summary>
</task-notification>"""
        self.run_audncode_hook(self.audncode_prompt_event(second, prompt=completion_prompt))
        self.append_audncode_task_notification(second, completion_prompt)
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(pending, [], self.state_debug())
        self.run_audncode_hook({**second, "last_assistant_message": "Resumed session is truly complete."})
        self.run_audncode_hook(self.audncode_idle_event(second))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Resumed session is truly complete.", payloads[0]["message"])
        self.assertNotIn("Foreground result before clear", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_ctrl_b_sidechain_and_manual_terminal_spoof_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        def wait_for_busy_reason(expected_reason: str, *, hold_atomic_replace: bool = False) -> None:
            held_handle: int | None = None
            close_handle = None
            if hold_atomic_replace:
                import ctypes
                from ctypes import wintypes

                pending_paths = list((self.state / "pending").glob("*.json"))
                self.assertEqual(len(pending_paths), 1, self.state_debug())
                create_file = ctypes.windll.kernel32.CreateFileW
                create_file.argtypes = (
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                )
                create_file.restype = wintypes.HANDLE
                close_handle = ctypes.windll.kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                close_handle.restype = wintypes.BOOL
                held_handle = create_file(
                    str(pending_paths[0]),
                    0x80000000,  # GENERIC_READ
                    0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deliberately no delete sharing
                    None,
                    3,  # OPEN_EXISTING
                    0x00000080,  # FILE_ATTRIBUTE_NORMAL
                    None,
                )
                self.assertNotEqual(held_handle, wintypes.HANDLE(-1).value)
            worker = self.start_worker("powershell")
            observed = False
            try:
                if hold_atomic_replace:
                    pending_name = pending_paths[0].name
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        if list(pending_paths[0].parent.glob(f".{pending_name}.*.tmp")):
                            break
                        self.assertIsNone(worker.poll(), self.state_debug())
                        time.sleep(0.01)
                    else:
                        self.fail(f"atomic replace was not attempted while a reader held the file: {self.state_debug()}")
                    self.assertIsNone(worker.poll(), self.state_debug())
                    self.assertTrue(close_handle(held_handle))
                    held_handle = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    with self.server.lock:
                        self.assertEqual(self.server.payloads, [], self.state_debug())
                    for pending_path in (self.state / "pending").glob("*.json"):
                        try:
                            pending = json.loads(
                                pending_path.read_text(encoding="utf-8-sig")
                            )
                        except (OSError, json.JSONDecodeError):
                            # This poll deliberately overlaps File.Replace while
                            # another handle temporarily denies delete sharing.
                            # A transient open/replace window is not malformed
                            # notifier state; retry the stable snapshot.
                            continue
                        if pending.get("gate_reason") == expected_reason:
                            observed = True
                            break
                    if observed:
                        break
                    time.sleep(0.1)
            finally:
                if held_handle is not None and close_handle is not None:
                    close_handle(held_handle)
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)
            self.assertTrue(observed, self.state_debug())

        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Move this query to the background"))
        sidechain = transcript.parent / session_id / "subagents" / "main" / "agent-sabc12345.jsonl"
        sidechain.parent.mkdir(parents=True)
        sidechain.write_text('{"type":"assistant"}\n', encoding="utf-8")
        self.run_audncode_hook({**event, "last_assistant_message": "Foreground is idle only."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        wait_for_busy_reason("audncode-background-tools-active")

        completion_prompt = """<task-notification>
<task-id>sabc12345</task-id>
<status>completed</status>
<summary>Main session completed</summary>
</task-notification>"""
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt=completion_prompt))
        self.append_audncode_task_notification(event, completion_prompt, origin_kind="user")
        self.run_audncode_hook({**event, "last_assistant_message": "A pasted XML block is not proof."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        wait_for_busy_reason("audncode-awaiting-terminal-proof", hold_atomic_replace=True)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt=completion_prompt))
        self.append_audncode_task_notification(event, completion_prompt)
        self.run_audncode_hook({**event, "last_assistant_message": "The background main session completed."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("background main session completed", payloads[0]["message"])
        self.assertNotIn("pasted XML", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_custom_task_list_popall_and_taskstop_are_observed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        self.env["CLAUDE_CODE_TASK_LIST_ID"] = "custom:list"
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Finish the custom task list"))
        task_path = self.write_audncode_task(
            event,
            status="in_progress",
            task_list_id="custom-list",
        )
        self.append_audncode_queue_operation(event, "enqueue")
        self.append_audncode_queue_operation(event, "popAll")
        self.run_audncode_hook({**event, "last_assistant_message": "Task list still active."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["status"] = "completed"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        task_lock = Path(str(task_path) + ".lock")
        task_lock.mkdir()
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        task_lock.rmdir()
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())

        self.env.pop("CLAUDE_CODE_TASK_LIST_ID", None)
        second_id = str(uuid.uuid4())
        second_transcript = self.audncode_home / "projects" / f"{second_id}.jsonl"
        second_transcript.write_text("", encoding="utf-8")
        second = self.audncode_event(session_id=second_id, transcript_path=second_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(second, prompt="Stop the background agent"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": second_id,
                "transcript_path": str(second_transcript),
                "cwd": second["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": "agent-stop-1"},
                "tool_use_id": "tool-use-stop-1",
            }
        )
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": second_id,
                "transcript_path": str(second_transcript),
                "cwd": second["cwd"],
                "tool_name": "TaskStop",
                "tool_input": {"task_id": "agent-stop-1"},
                "tool_response": {
                    "message": "Stopped task agent-stop-1",
                    "task_id": "agent-stop-1",
                    "task_type": "local_agent",
                },
                "tool_use_id": "tool-use-stop-2",
            }
        )
        self.run_audncode_hook({**second, "last_assistant_message": "Stopped task is terminal."})
        self.run_audncode_hook(self.audncode_idle_event(second))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(2)
        self.assertEqual(len(payloads), 2, self.state_debug())
        self.assertTrue(any("Stopped task is terminal" in payload["message"] for payload in payloads))

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_invisible_kill_all_clear_requires_trusted_consumption(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Start queued work"))

        self.append_audncode_queue_operation(event, "enqueue", content="queued before kill-all")
        kill_summary = 'Background agent "worker <one> & two" was stopped by the user.'
        self.append_audncode_queue_operation(event, "enqueue", content=kill_summary)
        self.append_audncode_queue_operation(event, "dequeue")
        self.run_audncode_hook({**event, "last_assistant_message": "All requested work is stopped."})
        self.run_audncode_hook(self.audncode_idle_event(event))

        # Merely pasting the source-shaped text is not proof that AudnCode's
        # internal kill-all command was consumed, so the invisible clear cannot
        # yet rebase the persisted queue ledger.
        self.append_audncode_task_notification(
            event, kill_summary, origin_kind="user"
        )
        self.append_audncode_task_notification_attachment(
            event, kill_summary, command_mode="prompt"
        )
        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        # A queued_command attachment is AudnCode's persisted acceptance proof.
        # It safely rebases the silently-cleared prefix without a timeout.
        self.append_audncode_task_notification_attachment(event, kill_summary)
        # Let the one-shot worker complete all of its required stable snapshot
        # passes. A fixed payload wait can otherwise kill a correct worker while
        # it is persisting command-queue-active or snapshot-settling state.
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("All requested work is stopped", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_precommit_rechecks_queue_after_second_read(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Finish only when idle"))
        self.run_audncode_hook({**event, "last_assistant_message": "Candidate result."})
        self.run_audncode_hook(self.audncode_idle_event(event))

        marker = self.temp / "audn-before-third-gate.marker"
        release = self.temp / "audn-before-third-gate.release"
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_BEFORE_PROMOTE_MS": "10000",
                "CODEX_NTFY_TEST_BEFORE_PROMOTE_MARKER": str(marker),
                "CODEX_NTFY_TEST_BEFORE_PROMOTE_RELEASE": str(release),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.exists(), self.state_debug())
            self.append_audncode_queue_operation(
                event, "enqueue", content="work arrived in the precommit window"
            )
            release.write_text("continue", encoding="ascii")
            deadline = time.time() + 30
            gate_reason = ""
            while time.time() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if len(pending) == 1:
                    try:
                        pending_record = json.loads(
                            pending[0].read_text(encoding="utf-8-sig")
                        )
                        gate_reason = str(pending_record.get("gate_reason", ""))
                    except (OSError, json.JSONDecodeError):
                        gate_reason = ""
                    if gate_reason == "audncode-command-queue-active":
                        break
                time.sleep(0.05)
            self.assertEqual(
                gate_reason, "audncode-command-queue-active", self.state_debug()
            )
        finally:
            if worker.poll() is None:
                release.write_text("continue", encoding="ascii")
                worker.terminate()
            worker.communicate(timeout=10)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        pending = list((self.state / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1, self.state_debug())
        pending_record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(pending_record.get("gate_reason"), "audncode-command-queue-active")

        self.append_audncode_queue_operation(event, "dequeue")
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cross_session_lifecycle_change_blocks_post_gate_promotion(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        first_id = str(uuid.uuid4())
        first_transcript = self.audncode_home / "projects" / f"{first_id}.jsonl"
        first_transcript.write_text("", encoding="utf-8")
        first = self.audncode_event(session_id=first_id, transcript_path=first_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(first, prompt="Finish when the host is idle"))
        self.run_audncode_hook({**first, "last_assistant_message": "Candidate before clear."})
        self.run_audncode_hook(self.audncode_idle_event(first))

        marker = self.temp / "audn-after-final-gate.marker"
        release = self.temp / "audn-after-final-gate.release"
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS": "10000",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER": str(marker),
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE": str(release),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.exists(), self.state_debug())

            second_id = str(uuid.uuid4())
            second_transcript = self.audncode_home / "projects" / f"{second_id}.jsonl"
            second_transcript.write_text("", encoding="utf-8")
            self.audncode_hosts[second_id] = self.audncode_hosts[first_id]
            second = self.audncode_event(session_id=second_id, transcript_path=second_transcript)
            self.run_audncode_hook(
                self.audncode_prompt_event(second, prompt="Launch work after clear")
            )

            # Exhaust the bounded session-token registry before this hook
            # pre-arms. Start returns a correlation but no guard_info; the
            # failure path must reconstruct the host guard from that trusted
            # identity before the worker can promote the earlier candidate.
            second_state_path, second_state = self.read_audncode_session_state(
                second_id
            )
            overflow_tokens = [uuid.uuid4().hex for _index in range(32)]
            second_state.update(
                {
                    "audncode_background_registry_valid": False,
                    "audncode_background_lifecycle_pending_tokens": overflow_tokens,
                    "audncode_background_lifecycle_pending_token": "",
                    "audncode_background_lifecycle_unverifiable": True,
                    "audncode_background_lifecycle_failure_reason": "audncode-background-lifecycle-pending",
                }
            )
            second_state_path.write_text(
                json.dumps(second_state), encoding="utf-8"
            )
            self.run_audncode_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": second_id,
                    "transcript_path": str(second_transcript),
                    "cwd": second["cwd"],
                    "tool_name": "Agent",
                    "tool_input": {"run_in_background": True},
                    "tool_response": {
                        "status": "async_launched",
                        "agentId": "agent-post-gate",
                    },
                    "tool_use_id": "tool-use-post-gate",
                }
            )

            guards = list(
                (self.state / "claude-sessions").glob("audn-lifecycle-background-*.json")
            )
            self.assertEqual(len(guards), 1, self.state_debug())
            guard = json.loads(guards[0].read_text(encoding="utf-8-sig"))
            self.assertTrue(guard["lost"], guard)
            release.write_text("continue", encoding="ascii")

            deadline = time.time() + 15
            deferred = False
            while time.time() < deadline:
                log_path = self.state / "notify.log"
                try:
                    deferred = (
                        log_path.exists()
                        and "deferred AudnCode candidate because lifecycle or recovery changed"
                        in log_path.read_text(encoding="utf-8-sig", errors="replace")
                    )
                except OSError:
                    deferred = False
                if deferred:
                    break
                time.sleep(0.05)
            self.assertTrue(deferred, self.state_debug())
        finally:
            if worker.poll() is None:
                release.write_text("continue", encoding="ascii")
                worker.terminate()
            worker.communicate(timeout=10)

        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_runtime_state_compacts_terminal_history_and_idle_lineage(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Compact idle lineage"))

        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        runtime_path = runtime_paths[0]
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        retained_sessions = list(runtime.get("sessions", []))
        self.assertEqual(len(retained_sessions), 1, self.state_debug())
        _current_state_path, current_state = self.read_audncode_session_state(session_id)

        old_sessions = []
        old_state_paths = []
        for _ in range(64):
            old_id = str(uuid.uuid4())
            old_transcript = self.audncode_home / "projects" / f"{old_id}.jsonl"
            old_transcript.write_text("", encoding="utf-8")
            old_sessions.append(
                {
                    "session_id": old_id,
                    "transcript_path": str(old_transcript),
                    "task_list_id": "",
                    "task_list_valid": True,
                    "team_name": "",
                    "team_name_valid": True,
                }
            )
            old_state_path, _old_state = self.write_audncode_session_state_clone(
                current_state,
                session_id=old_id,
                transcript_path=old_transcript,
            )
            old_state_paths.append(old_state_path)
        runtime["sessions"] = old_sessions + retained_sessions
        # Old explicit Agent/Bash terminal IDs do not need tombstones: only a
        # fresh trusted launch can reopen them. Update migrates this legacy row.
        runtime["closed_ids"] = ["agent-old-terminal"]
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

        self.run_audncode_hook({**event, "last_assistant_message": "Idle lineage compacted."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        retained_state_mtime = old_state_paths[-1].stat().st_mtime_ns
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())

        compacted = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertLessEqual(len(compacted.get("sessions", [])), 48, compacted)
        self.assertNotIn("agent-old-terminal", compacted.get("closed_ids", []))
        self.assertEqual(old_state_paths[-1].stat().st_mtime_ns, retained_state_mtime)
        self.assertTrue(
            any(
                item.get("session_id") == session_id
                for item in compacted.get("sessions", [])
            ),
            compacted,
        )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_runtime_compaction_preserves_old_active_remote_agent(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Keep remote lineage"))

        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        runtime_path = runtime_paths[0]
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        retained_sessions = list(runtime.get("sessions", []))
        self.assertEqual(len(retained_sessions), 1, self.state_debug())
        _current_state_path, current_state = self.read_audncode_session_state(session_id)

        old_sessions = []
        for _ in range(64):
            old_id = str(uuid.uuid4())
            old_transcript = self.audncode_home / "projects" / f"{old_id}.jsonl"
            old_transcript.write_text("", encoding="utf-8")
            old_sessions.append(
                {
                    "session_id": old_id,
                    "transcript_path": str(old_transcript),
                    "task_list_id": "",
                    "task_list_valid": True,
                    "team_name": "",
                    "team_name_valid": True,
                }
            )
            self.write_audncode_session_state_clone(
                current_state,
                session_id=old_id,
                transcript_path=old_transcript,
            )
        active_old = old_sessions[0]
        self.write_audncode_remote_agent_sidecar(
            {
                "session_id": active_old["session_id"],
                "transcript_path": active_old["transcript_path"],
            },
            task_id="rcompacts",
        )
        runtime["sessions"] = old_sessions + retained_sessions
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

        self.run_audncode_hook({**event, "last_assistant_message": "Must remain pending."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                pending = list((self.state / "pending").glob("*.json"))
                if pending:
                    try:
                        reason = json.loads(
                            pending[0].read_text(encoding="utf-8-sig")
                        ).get("gate_reason", "")
                    except (OSError, json.JSONDecodeError):
                        reason = ""
                    if reason == "audncode-remote-agents-active":
                        break
                if worker.poll() is not None:
                    break
                time.sleep(0.1)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            preserved = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
            self.assertGreater(len(preserved.get("sessions", [])), 64, preserved)
            self.assertTrue(
                any(
                    item.get("session_id") == active_old["session_id"]
                    for item in preserved.get("sessions", [])
                ),
                preserved,
            )
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_compacts_513_sidechain_tombstones_and_preserves_active_agent(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Preserve the active agent"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": "agent-still-active"},
                "tool_use_id": "tool-use-still-active",
            }
        )

        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        runtime_path = runtime_paths[0]
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        sidechain_ids = [f"s{index:08d}" for index in range(513)]
        sidechain_directory = transcript.parent / session_id / "subagents" / "main"
        sidechain_directory.mkdir(parents=True)
        for sidechain_id in sidechain_ids:
            (sidechain_directory / f"agent-{sidechain_id}.jsonl").write_text(
                '{"type":"assistant"}\n', encoding="utf-8"
            )
        # Simulate the legacy v2.6 preview representation at the exact former
        # failure boundary. Only sidechain IDs are tombstones; explicit Agent,
        # Bash, PowerShell, and Monitor IDs remain solely in background_ids.
        runtime["closed_ids"] = sidechain_ids
        runtime.pop("closed_ids_compact", None)
        runtime.pop("closed_ids_compact_sha256", None)
        runtime.pop("closed_ids_fingerprint", None)
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

        self.run_audncode_hook({**event, "last_assistant_message": "Must wait for the active agent."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            deadline = time.monotonic() + 20
            compacted: dict = {}
            while time.monotonic() < deadline:
                try:
                    compacted = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    compacted = {}
                if compacted.get("closed_ids_compact"):
                    break
                time.sleep(0.05)
            self.assertTrue(compacted.get("closed_ids_compact"), self.state_debug())
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.assertTrue(compacted.get("registry_valid"), compacted)
        self.assertEqual(compacted.get("background_ids"), ["agent-still-active"], compacted)
        self.assertLessEqual(len(compacted.get("closed_ids", [])), 512, compacted)
        archive = str(compacted.get("closed_ids_compact", ""))
        packed = base64.b64decode(archive, validate=True)
        self.assertEqual(len(packed) // 6, 513, compacted)
        self.assertEqual(len(packed) % 6, 0, compacted)
        packed_values = [
            int.from_bytes(packed[offset : offset + 6], "little")
            for offset in range(0, len(packed), 6)
        ]
        self.assertEqual(packed_values, [int(value[1:], 36) for value in sidechain_ids])
        self.assertEqual(len(str(compacted.get("closed_ids_compact_sha256", ""))), 64, compacted)
        self.assertNotIn("agent-still-active", compacted.get("closed_ids", []), compacted)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_midturn_attachment_completes_background_task_with_raw_result_chars(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Wait for the background shell"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "permission_mode": "default",
                "tool_name": "Bash",
                "tool_input": {"command": "sleep 1", "run_in_background": True},
                "tool_response": {
                    "stdout": "",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                    "backgroundTaskId": "b12345678",
                },
                "tool_use_id": "tool-use-shell-1",
            }
        )
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "permission_mode": "default",
                "tool_name": "Bash",
                "tool_input": {"command": "sleep 2", "run_in_background": True},
                "tool_response": {
                    "stdout": "",
                    "stderr": "",
                    "interrupted": False,
                    "backgroundTaskId": "b87654321",
                },
                "tool_use_id": "tool-use-shell-2",
            }
        )
        first_completion = """<task-notification>
<task-id>b12345678</task-id>
<tool-use-id>tool-use-shell-1</tool-use-id>
<status>completed</status>
<summary>Raw result A & B < C remains harmless</summary>
</task-notification>"""
        self.append_audncode_task_notification_attachment(event, first_completion)
        self.run_audncode_hook({**event, "last_assistant_message": "Foreground result recorded."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
        second_completion = """<task-notification>
<task-id>b87654321</task-id>
<tool-use-id>tool-use-shell-2</tool-use-id>
<status>completed</status>
<summary>Second task complete</summary>
</task-notification>"""
        self.append_audncode_task_notification_attachment(event, second_completion)
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Foreground result recorded", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_terminal_proof_scans_past_512_lines_and_ignores_nested_spoof(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Wait for both agents"))
        for index, agent_id in enumerate(("agent-long-1", "agent-long-2"), start=1):
            self.run_audncode_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "cwd": event["cwd"],
                    "tool_name": "Agent",
                    "tool_input": {"run_in_background": True},
                    "tool_response": {"status": "async_launched", "agentId": agent_id},
                    "tool_use_id": f"tool-use-long-{index}",
                }
            )

        with transcript.open("a", encoding="utf-8") as stream:
            for index in range(700):
                stream.write(json.dumps({"type": "progress", "index": index}) + "\n")

        first_completion = """<task-notification>
<task-id>agent-long-1</task-id>
<tool-use-id>tool-use-long-1</tool-use-id>
<output-file>C:\\tmp\\agent-long-1.output</output-file>
<status>completed</status>
<summary>First agent complete</summary>
<result>Raw output may contain & and <xml>.
<task-notification>
<task-id>agent-long-2</task-id>
<status>completed</status>
<summary>This nested payload is not a control header</summary>
</task-notification>
</result>
</task-notification>
Remote review suffix with <task-id>agent-long-2</task-id>."""
        self.append_audncode_task_notification_attachment(event, first_completion)
        self.run_audncode_hook({**event, "last_assistant_message": "One real task remains."})
        self.run_audncode_hook(self.audncode_idle_event(event))

        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        second_completion = """<task-notification>
<task-id>agent-long-2</task-id>
<status>completed</status>
<summary>Second agent complete</summary>
</task-notification>
The producer may append a safe explanatory suffix."""
        self.append_audncode_task_notification_attachment(event, second_completion)
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("One real task remains", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_terminal_proof_can_be_the_513th_canonical_notification(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Wait through a long task history"))
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {"status": "async_launched", "agentId": "agent-proof-513"},
                "tool_use_id": "tool-use-proof-513",
            }
        )

        timestamp = datetime.now(timezone.utc).isoformat()
        with transcript.open("a", encoding="utf-8") as stream:
            for index in range(513):
                proof_id = "agent-proof-513" if index == 512 else f"noise-{index:04d}"
                content = (
                    "<task-notification>\n"
                    f"<task-id>{proof_id}</task-id>\n"
                    "<status>completed</status>\n"
                    f"<summary>Canonical completion {index}</summary>\n"
                    "</task-notification>"
                )
                entry = {
                    "parentUuid": None,
                    "isSidechain": False,
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "uuid": str(uuid.uuid4()),
                    "timestamp": timestamp,
                    "origin": {"kind": "task-notification"},
                    "userType": "external",
                    "cwd": event["cwd"],
                    "sessionId": session_id,
                    "version": "0.9.1",
                }
                stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

        self.run_audncode_hook({**event, "last_assistant_message": "The 513th proof is final."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("513th proof is final", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_257_completed_sidechains_do_not_poison_finality(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Finish a large Ctrl+B history"))

        sidechain_directory = transcript.parent / session_id / "subagents" / "main"
        sidechain_directory.mkdir(parents=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        with transcript.open("a", encoding="utf-8") as stream:
            for index in range(257):
                sidechain_id = f"s{index:08d}"
                (sidechain_directory / f"agent-{sidechain_id}.jsonl").write_text(
                    '{"type":"assistant"}\n', encoding="utf-8"
                )
                content = (
                    "<task-notification>\n"
                    f"<task-id>{sidechain_id}</task-id>\n"
                    "<status>completed</status>\n"
                    f"<summary>Sidechain {index} complete</summary>\n"
                    "</task-notification>"
                )
                entry = {
                    "parentUuid": str(uuid.uuid4()),
                    "isSidechain": False,
                    "attachment": {
                        "type": "queued_command",
                        "prompt": content,
                        "commandMode": "task-notification",
                    },
                    "type": "attachment",
                    "uuid": str(uuid.uuid4()),
                    "timestamp": timestamp,
                    "userType": "external",
                    "cwd": event["cwd"],
                    "sessionId": session_id,
                    "version": "0.9.1",
                }
                stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

        self.run_audncode_hook({**event, "last_assistant_message": "All 257 sidechains completed."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        runtime_paths = list((self.state / "claude-sessions").glob("audn-runtime-*.json"))
        self.assertEqual(len(runtime_paths), 1, self.state_debug())
        runtime = json.loads(runtime_paths[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(runtime.get("registry_valid"), runtime)
        self.assertEqual(runtime.get("background_ids"), [], runtime)
        self.assertEqual(len(runtime.get("closed_ids", [])), 257, runtime)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_teammate_spawn_is_owned_by_team_registry(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        self.env.pop("CLAUDE_CODE_TEAM_NAME", None)
        self.env.pop("CLAUDE_CODE_TASK_LIST_ID", None)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Spawn a teammate"))
        team_config = self.write_audncode_team(event, teammate_active=True)
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True, "team_name": "notifier-team"},
                "tool_response": {
                    "status": "teammate_spawned",
                    "teammate_id": "worker-1",
                    "agent_id": "worker-1",
                },
                "tool_use_id": "tool-use-teammate-1",
            }
        )
        self.run_audncode_hook({**event, "last_assistant_message": "The teammate is still working."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            team = json.loads(team_config.read_text(encoding="utf-8"))
            team["members"][1]["isActive"] = False
            team_config.write_text(json.dumps(team), encoding="utf-8")
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            team["members"] = [team["members"][0]]
            team_config.write_text(json.dumps(team), encoding="utf-8")
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            original_config = team_config.read_bytes()
            team_config.write_bytes(b'{"name":')
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            team_config.write_bytes(original_config)
            inbox_directory = team_config.parent / "inboxes"
            inbox_directory.mkdir()
            (inbox_directory / "team-lead.json").write_text(
                json.dumps(
                    [
                        {
                            "from": "worker",
                            "text": "Direct inbox query still belongs to this lineage.",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "read": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            team_config.unlink()
            time.sleep(0.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())

            shutil.rmtree(team_config.parent)
            payloads = self.wait_for_payloads(1, timeout=8)
            self.assertEqual(len(payloads), 1, self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_team_created_after_final_gate_blocks_until_directory_delete(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        self.env.pop("CLAUDE_CODE_TEAM_NAME", None)
        self.env.pop("CLAUDE_CODE_TASK_LIST_ID", None)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Wait for direct team inbox work"))
        self.run_audncode_hook({**event, "last_assistant_message": "Foreground turn completed."})
        self.run_audncode_hook(self.audncode_idle_event(event))

        marker = self.temp / "audncode-team-after-final-gate.marker"
        release = self.temp / "audncode-team-after-final-gate.release"
        self.env.update(
            {
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS": "10000",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER": str(marker),
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE": str(release),
            }
        )
        worker = self.start_worker("powershell")
        team_config: Path | None = None
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not marker.exists():
                time.sleep(0.05)
            self.assertTrue(marker.exists(), self.state_debug())
            team_config = self.write_audncode_team(event, teammate_active=False)
            release.write_text("release", encoding="ascii")
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)

            shutil.rmtree(team_config.parent)
            payloads = self.wait_for_payloads(1, timeout=8)
            self.assertEqual(len(payloads), 1, self.state_debug())
        finally:
            release.write_text("release", encoding="ascii")
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
            for name in (
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE",
            ):
                self.env.pop(name, None)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_team_directory_junction_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        self.env.pop("CLAUDE_CODE_TEAM_NAME", None)
        self.env.pop("CLAUDE_CODE_TASK_LIST_ID", None)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        outside_team = self.temp / "outside-audn-team"
        outside_team.mkdir()
        (outside_team / "config.json").write_text(
            json.dumps(
                {
                    "name": "escaped-team",
                    "leadAgentId": "team-lead",
                    "leadSessionId": session_id,
                    "members": [{"agentId": "team-lead", "isActive": True}],
                }
            ),
            encoding="utf-8",
        )
        teams_root = self.audncode_home / "teams"
        teams_root.mkdir(exist_ok=True)
        junction = teams_root / "escaped-team"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside_team)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(f"directory junction unavailable: {created.stdout}{created.stderr}")
        try:
            self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Reject team junction"))
            self.run_audncode_hook({**event, "last_assistant_message": "Unsafe team evidence."})
            self.run_audncode_hook(self.audncode_idle_event(event))
            time.sleep(1.4)
            worker = self.start_worker("powershell")
            try:
                time.sleep(1.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)
        finally:
            os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_task_list_junction_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        self.env.pop("CLAUDE_CODE_TEAM_NAME", None)
        self.env.pop("CLAUDE_CODE_TASK_LIST_ID", None)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        outside_tasks = self.temp / "outside-audn-tasks"
        outside_tasks.mkdir()
        (outside_tasks / "1.json").write_text(
            json.dumps(
                {
                    "id": "1",
                    "subject": "Escaped task",
                    "description": "Must not be trusted through a junction.",
                    "status": "completed",
                    "blocks": [],
                    "blockedBy": [],
                }
            ),
            encoding="utf-8",
        )
        tasks_root = self.audncode_home / "tasks"
        tasks_root.mkdir(exist_ok=True)
        junction = tasks_root / session_id
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside_tasks)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(f"directory junction unavailable: {created.stdout}{created.stderr}")
        try:
            self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Reject task-list junction"))
            self.run_audncode_hook({**event, "last_assistant_message": "Unsafe task evidence."})
            self.run_audncode_hook(self.audncode_idle_event(event))
            time.sleep(1.4)
            worker = self.start_worker("powershell")
            try:
                time.sleep(1.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)
        finally:
            os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_session_switch_waits_for_fire_and_forget_pid_marker(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        first_id = str(uuid.uuid4())
        first_transcript = self.audncode_home / "projects" / f"{first_id}.jsonl"
        first_transcript.write_text("", encoding="utf-8")
        first = self.audncode_event(session_id=first_id, transcript_path=first_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(first, prompt="Before clear"))

        second_id = str(uuid.uuid4())
        second_transcript = self.audncode_home / "projects" / f"{second_id}.jsonl"
        second_transcript.write_text("", encoding="utf-8")
        self.audncode_hosts[second_id] = self.audncode_hosts[first_id]
        second = self.audncode_event(session_id=second_id, transcript_path=second_transcript)
        host_process, _ = self.audncode_hosts[first_id]
        marker_path = self.audncode_home / "sessions" / f"{host_process.pid}.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["sessionId"] = first_id
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        def finish_fire_and_forget_update() -> None:
            updated = dict(marker)
            updated["sessionId"] = second_id
            marker_path.write_text(json.dumps(updated), encoding="utf-8")

        delayed_update = threading.Timer(1.0, finish_fire_and_forget_update)
        delayed_update.start()
        try:
            self.run_audncode_hook(self.audncode_prompt_event(second, prompt="After clear"))
        finally:
            delayed_update.join(timeout=5)
        self.run_audncode_hook({**second, "last_assistant_message": "The switched session completed."})
        self.run_audncode_hook(self.audncode_idle_event(second))
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("switched session completed", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_all_lineage_sidechains_teams_and_task_lists_block_finality(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        def assert_no_payload() -> None:
            worker = self.start_worker("powershell")
            try:
                time.sleep(1.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)

        self.env["CLAUDE_CODE_TASK_LIST_ID"] = "legacy:list"
        self.env["CLAUDE_CODE_TEAM_NAME"] = "notifier-team"
        first_id = str(uuid.uuid4())
        first_transcript = self.audncode_home / "projects" / f"{first_id}.jsonl"
        first_transcript.write_text("", encoding="utf-8")
        first = self.audncode_event(session_id=first_id, transcript_path=first_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(first, prompt="Work before clear"))
        old_sidechain = first_transcript.parent / first_id / "subagents" / "main" / "agent-slineage1.jsonl"
        old_sidechain.parent.mkdir(parents=True)
        old_sidechain.write_text('{"type":"assistant"}\n', encoding="utf-8")
        team_config = self.write_audncode_team(first, teammate_active=True)
        task_path = self.write_audncode_task(first, status="in_progress", task_list_id="legacy-list")

        second_id = str(uuid.uuid4())
        second_transcript = self.audncode_home / "projects" / f"{second_id}.jsonl"
        second_transcript.write_text("", encoding="utf-8")
        self.audncode_hosts[second_id] = self.audncode_hosts[first_id]
        second = self.audncode_event(session_id=second_id, transcript_path=second_transcript)
        self.env["CLAUDE_CODE_TASK_LIST_ID"] = "new:list"
        self.env.pop("CLAUDE_CODE_TEAM_NAME", None)
        self.run_audncode_hook(self.audncode_prompt_event(second, prompt="Work after clear"))
        self.run_audncode_hook({**second, "last_assistant_message": "The new foreground turn is idle."})
        self.run_audncode_hook(self.audncode_idle_event(second))
        assert_no_payload()

        sidechain_completion = """<task-notification>
<task-id>slineage1</task-id>
<status>completed</status>
<summary>Old sidechain complete</summary>
</task-notification>"""
        self.append_audncode_task_notification_attachment(second, sidechain_completion)
        assert_no_payload()

        team = json.loads(team_config.read_text(encoding="utf-8"))
        team["members"][1]["isActive"] = False
        team_config.write_text(json.dumps(team), encoding="utf-8")
        assert_no_payload()

        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["status"] = "completed"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        shutil.rmtree(team_config.parent)
        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("new foreground turn is idle", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_unordered_recursive_and_mismatched_events_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

        self.run_audncode_hook(self.audncode_prompt_event(event))
        recursive = {**event, "stop_hook_active": True}
        self.run_audncode_hook(recursive)
        mismatched = {**event, "transcript_path": str(self.temp / "other.jsonl")}
        self.run_audncode_hook(mismatched)
        subagent = {**event, "hook_event_name": "SubagentStart", "agent_id": "agent-1"}
        self.run_audncode_hook(subagent)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.run_ok(self.worker_command("powershell"))
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_concurrent_sessions_do_not_collide_and_failure_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        events: list[dict] = []
        for index in range(2):
            session_id = str(uuid.uuid4())
            transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
            transcript.write_text("", encoding="utf-8")
            event = self.audncode_event(
                session_id=session_id,
                transcript_path=transcript,
                message=f"AudnCode session {index} complete.",
            )
            events.append(event)
            self.run_audncode_hook(self.audncode_prompt_event(event))
            self.run_audncode_hook(event)

        time.sleep(1.0)
        for event in events:
            self.run_audncode_hook(self.audncode_idle_event(event))

        failure_session = str(uuid.uuid4())
        failure_transcript = self.audncode_home / "projects" / f"{failure_session}.jsonl"
        failure_transcript.write_text("", encoding="utf-8")
        failure = self.audncode_event(session_id=failure_session, transcript_path=failure_transcript)
        self.run_audncode_hook(self.audncode_prompt_event(failure))
        failure.update(
            hook_event_name="StopFailure",
            error="rate_limit",
            last_assistant_message="API Error: Rate limit reached",
        )
        self.run_audncode_hook(failure)

        self.run_ok(self.worker_command("powershell"))
        payloads = self.wait_for_payloads(2)
        self.assertEqual(len(payloads), 2, self.state_debug())
        self.assertEqual(len({payload["sequence_id"] for payload in payloads}), 2)
        self.assertTrue(any("session 0 complete" in payload["message"] for payload in payloads))
        self.assertTrue(any("session 1 complete" in payload["message"] for payload in payloads))
        self.assertFalse(any("Rate limit reached" in payload["message"] for payload in payloads))

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_irrelevant_session_markers_do_not_hide_the_live_ancestor(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)

        for index in range(300):
            (self.audncode_home / "sessions" / f"noise-{index:04d}.json").write_text(
                json.dumps(
                    {
                        "pid": 900000 + index,
                        "sessionId": str(uuid.uuid4()),
                        "startedAt": 1,
                    }
                ),
                encoding="utf-8",
            )

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Ignore stale markers"))
        self.run_audncode_hook({**event, "last_assistant_message": "The relevant host stayed visible."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("relevant host stayed visible", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_alternating_idle_hosts_do_not_create_multi_host_conflict(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        first = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            message="Superseded result from the first idle window.",
        )
        first_host = self.audncode_hosts[session_id]
        second_host = self.start_audncode_host(session_id)
        self.audncode_hosts[f"{session_id}:second-idle-host"] = second_host
        second = {
            **first,
            "last_assistant_message": "Only the alternating second window completed.",
        }

        self.run_audncode_hook(
            self.audncode_prompt_event(first, prompt="First window query"), host=first_host
        )
        self.run_audncode_hook(first, host=first_host)
        self.run_audncode_hook(self.audncode_idle_event(first), host=first_host)
        _state_path, first_idle = self.read_audncode_session_state(session_id)
        self.assertEqual(first_idle["state"], "idle", first_idle)
        self.assertFalse(first_idle["audncode_multi_host_conflict"], first_idle)

        # The first live process is already proven idle. Starting the next query
        # from another live window must replace, not conflict with, that owner.
        self.run_audncode_hook(
            self.audncode_prompt_event(second, prompt="Second window query"), host=second_host
        )
        _state_path, second_busy = self.read_audncode_session_state(session_id)
        self.assertEqual(second_busy["state"], "busy", second_busy)
        self.assertFalse(second_busy["audncode_multi_host_conflict"], second_busy)
        self.assertEqual(
            [item["pid"] for item in second_busy["audncode_host_lifetimes"]],
            [second_host[0].pid],
            second_busy,
        )

        self.run_audncode_hook(second, host=second_host)
        self.run_audncode_hook(self.audncode_idle_event(second), host=second_host)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("alternating second window completed", payloads[0]["message"])
        self.assertNotIn("first idle window", payloads[0]["message"])

        # Alternation is symmetric: a later synchronous prompt from the first
        # still-live process must pre-arm it again as the sole owner.
        self.run_audncode_hook(
            self.audncode_prompt_event(first, prompt="First window returns"), host=first_host
        )
        _state_path, first_returns = self.read_audncode_session_state(session_id)
        self.assertEqual(first_returns["state"], "busy", first_returns)
        self.assertFalse(first_returns["audncode_multi_host_conflict"], first_returns)
        self.assertEqual(
            [item["pid"] for item in first_returns["audncode_host_lifetimes"]],
            [first_host[0].pid],
            first_returns,
        )
        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_idle_handoff_requires_old_host_runtime_and_guards_clear(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        for mode in ("background", "cron", "lost-guard"):
            with self.subTest(mode=mode):
                session_id = str(uuid.uuid4())
                transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                event = self.audncode_event(
                    session_id=session_id,
                    transcript_path=transcript,
                    project_root=self.temp / f"audncode-retirement-{mode}",
                    message=f"Blocked old {mode} runtime.",
                )
                first_host = self.audncode_hosts[session_id]
                second_host = self.start_audncode_host(session_id, cwd=event["cwd"])
                self.audncode_hosts[f"{session_id}:retirement-{mode}"] = second_host

                self.run_audncode_hook(
                    self.audncode_prompt_event(event, prompt=f"Arm {mode}"), host=first_host
                )
                if mode == "background":
                    self.run_audncode_hook(
                        {
                            "hook_event_name": "PostToolUse",
                            "session_id": session_id,
                            "transcript_path": str(transcript),
                            "cwd": event["cwd"],
                            "tool_name": "Agent",
                            "tool_input": {"run_in_background": True},
                            "tool_response": {
                                "status": "async_launched",
                                "agentId": "agent-retirement-fast-path",
                            },
                            "tool_use_id": f"tool-{session_id}",
                        },
                        host=first_host,
                    )
                elif mode == "cron":
                    self.run_audncode_hook(
                        self.audncode_cron_tool_event(
                            event,
                            action="create",
                            cron_id="c0decafe",
                            recurring=False,
                            durable=False,
                        ),
                        host=first_host,
                    )
                else:
                    _state_path, armed = self.read_audncode_session_state(session_id)
                    runtime_key = armed["audncode_runtime_key"]
                    guard_path = (
                        self.state
                        / "claude-sessions"
                        / f"audn-lifecycle-background-{runtime_key}.json"
                    )
                    guard_path.write_text(
                        json.dumps(
                            {
                                "schema": 2,
                                "kind": "audncode-lifecycle-guard",
                                "registry_kind": "background",
                                "runtime_key": runtime_key,
                                "audncode_home": str(self.audncode_home.resolve()),
                                "host_pid": first_host[0].pid,
                                "host_started_unix_ms": first_host[1],
                                "pending_tokens": [],
                                "lost": True,
                                "updated_unix_ms": int(time.time() * 1000),
                            },
                            separators=(",", ":"),
                        ),
                        encoding="utf-8",
                    )

                self.run_audncode_hook(event, host=first_host)
                self.run_audncode_hook(self.audncode_idle_event(event), host=first_host)
                self.run_audncode_hook(
                    self.audncode_prompt_event(event, prompt="Move to the other host"),
                    host=second_host,
                )
                _state_path, conflicted = self.read_audncode_session_state(session_id)
                self.assertTrue(conflicted["audncode_multi_host_conflict"], conflicted)
                self.assertEqual(
                    {item["pid"] for item in conflicted["audncode_host_lifetimes"]},
                    {first_host[0].pid, second_host[0].pid},
                    conflicted,
                )

        self.run_ok(self.worker_command("powershell"), timeout=90)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_superseded_terminal_pair_cannot_retire_active_old_runtime(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        first_host = self.audncode_hosts[session_id]
        second_host = self.start_audncode_host(session_id, cwd=event["cwd"])
        self.audncode_hosts[f"{session_id}:terminal-runtime"] = second_host

        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Launch an old-host agent"), host=first_host
        )
        self.run_audncode_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Agent",
                "tool_input": {"run_in_background": True},
                "tool_response": {
                    "status": "async_launched",
                    "agentId": "agent-terminal-runtime",
                },
                "tool_use_id": "tool-terminal-runtime",
            },
            host=first_host,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="New owner"), host=second_host
        )
        self.run_audncode_hook(event, host=first_host)
        self.run_audncode_hook(self.audncode_idle_event(event), host=first_host)
        _state_path, still_conflicted = self.read_audncode_session_state(session_id)
        self.assertTrue(still_conflicted["audncode_multi_host_conflict"], still_conflicted)
        self.assertEqual(len(still_conflicted["audncode_host_lifetimes"]), 2, still_conflicted)
        self.assertEqual(len(still_conflicted["audncode_superseded_host_stop_proofs"]), 1)

        self.run_audncode_hook(event, host=second_host)
        self.run_audncode_hook(self.audncode_idle_event(event), host=second_host)
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_late_old_host_lifecycle_re_registers_after_clean_retirement(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        first_host = self.audncode_hosts[session_id]
        second_host = self.start_audncode_host(session_id, cwd=event["cwd"])
        self.audncode_hosts[f"{session_id}:late-lifecycle"] = second_host
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Clean first owner"), host=first_host
        )
        self.run_audncode_hook(event, host=first_host)
        self.run_audncode_hook(self.audncode_idle_event(event), host=first_host)

        retirement_marker = self.temp / "retirement-checked.marker"
        retirement_release = self.temp / "retirement-release.marker"
        late_child_started = self.temp / "late-lifecycle-child.marker"
        failures: list[BaseException] = []

        def run_second_prompt() -> None:
            try:
                self.run_audncode_hook(
                    self.audncode_prompt_event(event, prompt="Retire the first owner"),
                    host=second_host,
                    env_overrides={
                        "CODEX_NTFY_TEST_AUDNCODE_RETIREMENT_MARKER": str(retirement_marker),
                        "CODEX_NTFY_TEST_AUDNCODE_RETIREMENT_RELEASE": str(retirement_release),
                    },
                )
            except BaseException as error:  # surfaced in the main test thread below
                failures.append(error)

        late_event = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": event["cwd"],
            "tool_name": "Agent",
            "tool_input": {"run_in_background": True},
            "tool_response": {
                "status": "async_launched",
                "agentId": "agent-arrived-after-retirement-check",
            },
            "tool_use_id": "tool-arrived-after-retirement-check",
        }

        def run_late_lifecycle() -> None:
            try:
                self.run_audncode_hook(
                    late_event,
                    host=first_host,
                    child_started_path=late_child_started,
                )
            except BaseException as error:  # surfaced in the main test thread below
                failures.append(error)

        second_thread = threading.Thread(target=run_second_prompt, daemon=True)
        second_thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not retirement_marker.exists():
            time.sleep(0.02)
        self.assertTrue(retirement_marker.exists(), self.state_debug())
        late_thread = threading.Thread(target=run_late_lifecycle, daemon=True)
        late_thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not late_child_started.exists():
            time.sleep(0.02)
        self.assertTrue(late_child_started.exists(), self.state_debug())
        time.sleep(0.2)
        retirement_release.write_text("release", encoding="ascii")
        second_thread.join(timeout=30)
        late_thread.join(timeout=30)
        self.assertFalse(second_thread.is_alive(), self.state_debug())
        self.assertFalse(late_thread.is_alive(), self.state_debug())
        if failures:
            raise failures[0]

        _state_path, conflicted = self.read_audncode_session_state(session_id)
        self.assertTrue(conflicted["audncode_multi_host_conflict"], conflicted)
        self.assertEqual(
            {item["pid"] for item in conflicted["audncode_host_lifetimes"]},
            {first_host[0].pid, second_host[0].pid},
            conflicted,
        )
        self.run_audncode_hook(event, host=second_host)
        self.run_audncode_hook(self.audncode_idle_event(event), host=second_host)
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_retired_host_resume_registration_failure_marks_current_background_guard_lost(
        self,
    ) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            message="Must remain blocked after the lost old-host resume.",
        )
        first_host = self.audncode_hosts[session_id]
        second_host = self.start_audncode_host(session_id, cwd=event["cwd"])
        self.audncode_hosts[f"{session_id}:resume-registration-failure"] = second_host

        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Finish on the first owner"),
            host=first_host,
        )
        self.run_audncode_hook(event, host=first_host)
        self.run_audncode_hook(self.audncode_idle_event(event), host=first_host)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Retire the first owner"),
            host=second_host,
        )
        _state_path, second_busy = self.read_audncode_session_state(session_id)
        self.assertEqual(
            [item["pid"] for item in second_busy["audncode_host_lifetimes"]],
            [second_host[0].pid],
            second_busy,
        )
        self.assertFalse(second_busy["audncode_multi_host_conflict"], second_busy)

        # Keep the current owner idle so Start-AudnCodeBackgroundLifecycleMutation
        # cannot pre-arm. The loss path must still resolve and poison the current
        # runtime through Set-AudnCodeBackgroundLifecycleUnverifiable's fallback.
        self.run_audncode_hook(event, host=second_host)
        self.run_audncode_hook(self.audncode_idle_event(event), host=second_host)
        _state_path, second_idle = self.read_audncode_session_state(session_id)
        self.assertEqual(second_idle["state"], "idle", second_idle)
        current_runtime_key = second_idle["audncode_runtime_key"]

        agent_id = "aresume01"
        agent_transcript = (
            transcript.parent
            / session_id
            / "subagents"
            / f"agent-{agent_id}.jsonl"
        )
        agent_transcript.parent.mkdir(parents=True, exist_ok=True)
        agent_transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")

        # Ingress discovery intentionally binds only the ancestor PID/start tuple
        # because stdin has not been parsed yet. Repointing the still-live old
        # marker to another logical session therefore lets ingress arm, while the
        # later exact RegisterObserved check rejects this delayed old-session hook.
        first_marker = self.audncode_home / "sessions" / f"{first_host[0].pid}.json"
        original_marker = first_marker.read_text(encoding="utf-8-sig")
        changed_marker = json.loads(original_marker)
        changed_marker["sessionId"] = str(uuid.uuid4())
        first_marker.write_text(json.dumps(changed_marker), encoding="utf-8")
        try:
            self.run_audncode_hook(
                self.audncode_subagent_start_event(event, agent_id=agent_id),
                host=first_host,
            )
        finally:
            first_marker.write_text(original_marker, encoding="utf-8")

        _state_path, lost = self.read_audncode_session_state(session_id)
        self.assertFalse(lost["audncode_background_registry_valid"], lost)
        self.assertTrue(lost["audncode_background_lifecycle_unverifiable"], lost)
        self.assertEqual(
            lost["audncode_background_lifecycle_failure_reason"],
            "audncode-subagent-start-could-not-be-correlated-or-committed",
            lost,
        )
        self.assertEqual(lost["audncode_background_lifecycle_pending_tokens"], [], lost)
        current_guard_path = (
            self.state
            / "claude-sessions"
            / f"audn-lifecycle-background-{current_runtime_key}.json"
        )
        current_guard = self.read_json_retry(current_guard_path)
        self.assertEqual(current_guard["runtime_key"], current_runtime_key, current_guard)
        self.assertEqual(current_guard["host_pid"], second_host[0].pid, current_guard)
        self.assertEqual(
            current_guard["host_started_unix_ms"], second_host[1], current_guard
        )
        self.assertTrue(current_guard["lost"], current_guard)
        self.assertEqual(current_guard["pending_tokens"], [], current_guard)
        self.assertFalse(current_guard["safe_to_finalize"], current_guard)

        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_fresh_subagent_registration_failure_marks_loss_before_ingress_completion(
        self,
    ) -> None:
        calls_path = self.temp / "fresh-subagent-registration-failure-calls.json"
        script = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_NOTIFIER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
$branches = @($ast.FindAll({
    param($node)
    if ($node -isnot [System.Management.Automation.Language.IfStatementAst] -or
        $node.Clauses.Count -eq 0) { return $false }
    $condition = [string]$node.Clauses[0].Item1.Extent.Text
    return $condition -match '\$isAudnCode' -and
      $condition -match '\$hookName' -and
      $condition -match "'SubagentStart'"
  }, $true))
if ($branches.Count -ne 1) { throw "Expected one SubagentStart branch, got $($branches.Count)" }

$script:Calls = New-Object 'System.Collections.Generic.List[string]'
function Get-ObjectValue {
  param([object]$Object, [string]$Name, [object]$Default = $null)
  if ($null -eq $Object) { return $Default }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property -or $null -eq $property.Value) { return $Default }
  return $property.Value
}
function Register-AudnCodeObservedHostLifetime {
  param(
    [string]$SessionId,
    [string]$TranscriptPath,
    [string]$HomePath,
    [int]$HostPid,
    [int64]$HostStartedUnixMs
  )
  return $false
}
function Get-AudnCodeSubagentStartResumeProof {
  param(
    [string]$SessionId,
    [string]$TranscriptPath,
    [string]$AgentId,
    [string]$AgentType,
    [string]$HomePath
  )
  return [pscustomobject]@{
    ok = $true
    resume = $false
    agent_id = $AgentId
    receipt = ''
  }
}
function Start-AudnCodeBackgroundLifecycleMutation {
  throw 'a proven fresh SubagentStart must not arm a background mutation'
}
function Add-AudnCodeBackgroundIds {
  throw 'an unregistered host must not commit background work'
}
function Set-AudnCodeBackgroundLifecycleUnverifiable {
  param(
    [string]$SessionId,
    [int64]$HookStartTicks,
    [AllowNull()][object]$LifecycleArm,
    [string]$Reason
  )
  if ($null -ne $LifecycleArm) { throw 'fresh registration failure should use session fallback' }
  if ($Reason -ne 'audncode-subagent-start-could-not-be-correlated-or-committed') {
    throw "unexpected lifecycle-loss reason: $Reason"
  }
  [void]$script:Calls.Add('background-loss')
  return $true
}
function Complete-AudnCodeIngressMutation {
  param([object]$IngressArm, [switch]$Fail)
  if (-not $Fail) { throw 'failed host registration must fail ingress' }
  [void]$script:Calls.Add('ingress-fail')
  [IO.File]::WriteAllText(
    $env:CODEX_NTFY_TEST_CALLS,
    (ConvertTo-Json -Compress -InputObject @($script:Calls.ToArray())),
    (New-Object Text.UTF8Encoding($false))
  )
  return $true
}
function Write-RuntimeLog { param([string]$Message) }

$isAudnCode = $true
$hookName = 'SubagentStart'
$sessionId = [Guid]::NewGuid().ToString()
$transcriptPath = [IO.Path]::GetFullPath((Join-Path $env:TEMP 'fresh-subagent.jsonl'))
$AudnCodeHome = [IO.Path]::GetFullPath((Join-Path $env:TEMP 'audncode-home'))
$audnCodeIngressArm = [pscustomobject]@{
  host_pid = 4242
  host_started_unix_ms = [int64]1700000000123
}
$agentId = 'afresh001'
$hookInput = [pscustomobject]@{ agent_type = 'general-purpose' }
$HookProcessStartUtcTicks = [DateTime]::UtcNow.Ticks
Invoke-Expression $branches[0].Extent.Text
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **self.env,
                "CODEX_NTFY_TEST_NOTIFIER": str(POWERSHELL_NOTIFIER),
                "CODEX_NTFY_TEST_CALLS": str(calls_path),
            },
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertEqual(result.stdout.strip(), "{}")
        self.assertEqual(
            json.loads(calls_path.read_text(encoding="utf-8-sig")),
            ["background-loss", "ingress-fail"],
        )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_same_session_uuid_from_two_live_hosts_fails_closed_cross_host(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        first = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            message="Wrong host result must not be attributed.",
        )
        first_host = self.audncode_hosts[session_id]
        second_host = self.start_audncode_host(session_id)
        self.audncode_hosts[f"{session_id}:second-host"] = second_host
        second = {
            **first,
            "last_assistant_message": "Only the owning host completed.",
        }

        # The second prompt supersedes the shared logical UUID. Hooks still
        # arriving from the first live process must not be attributed to it.
        self.run_audncode_hook(
            self.audncode_prompt_event(first, prompt="First window"), host=first_host
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(second, prompt="Second window"), host=second_host
        )
        _state_path, conflicted = self.read_audncode_session_state(session_id)
        self.assertTrue(conflicted["audncode_multi_host_conflict"], conflicted)
        self.assertEqual(len(conflicted["audncode_host_lifetimes"]), 2, conflicted)
        self.assertEqual(conflicted["audncode_superseded_host_stop_proofs"], [], conflicted)

        # idle_prompt alone cannot retire a live prior owner. The receipt must
        # be an ordered Stop + idle_prompt pair from the exact old lifetime.
        self.run_audncode_hook(self.audncode_idle_event(first), host=first_host)
        _state_path, idle_before_stop = self.read_audncode_session_state(session_id)
        self.assertTrue(idle_before_stop["audncode_multi_host_conflict"], idle_before_stop)
        self.assertEqual(len(idle_before_stop["audncode_host_lifetimes"]), 2, idle_before_stop)
        self.assertEqual(
            idle_before_stop["audncode_superseded_host_stop_proofs"],
            [],
            idle_before_stop,
        )

        self.run_audncode_hook(first, host=first_host)
        _state_path, stopped_old_host = self.read_audncode_session_state(session_id)
        self.assertTrue(stopped_old_host["audncode_multi_host_conflict"], stopped_old_host)
        self.assertEqual(len(stopped_old_host["audncode_host_lifetimes"]), 2, stopped_old_host)
        self.assertEqual(
            len(stopped_old_host["audncode_superseded_host_stop_proofs"]),
            1,
            stopped_old_host,
        )
        old_host_proof = stopped_old_host["audncode_superseded_host_stop_proofs"][0]
        self.assertEqual(old_host_proof["pid"], first_host[0].pid, stopped_old_host)
        self.assertEqual(old_host_proof["started_unix_ms"], first_host[1], stopped_old_host)

        # The owning host may finish first. Its busy -> idle rewrite must keep
        # the one-element proof as a JSON array so the old owner's later idle
        # can complete the pair instead of leaving a permanent conflict.
        self.run_audncode_hook(second, host=second_host)
        self.run_audncode_hook(self.audncode_idle_event(second), host=second_host)
        _state_path, owning_host_idle = self.read_audncode_session_state(session_id)
        self.assertEqual(owning_host_idle["state"], "idle", owning_host_idle)
        self.assertTrue(owning_host_idle["audncode_multi_host_conflict"], owning_host_idle)
        self.assertEqual(len(owning_host_idle["audncode_host_lifetimes"]), 2, owning_host_idle)
        self.assertIsInstance(
            owning_host_idle["audncode_superseded_host_stop_proofs"],
            list,
            owning_host_idle,
        )
        self.assertEqual(
            len(owning_host_idle["audncode_superseded_host_stop_proofs"]),
            1,
            owning_host_idle,
        )
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

        self.run_audncode_hook(self.audncode_idle_event(first), host=first_host)
        _state_path, retired_old_host = self.read_audncode_session_state(session_id)
        self.assertEqual(retired_old_host["state"], "idle", retired_old_host)
        self.assertFalse(
            retired_old_host["audncode_multi_host_conflict"],
            f"{retired_old_host}\n{self.state_debug()}",
        )
        self.assertEqual(len(retired_old_host["audncode_host_lifetimes"]), 1, retired_old_host)
        self.assertEqual(
            retired_old_host["audncode_host_lifetimes"][0]["pid"],
            second_host[0].pid,
            retired_old_host,
        )
        self.assertEqual(
            retired_old_host["audncode_superseded_host_stop_proofs"],
            [],
            retired_old_host,
        )
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Only the owning host completed", payloads[0]["message"])
        self.assertNotIn("Wrong host", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cron_observation_requires_postinstall_host_restart(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-observation-project"
        project_root.mkdir()
        preinstall_host = self.start_audncode_host(session_id, cwd=str(project_root))
        self.audncode_hosts[session_id] = preinstall_host
        preinstall_started = preinstall_host[1]
        installed_unix_ms = max(int(time.time() * 1000), preinstall_started + 1)
        self.write_audncode_hook_observation_marker(
            installed_unix_ms=installed_unix_ms,
            generation=uuid.uuid4().hex,
        )
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )

        # A prompt seen after installation cannot prove that this already-live
        # process had no memory-only CronCreate before hooks were installed.
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Still old host"))
        self.run_audncode_hook({**event, "last_assistant_message": "Old host must stay blocked."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        preinstall_host[0].terminate()
        preinstall_host[0].communicate(timeout=10)
        while int(time.time() * 1000) <= installed_unix_ms:
            time.sleep(0.005)
        postinstall_host = self.start_audncode_host(session_id, cwd=str(project_root))
        self.audncode_hosts[session_id] = postinstall_host
        restarted = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
            message="Restarted host is fully observable.",
        )
        self.run_audncode_hook(self.audncode_prompt_event(restarted, prompt="Fresh host"))
        self.run_audncode_hook(restarted)
        self.run_audncode_hook(self.audncode_idle_event(restarted))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("fully observable", payloads[0]["message"])
        self.assertNotIn("Old host", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_preinstall_exited_host_keeps_durable_cron_after_restart(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-preinstall-durable-project"
        project_root.mkdir()
        preinstall_host = self.start_audncode_host(session_id, cwd=str(project_root))
        self.audncode_hosts[session_id] = preinstall_host
        preinstall_started = preinstall_host[1]
        installed_unix_ms = max(int(time.time() * 1000), preinstall_started + 1)
        self.write_audncode_hook_observation_marker(
            installed_unix_ms=installed_unix_ms,
            generation=uuid.uuid4().hex,
        )

        cron_directory = project_root / ".claude"
        cron_directory.mkdir(parents=True)
        (cron_directory / "scheduled_tasks.json").write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": "d00df00d",
                            "cron": "*/5 * * * *",
                            "prompt": "Remain durable across the host restart",
                            "createdAt": int(time.time() * 1000),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Observe pre-install durable work")
        )
        cron_states = list((self.state / "claude-sessions").glob("audn-cron-*.json"))
        self.assertEqual(len(cron_states), 1, self.state_debug())
        preinstall_cron_state = json.loads(cron_states[0].read_text(encoding="utf-8-sig"))
        self.assertFalse(preinstall_cron_state["registry_valid"], preinstall_cron_state)
        self.assertTrue(preinstall_cron_state["pre_observation_only"], preinstall_cron_state)

        preinstall_host[0].terminate()
        preinstall_host[0].communicate(timeout=10)
        while int(time.time() * 1000) <= installed_unix_ms:
            time.sleep(0.005)
        postinstall_host = self.start_audncode_host(session_id, cwd=str(project_root))
        self.audncode_hosts[session_id] = postinstall_host
        restarted = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(restarted, prompt="Do not retire durable work")
        )

        _state_path, state = self.read_audncode_session_state(session_id)
        self.assertTrue(state["audncode_multi_host_conflict"], state)
        self.assertEqual(len(state["audncode_host_lifetimes"]), 2, state)
        self.assertEqual(
            {lifetime["pid"] for lifetime in state["audncode_host_lifetimes"]},
            {preinstall_host[0].pid, postinstall_host[0].pid},
            state,
        )
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_preinstall_candidate_releases_after_exact_host_exit(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-preinstall-exit-project"
        project_root.mkdir()
        preinstall_host = self.start_audncode_host(session_id, cwd=str(project_root))
        self.audncode_hosts[session_id] = preinstall_host
        self.write_audncode_hook_observation_marker(
            installed_unix_ms=max(int(time.time() * 1000), preinstall_host[1] + 1),
            generation=uuid.uuid4().hex,
        )
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
            message="Exited pre-install host is now final.",
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Finish on the pre-install host")
        )
        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))

        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        preinstall_host[0].terminate()
        preinstall_host[0].communicate(timeout=10)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("pre-install host is now final", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_observation_uses_process_start_not_delayed_session_marker(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-delayed-marker-project"
        project_root.mkdir()
        host = self.start_audncode_host(session_id, cwd=str(project_root))
        installed_unix_ms = max(int(time.time() * 1000), host[1] + 1)
        self.write_audncode_hook_observation_marker(
            installed_unix_ms=installed_unix_ms,
            generation=uuid.uuid4().hex,
        )

        # AudnCode writes sessions/<pid>.json after awaited startup work. Model
        # a delayed marker that appears post-install even though Process.StartTime
        # predates the hook generation; marker.startedAt must not make it observable.
        delayed_marker_started = installed_unix_ms + 1
        delayed_host = (host[0], delayed_marker_started)
        self.audncode_hosts[session_id] = delayed_host
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Use the real process start")
        )

        cron_states = list((self.state / "claude-sessions").glob("audn-cron-*.json"))
        self.assertEqual(len(cron_states), 1, self.state_debug())
        cron_state = json.loads(cron_states[0].read_text(encoding="utf-8-sig"))
        self.assertLessEqual(cron_state["host_process_started_unix_ms"], installed_unix_ms)
        self.assertGreater(cron_state["host_started_unix_ms"], installed_unix_ms)
        self.assertFalse(cron_state["registry_valid"], cron_state)
        self.assertTrue(cron_state["pre_observation_only"], cron_state)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_preinstall_runtime_is_not_normalized_before_validation(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-preinstall-corrupt-runtime-project"
        project_root.mkdir()
        preinstall_host = self.start_audncode_host(session_id, cwd=str(project_root))
        self.audncode_hosts[session_id] = preinstall_host
        installed_unix_ms = max(int(time.time() * 1000), preinstall_host[1] + 1)
        self.write_audncode_hook_observation_marker(
            installed_unix_ms=installed_unix_ms,
            generation=uuid.uuid4().hex,
        )
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Persist a strict pre-install runtime")
        )
        old_runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        corrupted = json.loads(old_runtime_path.read_text(encoding="utf-8-sig"))
        corrupted["sessions"] = corrupted["sessions"][0]
        old_runtime_path.write_text(json.dumps(corrupted), encoding="utf-8")

        preinstall_host[0].terminate()
        preinstall_host[0].communicate(timeout=10)
        while int(time.time() * 1000) <= installed_unix_ms:
            time.sleep(0.005)
        postinstall_host = self.start_audncode_host(session_id, cwd=str(project_root))
        self.audncode_hosts[session_id] = postinstall_host
        restarted = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(restarted, prompt="Do not normalize old evidence")
        )

        _state_path, state = self.read_audncode_session_state(session_id)
        self.assertTrue(state["audncode_multi_host_conflict"], state)
        self.assertEqual(len(state["audncode_host_lifetimes"]), 2, state)
        persisted = json.loads(old_runtime_path.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(persisted["sessions"], dict, persisted)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cron_mutation_preserves_preinstall_runtime_mode(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        first_session = str(uuid.uuid4())
        first_transcript = self.audncode_home / "projects" / f"{first_session}.jsonl"
        first_transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-preinstall-shared-cron-project"
        project_root.mkdir()
        preinstall_host = self.start_audncode_host(first_session, cwd=str(project_root))
        self.audncode_hosts[first_session] = preinstall_host
        installed_unix_ms = max(int(time.time() * 1000), preinstall_host[1] + 1)
        self.write_audncode_hook_observation_marker(
            installed_unix_ms=installed_unix_ms,
            generation=uuid.uuid4().hex,
        )
        first = self.audncode_event(
            session_id=first_session,
            transcript_path=first_transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(first, prompt="Keep pre-install runtime mode")
        )
        preinstall_runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))

        while int(time.time() * 1000) <= installed_unix_ms:
            time.sleep(0.005)
        second_session = str(uuid.uuid4())
        second_transcript = self.audncode_home / "projects" / f"{second_session}.jsonl"
        second_transcript.write_text("", encoding="utf-8")
        postinstall_host = self.start_audncode_host(second_session, cwd=str(project_root))
        self.audncode_hosts[second_session] = postinstall_host
        second = self.audncode_event(
            session_id=second_session,
            transcript_path=second_transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(second, prompt="Mutate cron from observable host")
        )
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                second,
                action="create",
                cron_id="faceb00c",
                durable=False,
            )
        )

        preinstall_runtime = json.loads(
            preinstall_runtime_path.read_text(encoding="utf-8-sig")
        )
        self.assertFalse(preinstall_runtime["registry_valid"], preinstall_runtime)
        self.assertTrue(preinstall_runtime["pre_observation_only"], preinstall_runtime)
        observable_runtime = next(
            json.loads(path.read_text(encoding="utf-8-sig"))
            for path in (self.state / "claude-sessions").glob("audn-cron-*.json")
            if path != preinstall_runtime_path
        )
        self.assertTrue(observable_runtime["registry_valid"], observable_runtime)
        self.assertFalse(observable_runtime["pre_observation_only"], observable_runtime)
        self.assertIn("faceb00c", {cron["id"] for cron in observable_runtime["crons"]})
        _state_path, second_state = self.read_audncode_session_state(second_session)
        self.assertFalse(second_state["audncode_cron_lifecycle_unverifiable"], second_state)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cron_registry_compacts_257_idle_sessions(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-cron-compaction-project"
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Create runtime"))
        cron_states = list((self.state / "claude-sessions").glob("audn-cron-*.json"))
        self.assertEqual(len(cron_states), 1, self.state_debug())
        cron_state_path = cron_states[0]
        cron_state = json.loads(cron_state_path.read_text(encoding="utf-8-sig"))
        cron_state["sessions"].extend(
            {
                "session_id": str(uuid.uuid4()),
                "project_root": str(project_root.resolve()),
                "last_seen_unix_ms": index + 1,
            }
            for index in range(257)
        )
        cron_state_path.write_text(json.dumps(cron_state), encoding="utf-8")

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Compact runtime"))
        compacted = json.loads(cron_state_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(compacted["registry_valid"], compacted)
        self.assertLessEqual(len(compacted["sessions"]), 48)
        self.assertIn(session_id, {item["session_id"] for item in compacted["sessions"]})

        self.run_audncode_hook({**event, "last_assistant_message": "Compacted cron lineage."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Compacted cron lineage", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_session_cron_blocks_until_trusted_crondelete(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-session-cron-project"
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        cron_id = "a1b2c3d4"

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Schedule a check"))
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                event,
                action="create",
                cron_id=cron_id,
                recurring=False,
                durable=False,
            )
        )
        self.run_audncode_hook({**event, "last_assistant_message": "The timer is armed."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Cancel the check"))
        delete_event = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_id
        )
        self.append_audncode_cron_tool_proof(event, delete_event)
        self.run_audncode_hook(delete_event)
        cron_state_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        cron_state = json.loads(cron_state_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(cron_state["registry_valid"], cron_state)
        self.assertEqual(cron_state["crons"], [], cron_state)
        self.assertEqual(
            {claim["id"] for claim in cron_state["delete_claims"]},
            {cron_id},
            cron_state,
        )
        self.assertRegex(
            cron_state["delete_claims"][0]["transcript_proof_hash"], r"^[a-f0-9]{64}$"
        )
        self.assertEqual(cron_state["retired_session_cron_ids"], [cron_id])
        self.run_audncode_hook({**event, "last_assistant_message": "The timer was cancelled."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("timer was cancelled", payloads[0]["message"])
        self.assertNotIn("timer is armed", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_crondelete_without_transcript_tool_result_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=self.temp / "audncode-cron-delete-no-proof",
        )
        cron_id = "bad0cafe"
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Create a timer"))
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                event, action="create", cron_id=cron_id, durable=False
            )
        )

        # A synthetic PostToolUse payload with a valid-looking response is not
        # proof that AudnCode actually persisted and completed this tool use.
        synthetic_delete = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_id
        )
        self.run_audncode_hook(synthetic_delete)

        runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertFalse(runtime["registry_valid"], runtime)
        self.assertEqual({item["id"] for item in runtime["crons"]}, {cron_id}, runtime)
        self.assertEqual(runtime["delete_claims"], [], runtime)
        session_key = hashlib.sha256(
            f"codex-ntfy/v1|claude-session|{session_id}".encode("utf-8")
        ).hexdigest()
        session_path = self.state / "claude-sessions" / f"{session_key}.json"
        session = json.loads(session_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(session["audncode_cron_lifecycle_unverifiable"], session)

        self.run_audncode_hook({**event, "last_assistant_message": "Unproven delete."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        blocked_worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if blocked_worker.poll() is None:
                blocked_worker.terminate()
            blocked_worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_crondelete_replay_and_same_id_recreation_stay_sticky(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=self.temp / "audncode-cron-delete-replay",
        )
        cron_id = "d15ea5ed"
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="First incarnation"))
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                event, action="create", cron_id=cron_id, durable=False
            )
        )
        delete_event = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_id
        )
        self.append_audncode_cron_tool_proof(event, delete_event)
        self.run_audncode_hook(delete_event)

        runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        first_delete = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(first_delete["registry_valid"], first_delete)
        self.assertEqual(first_delete["crons"], [], first_delete)
        self.assertEqual(first_delete["retired_session_cron_ids"], [cron_id])
        self.assertEqual(len(first_delete["delete_claims"]), 1, first_delete)

        # Retrying the exact PostToolUse receipt in the same prompt is
        # idempotent: it neither appends a second receipt nor mutates state.
        self.run_audncode_hook(delete_event)
        replayed = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(replayed["registry_valid"], replayed)
        self.assertEqual(replayed["crons"], [], replayed)
        self.assertEqual(len(replayed["delete_claims"]), 1, replayed)

        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Try a second incarnation")
        )
        recreated = self.audncode_cron_tool_event(
            event, action="create", cron_id=cron_id, durable=False
        )
        recreated["tool_use_id"] = f"cron-create-{cron_id}-second"
        recreated["tool_input"]["prompt"] = "Second incarnation"
        self.run_audncode_hook(recreated)
        after_recreate = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertFalse(after_recreate["registry_valid"], after_recreate)
        self.assertEqual(after_recreate["crons"], [], after_recreate)
        self.assertEqual(after_recreate["retired_session_cron_ids"], [cron_id])

        # The old pair is now before the new prompt cursor and cannot become
        # proof for this epoch or target a hypothetical successor.
        self.run_audncode_hook(delete_event)
        after_late_replay = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertFalse(after_late_replay["registry_valid"], after_late_replay)
        self.assertEqual(after_late_replay["crons"], [], after_late_replay)
        self.assertEqual(len(after_late_replay["delete_claims"]), 1, after_late_replay)

    def test_audncode_cron_handler_prearms_before_correlation_checks(self) -> None:
        source = POWERSHELL_NOTIFIER.read_text(encoding="utf-8")
        branch = source.index("if ($toolName -in @('CronCreate', 'CronDelete'))")
        branch_end = source.index("$backgroundIds = @(", branch)
        cron_branch = source[branch:branch_end]
        arm = cron_branch.index("Start-AudnCodeCronLifecycleMutation")
        transcript = cron_branch.index("Test-AudnCodeTranscriptPath")
        update = cron_branch.index("Update-AudnCodeCronFromToolEvent")
        self.assertLess(arm, transcript)
        self.assertLess(arm, update)

        update_start = source.index("function Update-AudnCodeCronFromToolEvent")
        update_end = source.index("function Set-AudnCodeCronLifecycleUnverifiable", update_start)
        update_body = source[update_start:update_end]
        self.assertIn("Get-AudnCodeHostSession", update_body)
        self.assertNotIn("Start-AudnCodeCronLifecycleMutation", update_body)

        background_branch = source.index("$claimsBackgroundLaunch =", branch)
        background_end = source.index("if (-not [bool]$updated)", background_branch)
        background_body = source[background_branch:background_end]
        background_arm = background_body.index("Start-AudnCodeBackgroundLifecycleMutation")
        background_transcript = background_body.index("Test-AudnCodeTranscriptPath")
        background_update = background_body.index("Add-AudnCodeBackgroundIds")
        self.assertLess(background_arm, background_transcript)
        self.assertLess(background_arm, background_update)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_uncorrelated_cron_lifecycle_is_sticky_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-lost-cron-hook-project"
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        cron_id = "e1f2a3b4"

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Schedule hidden work"))
        host_process, _host_started_at = self.audncode_hosts[session_id]
        marker_path = self.audncode_home / "sessions" / f"{host_process.pid}.json"
        marker_bytes = marker_path.read_bytes()
        marker_path.unlink()
        try:
            self.run_audncode_hook(
                self.audncode_cron_tool_event(
                    event,
                    action="create",
                    cron_id=cron_id,
                    durable=False,
                )
            )
        finally:
            marker_path.write_bytes(marker_bytes)

        guards = list((self.state / "claude-sessions").glob("audn-lifecycle-cron-*.json"))
        self.assertEqual(len(guards), 1, self.state_debug())
        guard = json.loads(guards[0].read_text(encoding="utf-8-sig"))
        self.assertTrue(guard["lost"], guard)
        self.assertEqual(guard["pending_token"], "", guard)

        self.run_audncode_hook(
            {**event, "last_assistant_message": "A lost cron hook must block this result."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))

        def assert_no_payload() -> None:
            worker = self.start_worker("powershell")
            try:
                time.sleep(1.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)

        assert_no_payload()

        # Once observation was lost, even a later valid-looking delete cannot
        # prove what the missing create produced. Only a new host runtime can
        # safely reset the sticky failure.
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Try to cancel hidden work"))
        delete_event = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_id
        )
        self.append_audncode_cron_tool_proof(event, delete_event)
        self.run_audncode_hook(delete_event)
        self.run_audncode_hook(
            {**event, "last_assistant_message": "A later delete cannot unlock uncertainty."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        assert_no_payload()

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_maintenance_preserves_live_audncode_cron_runtime_and_guard(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0, sent_retention_days=1)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-live-cleanup-project"
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Keep live cron state"))
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                event,
                action="create",
                cron_id="c1d2e3f4",
                durable=False,
            )
        )
        background_paths = list(
            (self.state / "claude-sessions").glob("audn-runtime-*.json")
        )
        cron_paths = list((self.state / "claude-sessions").glob("audn-cron-*.json"))
        guard_paths = list(
            (self.state / "claude-sessions").glob("audn-lifecycle-cron-*.json")
        )
        self.assertEqual(len(background_paths), 1, self.state_debug())
        self.assertEqual(len(cron_paths), 1, self.state_debug())
        self.assertEqual(len(guard_paths), 1, self.state_debug())
        tracked = background_paths + cron_paths + guard_paths
        old = time.time() - (3 * 24 * 60 * 60)
        for path in tracked:
            os.utime(path, (old, old))

        maintenance = [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_NOTIFIER),
            "-Maintenance",
        ]
        live_maintenance = subprocess.run(
            maintenance,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(
            live_maintenance.returncode,
            0,
            f"stdout={live_maintenance.stdout}\nstderr={live_maintenance.stderr}\n{self.state_debug()}",
        )
        self.assertTrue(all(path.exists() for path in tracked), self.state_debug())

        host_process, _host_started_at = self.audncode_hosts.pop(session_id)
        host_process.terminate()
        host_process.communicate(timeout=10)
        for path in tracked:
            os.utime(path, (old, old))
        exited_maintenance = subprocess.run(
            maintenance,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(
            exited_maintenance.returncode,
            0,
            f"stdout={exited_maintenance.stdout}\nstderr={exited_maintenance.stderr}\n{self.state_debug()}",
        )
        self.assertTrue(all(not path.exists() for path in tracked), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_host_keeps_lost_lifecycle_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=self.temp / "audncode-exited-lost-project",
        )
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Launch uncertain work"))
        host_process, _host_started_at = self.audncode_hosts[session_id]
        marker_path = self.audncode_home / "sessions" / f"{host_process.pid}.json"
        marker_bytes = marker_path.read_bytes()
        marker_path.unlink()
        try:
            self.run_audncode_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "cwd": event["cwd"],
                    "tool_name": "Agent",
                    "tool_input": {"run_in_background": True},
                    "tool_response": {
                        "status": "async_launched",
                        "agentId": "agent-exited-lost",
                    },
                    "tool_use_id": "tool-use-exited-lost",
                }
            )
        finally:
            marker_path.write_bytes(marker_bytes)

        guards = list(
            (self.state / "claude-sessions").glob("audn-lifecycle-background-*.json")
        )
        self.assertEqual(len(guards), 1, self.state_debug())
        self.assertTrue(
            json.loads(guards[0].read_text(encoding="utf-8-sig"))["lost"],
            self.state_debug(),
        )
        self.run_audncode_hook(
            {**event, "last_assistant_message": "The exited host may have unobserved detached work."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.exit_audncode_host(session_id)

        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_host_keeps_pending_lifecycle_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=self.temp / "audncode-exited-pending-project",
        )
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Launch interrupted work"))

        marker_dir = self.temp / "audncode-exited-pending-markers"
        release = self.temp / "audncode-exited-pending.release"
        child_started = self.temp / "audncode-exited-pending-child.pid"
        self.env.update(
            {
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_MARKER_DIR": str(marker_dir),
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_RELEASE": str(release),
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_WAIT_MS": "10000",
            }
        )
        launch = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": event["cwd"],
            "tool_name": "Agent",
            "tool_input": {"run_in_background": True},
            "tool_response": {
                "status": "async_launched",
                "agentId": "agent-exited-pending",
            },
            "tool_use_id": "tool-use-exited-pending",
        }
        launch_errors: list[BaseException] = []

        def launch_until_killed() -> None:
            try:
                self.run_audncode_hook(
                    launch,
                    child_started_path=child_started,
                    expect_success=False,
                )
            except BaseException as error:
                launch_errors.append(error)

        launch_thread = threading.Thread(target=launch_until_killed, daemon=True)
        launch_thread.start()
        try:
            deadline = time.time() + 30
            markers: list[Path] = []
            while time.time() < deadline:
                markers = list(marker_dir.glob("background-*.marker"))
                if len(markers) == 1 and child_started.exists():
                    break
                time.sleep(0.05)
            self.assertEqual(len(markers), 1, self.state_debug())
            child_pid = int(child_started.read_text(encoding="ascii"))
            self.taskkill_tree_if_running(child_pid)
        finally:
            release.write_text("release", encoding="ascii")
            launch_thread.join(timeout=30)
            for name in (
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_MARKER_DIR",
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_RELEASE",
                "CODEX_NTFY_TEST_LIFECYCLE_ARM_WAIT_MS",
            ):
                self.env.pop(name, None)
        self.assertFalse(launch_thread.is_alive(), "interrupted AudnCode hook hung")
        if launch_errors:
            raise launch_errors[0]

        guards = list(
            (self.state / "claude-sessions").glob("audn-lifecycle-background-*.json")
        )
        self.assertEqual(len(guards), 1, self.state_debug())
        guard = json.loads(guards[0].read_text(encoding="utf-8-sig"))
        self.assertFalse(guard["lost"], guard)
        self.assertEqual(len(guard["pending_tokens"]), 1, guard)
        self.run_audncode_hook(
            {**event, "last_assistant_message": "The interrupted hook's host exited."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.exit_audncode_host(session_id)

        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_host_rechecks_cron_lifecycle_guard(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=self.temp / "audncode-exited-cron-guard-project",
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Prove the exited cron guard is clear")
        )

        cron_id = "f00dcafe"
        create_event = self.audncode_cron_tool_event(
            event, action="create", cron_id=cron_id
        )
        self.append_audncode_cron_tool_proof(event, create_event)
        self.run_audncode_hook(create_event)
        delete_event = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_id
        )
        self.append_audncode_cron_tool_proof(event, delete_event)
        self.run_audncode_hook(delete_event)

        guard_paths = list(
            (self.state / "claude-sessions").glob("audn-lifecycle-cron-*.json")
        )
        self.assertEqual(len(guard_paths), 1, self.state_debug())
        guard = json.loads(guard_paths[0].read_text(encoding="utf-8-sig"))
        self.assertFalse(guard["lost"], guard)
        self.assertEqual(guard["pending_tokens"], [], guard)
        guard["lost"] = True
        guard["safe_to_finalize"] = False
        guard["operation_committed"] = False
        guard_paths[0].write_text(json.dumps(guard), encoding="utf-8")

        self.run_audncode_hook(
            {**event, "last_assistant_message": "The cron lifecycle guard was lost."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.exit_audncode_host(session_id)

        worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            self.assertEqual(
                len(list((self.state / "pending").glob("*.json"))),
                1,
                self.state_debug(),
            )
            self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_host_keeps_all_durable_cron_evidence_fail_closed(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            sent_retention_days=1,
        )

        # A durable cron owned by an earlier /clear lineage project remains
        # relevant even when the final Stop belongs to a different project.
        first_session = str(uuid.uuid4())
        first_transcript = self.audncode_home / "projects" / f"{first_session}.jsonl"
        first_transcript.write_text("", encoding="utf-8")
        first_project = self.temp / "audncode-exited-lineage-first"
        first = self.audncode_event(
            session_id=first_session,
            transcript_path=first_transcript,
            project_root=first_project,
        )
        self.run_audncode_hook(self.audncode_prompt_event(first, prompt="Create durable lineage work"))
        cron_dir = first_project / ".claude"
        cron_dir.mkdir(parents=True)
        (cron_dir / "scheduled_tasks.json").write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": "a1b2c3d4",
                            "cron": "*/5 * * * *",
                            "prompt": "Persist across host exits",
                            "createdAt": int(time.time() * 1000),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                first,
                action="create",
                cron_id="a1b2c3d4",
                durable=True,
            )
        )
        second_session = str(uuid.uuid4())
        second_transcript = self.audncode_home / "projects" / f"{second_session}.jsonl"
        second_transcript.write_text("", encoding="utf-8")
        self.audncode_hosts[second_session] = self.audncode_hosts[first_session]
        second = self.audncode_event(
            session_id=second_session,
            transcript_path=second_transcript,
            project_root=self.temp / "audncode-exited-lineage-second",
        )
        self.run_audncode_hook(self.audncode_prompt_event(second, prompt="Finish after clear"))
        self.run_audncode_hook({**second, "last_assistant_message": "Lineage still has a durable cron."})
        self.run_audncode_hook(self.audncode_idle_event(second))
        self.exit_audncode_host(second_session)
        self.audncode_hosts.pop(first_session, None)

        # A tracked durable ID with a missing canonical file is unknown, not
        # idle. Keep its runtime even after the normal retention cutoff while
        # the pending completion still depends on those expectations.
        missing_session = str(uuid.uuid4())
        missing_transcript = self.audncode_home / "projects" / f"{missing_session}.jsonl"
        missing_transcript.write_text("", encoding="utf-8")
        missing_event = self.audncode_event(
            session_id=missing_session,
            transcript_path=missing_transcript,
            project_root=self.temp / "audncode-exited-missing-durable",
        )
        self.run_audncode_hook(self.audncode_prompt_event(missing_event, prompt="Track a durable cron"))
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                missing_event,
                action="create",
                cron_id="b1c2d3e4",
                durable=True,
            )
        )
        self.run_audncode_hook(
            {**missing_event, "last_assistant_message": "Missing durable evidence is not final."}
        )
        self.run_audncode_hook(self.audncode_idle_event(missing_event))
        missing_pid = self.audncode_hosts[missing_session][0].pid
        self.exit_audncode_host(missing_session)
        missing_runtime = next(
            path
            for path in (self.state / "claude-sessions").glob("audn-cron-*.json")
            if json.loads(path.read_text(encoding="utf-8-sig")).get("host_pid") == missing_pid
        )
        old = time.time() - (3 * 24 * 60 * 60)
        os.utime(missing_runtime, (old, old))
        maintenance = [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_NOTIFIER),
            "-Maintenance",
        ]
        self.run_ok(maintenance)
        self.assertTrue(missing_runtime.exists(), self.state_debug())

        malformed_session = str(uuid.uuid4())
        malformed_transcript = self.audncode_home / "projects" / f"{malformed_session}.jsonl"
        malformed_transcript.write_text("", encoding="utf-8")
        malformed_project = self.temp / "audncode-exited-malformed-durable"
        malformed_event = self.audncode_event(
            session_id=malformed_session,
            transcript_path=malformed_transcript,
            project_root=malformed_project,
        )
        malformed_dir = malformed_project / ".claude"
        malformed_dir.mkdir(parents=True)
        (malformed_dir / "scheduled_tasks.json").write_text(
            '{"tasks":"not-an-array"}',
            encoding="utf-8",
        )
        self.run_audncode_hook(self.audncode_prompt_event(malformed_event, prompt="Inspect malformed cron evidence"))
        self.run_audncode_hook(
            {**malformed_event, "last_assistant_message": "Malformed durable evidence is not final."}
        )
        self.run_audncode_hook(self.audncode_idle_event(malformed_event))
        self.exit_audncode_host(malformed_session)

        worker = self.start_worker("powershell")
        try:
            time.sleep(2.2)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_abrupt_host_exit_keeps_detached_background_work_fail_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Launch detached work before a crash")
        )

        launcher = r"""
import subprocess
import sys
flags = (
    getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    creationflags=flags,
)
print(child.pid, flush=True)
"""
        host_process = self.audncode_hosts[session_id][0]
        self.assertIsNotNone(host_process.stdin)
        self.assertIsNotNone(host_process.stdout)
        request = {
            "command": [sys.executable, "-c", launcher],
            "input": base64.b64encode(b"").decode("ascii"),
            "env": dict(self.env),
        }
        host_process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        host_process.stdin.flush()
        response_line = host_process.stdout.readline()
        self.assertTrue(response_line, "AudnCode host exited before detached child launch")
        response = json.loads(response_line.decode("utf-8"))
        self.assertEqual(response["returncode"], 0, response)
        detached_pid = int(base64.b64decode(response["stdout"]).decode("ascii").strip())

        try:
            self.run_audncode_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "cwd": event["cwd"],
                    "tool_name": "Agent",
                    "tool_input": {"run_in_background": True},
                    "tool_response": {
                        "status": "async_launched",
                        "agentId": "a1234abcd",
                    },
                    "tool_use_id": "tool-use-detached-crash",
                }
            )
            self.run_audncode_hook(
                {**event, "last_assistant_message": "Parent exited while detached work remained."}
            )
            self.run_audncode_hook(self.audncode_idle_event(event))
            self.exit_audncode_host(session_id)

            child_check = subprocess.run(
                [
                    str(WINDOWS_POWERSHELL),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"if (Get-Process -Id {detached_pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
                ],
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(child_check.returncode, 0, "detached child did not survive parent exit")

            worker = self.start_worker("powershell")
            try:
                deadline = time.monotonic() + 20
                record: dict = {}
                while time.monotonic() < deadline:
                    pending = list((self.state / "pending").glob("*.json"))
                    if len(pending) == 1:
                        try:
                            record = json.loads(pending[0].read_text(encoding="utf-8-sig"))
                        except (OSError, json.JSONDecodeError):
                            record = {}
                        if record.get("gate_reason"):
                            break
                    time.sleep(0.1)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
                pending = list((self.state / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1, self.state_debug())
                self.assertEqual(
                    record.get("gate_reason"),
                    "audncode-detached-background-active",
                    record,
                )
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)
        finally:
            subprocess.run(
                ["taskkill", "/PID", str(detached_pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_superseded_host_detached_background_survives_refresh(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            message="Current owner must wait for the exited host's detached work.",
        )
        exited_host = self.audncode_hosts[session_id]
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Launch detached work on the first host"),
            host=exited_host,
        )

        launcher = r"""
import subprocess
import sys
flags = (
    getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    creationflags=flags,
)
print(child.pid, flush=True)
"""
        host_process = exited_host[0]
        self.assertIsNotNone(host_process.stdin)
        self.assertIsNotNone(host_process.stdout)
        host_process.stdin.write(
            json.dumps(
                {
                    "command": [sys.executable, "-c", launcher],
                    "input": base64.b64encode(b"").decode("ascii"),
                    "env": dict(self.env),
                }
            ).encode("utf-8")
            + b"\n"
        )
        host_process.stdin.flush()
        response_line = host_process.stdout.readline()
        self.assertTrue(response_line, "AudnCode host exited before detached child launch")
        response = json.loads(response_line.decode("utf-8"))
        self.assertEqual(response["returncode"], 0, response)
        detached_pid = int(base64.b64decode(response["stdout"]).decode("ascii").strip())

        try:
            self.run_audncode_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "cwd": event["cwd"],
                    "tool_name": "Agent",
                    "tool_input": {"run_in_background": True},
                    "tool_response": {
                        "status": "async_launched",
                        "agentId": "a1b2c3d4",
                    },
                    "tool_use_id": "tool-use-exited-superseded-detached",
                },
                host=exited_host,
            )
            current_host = self.start_audncode_host(session_id, cwd=event["cwd"])
            self.audncode_hosts[f"{session_id}:current-owner"] = current_host
            self.run_audncode_hook(
                self.audncode_prompt_event(event, prompt="Continue on the second host"),
                host=current_host,
            )
            _state_path, conflicted = self.read_audncode_session_state(session_id)
            self.assertTrue(conflicted["audncode_multi_host_conflict"], conflicted)
            self.assertEqual(
                {item["pid"] for item in conflicted["audncode_host_lifetimes"]},
                {exited_host[0].pid, current_host[0].pid},
                conflicted,
            )

            self.exit_audncode_host(session_id)
            self.assertTrue(self.windows_pid_is_alive(detached_pid), "detached child did not survive")
            # A later valid hook from B must not discard A's exited tuple before
            # Refresh gets the chance to validate A's host-specific finality.
            self.run_audncode_hook(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "cwd": event["cwd"],
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo current-owner"},
                    "tool_response": {"stdout": "current-owner", "stderr": ""},
                    "tool_use_id": "tool-use-current-owner-after-exit",
                },
                host=current_host,
            )
            _state_path, after_observation = self.read_audncode_session_state(session_id)
            self.assertEqual(
                {item["pid"] for item in after_observation["audncode_host_lifetimes"]},
                {exited_host[0].pid, current_host[0].pid},
                after_observation,
            )
            self.run_audncode_hook(event, host=current_host)
            self.run_audncode_hook(self.audncode_idle_event(event), host=current_host)

            _state_path, blocked = self.read_audncode_session_state(session_id)
            self.assertTrue(blocked["audncode_multi_host_conflict"], blocked)
            self.assertEqual(
                {item["pid"] for item in blocked["audncode_host_lifetimes"]},
                {exited_host[0].pid, current_host[0].pid},
                blocked,
            )
            worker = self.start_worker("powershell")
            try:
                time.sleep(2.0)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)
        finally:
            subprocess.run(
                ["taskkill", "/PID", str(detached_pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_host_candidate_loses_to_new_prompt_epoch(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=self.temp / "audncode-exited-epoch-project",
        )
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Finish the old epoch"))
        self.run_audncode_hook({**event, "last_assistant_message": "Old epoch result."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.exit_audncode_host(session_id)

        after_gate_marker = self.temp / "audncode-exited-after-gate.marker"
        after_gate_release = self.temp / "audncode-exited-after-gate.release"
        self.env.update(
            {
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS": "10000",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER": str(after_gate_marker),
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE": str(after_gate_release),
            }
        )
        worker = self.start_worker("powershell")
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not after_gate_marker.exists():
                time.sleep(0.05)
            self.assertTrue(after_gate_marker.exists(), self.state_debug())

            resumed = self.audncode_event(
                session_id=session_id,
                transcript_path=transcript,
                project_root=self.temp / "audncode-exited-epoch-project",
            )
            self.run_audncode_hook(
                self.audncode_prompt_event(resumed, prompt="Start a newer epoch")
            )
            after_gate_release.write_text("release", encoding="ascii")
            self.assert_worker_ok(worker, timeout=30)
        finally:
            after_gate_release.write_text("release", encoding="ascii")
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
            for name in (
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MS",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_MARKER",
                "CODEX_NTFY_TEST_AFTER_FINAL_GATE_RELEASE",
            ):
                self.env.pop(name, None)
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_preparse_rejects_malformed_utf8_and_oversized_payloads(self) -> None:
        malformed_inputs = (
            b'{"hook_event_name":"PostToolUse"',
            b'{"hook_event_name":"PostToolUse","tool_name":"Bash","bad":"\xff"}',
            b"x" * (8 * 1024 * 1024 + 1),
        )
        for index, raw in enumerate(malformed_inputs):
            with self.subTest(index=index):
                session_id = str(uuid.uuid4())
                transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
                transcript.write_text("", encoding="utf-8")
                event = self.audncode_event(
                    session_id=session_id, transcript_path=transcript
                )
                self.run_audncode_hook(self.audncode_prompt_event(event))
                host = self.audncode_hosts[session_id]
                result = self.run_audncode_raw_hook(
                    raw,
                    expected_event="PostToolUse",
                    host=host,
                    close_stdin_after_ms=2000 if index == 2 else 0,
                )
                self.assertEqual(result.stdout.decode("utf-8").strip(), "{}")
                _guard_path, guard = self.read_audncode_ingress_guard(host[0].pid)
                self.assertTrue(guard["lost"], guard)
                self.assertEqual(guard["pending_tokens"], [], guard)
                self.assertFalse(guard["safe_to_finalize"], guard)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_open_stdin_pipe_times_out_fail_closed(self) -> None:
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        host = self.audncode_hosts[session_id]
        child_started = self.temp / "open-stdin-hook.pid"

        started = time.monotonic()
        result = self.run_audncode_raw_hook(
            b"{",
            expected_event="PostToolUse",
            host=host,
            hold_stdin_open=True,
            child_started_path=child_started,
            env_overrides={"CODEX_NTFY_TEST_STDIN_TIMEOUT_MS": "300"},
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 8.0, f"open stdin pipe blocked for {elapsed:.3f}s")
        self.assertEqual(result.stdout.decode("utf-8").strip(), "{}")
        hook_pid = int(child_started.read_text(encoding="ascii"))
        process_probe = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                r"""
$hookPid = [int]$env:CODEX_NTFY_TEST_HOOK_PID
$hook = Get-Process -Id $hookPid -ErrorAction SilentlyContinue
$children = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId = {0}" -f $hookPid) -ErrorAction Stop)
[pscustomobject]@{
  alive = $null -ne $hook
  descendant_pids = @($children | ForEach-Object { [int]$_.ProcessId })
} | ConvertTo-Json -Compress
""",
            ],
            env={**self.env, "CODEX_NTFY_TEST_HOOK_PID": str(hook_pid)},
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(
            process_probe.returncode,
            0,
            process_probe.stdout + process_probe.stderr,
        )
        process_state = json.loads(process_probe.stdout.strip().splitlines()[-1])
        self.assertFalse(process_state["alive"], process_state)
        self.assertEqual(process_state["descendant_pids"], [], process_state)
        _guard_path, guard = self.read_audncode_ingress_guard(host[0].pid)
        self.assertTrue(guard["lost"], guard)
        self.assertEqual(guard["pending_tokens"], [], guard)
        self.assertFalse(guard["safe_to_finalize"], guard)
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())
        log_text = (self.state / "notify.log").read_text(encoding="utf-8-sig")
        self.assertIn("stdin did not reach EOF before the safety deadline", log_text)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_killed_before_stdin_parse_leaves_pending_ingress(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event))
        self.run_audncode_hook(event)
        host = self.audncode_hosts[session_id]
        child_started = self.temp / "ingress-child.pid"
        raw_event = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": event["cwd"],
            "tool_name": "Bash",
            "tool_input": {"command": "echo foreground"},
            "tool_response": {"data": {"status": "completed"}},
            "tool_use_id": "foreground-before-kill",
        }
        results: list[subprocess.CompletedProcess[bytes]] = []
        errors: list[BaseException] = []

        def launch_blocked_reader() -> None:
            try:
                results.append(
                    self.run_audncode_raw_hook(
                        json.dumps(raw_event).encode("utf-8"),
                        expected_event="PostToolUse",
                        host=host,
                        # Keep stdin unavailable beyond the 15 s observation
                        # deadline. The proxy stops waiting as soon as the child
                        # is killed, so this remains fast and deterministic.
                        delay_input_ms=30000,
                        child_started_path=child_started,
                        expect_success=False,
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        thread = threading.Thread(target=launch_blocked_reader, daemon=True)
        thread.start()
        deadline = time.time() + 15
        child_pid = 0
        pending_seen = False
        while time.time() < deadline:
            if child_started.exists():
                child_pid = int(child_started.read_text(encoding="ascii"))
            try:
                _guard_path, guard = self.read_audncode_ingress_guard(host[0].pid)
                pending_seen = len(guard.get("pending_tokens", [])) == 1
            except AssertionError:
                pending_seen = False
            if child_pid > 0 and pending_seen:
                break
            time.sleep(0.02)
        self.assertGreater(child_pid, 0, self.state_debug())
        self.assertTrue(pending_seen, self.state_debug())
        # The hook is the direct PowerShell child and cannot have launched any
        # descendants before stdin is parsed.  Avoid /T here: Windows tree
        # enumeration can outlive the artificial input delay and let the hook
        # commit before taskkill reaches the target.
        kill_result = self.taskkill_tree_if_running(child_pid, timeout=15, tree=False)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "killed pre-parse hook did not return")
        if errors:
            raise errors[0]
        self.assertEqual(len(results), 1)
        if kill_result.returncode == 0:
            self.assertNotEqual(results[0].returncode, 0)
        _guard_path, guard = self.read_audncode_ingress_guard(host[0].pid)
        self.assertEqual(len(guard["pending_tokens"]), 1, guard)

        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_promotion_rechecks_fallback_under_lock_before_ingress_guard(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Do not promote across new hook ingress")
        )
        self.run_audncode_hook({**event, "last_assistant_message": "Candidate before ingress race."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        time.sleep(1.4)

        host = self.audncode_hosts[session_id]
        ingress_guard_path, _guard = self.read_audncode_ingress_guard(host[0].pid)
        ingress_lock_path = ingress_guard_path.with_suffix(".lock")
        promotion_marker = self.temp / "promotion-fallback-read.marker"
        promotion_release = self.temp / "promotion-fallback-read.release"
        worker = subprocess.Popen(
            self.worker_command("powershell"),
            env={
                **self.env,
                "CODEX_NTFY_TEST_AFTER_PROMOTION_FALLBACK_READ_MS": "10000",
                "CODEX_NTFY_TEST_AFTER_PROMOTION_FALLBACK_READ_MARKER": str(
                    promotion_marker
                ),
                "CODEX_NTFY_TEST_AFTER_PROMOTION_FALLBACK_READ_RELEASE": str(
                    promotion_release
                ),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ingress_release = self.temp / "ingress-lock.release"
        ingress_locked = self.temp / "ingress-lock.marker"
        lock_holder: subprocess.Popen[str] | None = None
        hook_results: list[subprocess.CompletedProcess[bytes]] = []
        hook_errors: list[BaseException] = []

        def launch_late_ingress() -> None:
            try:
                hook_results.append(
                    self.run_audncode_hook(
                        {
                            "hook_event_name": "PostToolUse",
                            "session_id": session_id,
                            "transcript_path": str(transcript),
                            "cwd": event["cwd"],
                            "tool_name": "Bash",
                            "tool_input": {"command": "echo late ingress"},
                            "tool_response": {"data": {"status": "completed"}},
                            "tool_use_id": "promotion-fallback-race",
                        },
                        host=host,
                    )
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                hook_errors.append(error)

        hook_thread: threading.Thread | None = None
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not promotion_marker.exists():
                time.sleep(0.025)
            self.assertTrue(promotion_marker.exists(), self.state_debug())

            lock_holder = subprocess.Popen(
                [
                    str(WINDOWS_POWERSHELL),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    r"""
$stream = $null
try {
  $stream = [IO.File]::Open(
    $env:CODEX_NTFY_TEST_LOCK_PATH,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None
  )
  [IO.File]::WriteAllText(
    $env:CODEX_NTFY_TEST_LOCK_MARKER,
    'locked',
    (New-Object Text.UTF8Encoding($false))
  )
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
  while (-not (Test-Path -LiteralPath $env:CODEX_NTFY_TEST_LOCK_RELEASE -PathType Leaf) -and
      [DateTimeOffset]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 20
  }
} finally {
  if ($null -ne $stream) { $stream.Dispose() }
}
""",
                ],
                env={
                    **self.env,
                    "CODEX_NTFY_TEST_LOCK_PATH": str(ingress_lock_path),
                    "CODEX_NTFY_TEST_LOCK_MARKER": str(ingress_locked),
                    "CODEX_NTFY_TEST_LOCK_RELEASE": str(ingress_release),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.time() + 15
            while time.time() < deadline and not ingress_locked.exists():
                time.sleep(0.02)
            self.assertTrue(ingress_locked.exists(), self.state_debug())

            hook_thread = threading.Thread(target=launch_late_ingress, daemon=True)
            hook_thread.start()
            fallback_pending = False
            deadline = time.time() + 15
            while time.time() < deadline and not fallback_pending:
                for fallback_path in (self.state / "claude-sessions").glob(
                    "audn-ingress-fallback-*.json"
                ):
                    try:
                        fallback_state = json.loads(
                            fallback_path.read_text(encoding="utf-8-sig")
                        )
                    except (OSError, json.JSONDecodeError):
                        continue
                    if fallback_state.get("kind") == "audncode-ingress-fallback":
                        fallback_pending = len(fallback_state.get("pending", [])) == 1
                        if fallback_pending:
                            break
                if not fallback_pending:
                    time.sleep(0.02)
            self.assertTrue(fallback_pending, self.state_debug())

            # The worker already observed the fallback as clear. It must take
            # that lock again and fail closed without waiting for the ingress
            # guard currently holding the new hook between fallback and host arm.
            promotion_release.write_text("continue", encoding="ascii")
            lifecycle_deferred = False
            deadline = time.time() + 12
            while time.time() < deadline and not lifecycle_deferred:
                try:
                    lifecycle_deferred = (
                        "deferred AudnCode candidate because lifecycle or recovery changed"
                        in (self.state / "notify.log").read_text(encoding="utf-8-sig")
                    )
                except OSError:
                    lifecycle_deferred = False
                if not lifecycle_deferred:
                    time.sleep(0.025)
            self.assertTrue(lifecycle_deferred, self.state_debug())
            self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            # This regression exercises one promotion attempt. Stop the
            # one-shot worker before releasing the synthetic ingress writer;
            # otherwise its internal retry may legitimately promote after that
            # PostToolUse hook has fully completed and all guards are clear.
            worker.terminate()
            worker.communicate(timeout=10)
            ingress_release.write_text("release", encoding="ascii")
        finally:
            promotion_release.write_text("continue", encoding="ascii")
            ingress_release.write_text("release", encoding="ascii")
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)
            if lock_holder is not None:
                try:
                    lock_stdout, lock_stderr = lock_holder.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    lock_holder.terminate()
                    lock_stdout, lock_stderr = lock_holder.communicate(timeout=10)
                self.assertEqual(
                    lock_holder.returncode,
                    0,
                    msg=f"stdout={lock_stdout}\nstderr={lock_stderr}",
                )
            if hook_thread is not None:
                hook_thread.join(timeout=30)
                self.assertFalse(hook_thread.is_alive(), "late ingress hook did not return")
        if hook_errors:
            raise hook_errors[0]
        self.assertEqual(len(hook_results), 1)
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        with self.server.lock:
            self.assertEqual(self.server.payloads, [], self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_concurrent_post_tool_ingress_handoffs_without_gap(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(session_id=session_id, transcript_path=transcript)
        self.run_audncode_hook(self.audncode_prompt_event(event))
        foreground_events = [
            {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "cwd": event["cwd"],
                "tool_name": "Bash",
                "tool_input": {"command": f"echo {index}"},
                "tool_response": {"data": {"status": "completed"}},
                "tool_use_id": f"foreground-{index}",
            }
            for index in range(2)
        ]
        self.run_audncode_hooks_concurrently(foreground_events)
        host = self.audncode_hosts[session_id]
        _guard_path, guard = self.read_audncode_ingress_guard(host[0].pid)
        self.assertFalse(guard["lost"], guard)
        self.assertEqual(guard["pending_tokens"], [], guard)
        self.assertTrue(guard["safe_to_finalize"], guard)

        self.run_audncode_hook(event)
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.run_ok(self.worker_command("powershell"))
        self.assertEqual(len(self.wait_for_payloads(1)), 1, self.state_debug())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_ingress_loss_is_scoped_to_exact_host_lifetime(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        sessions: list[tuple[str, dict]] = []
        for label in ("blocked host", "healthy host"):
            session_id = str(uuid.uuid4())
            transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
            transcript.write_text("", encoding="utf-8")
            event = self.audncode_event(
                session_id=session_id,
                transcript_path=transcript,
                message=f"Final result from {label}.",
            )
            self.run_audncode_hook(self.audncode_prompt_event(event))
            sessions.append((session_id, event))

        blocked_id, blocked_event = sessions[0]
        self.run_audncode_raw_hook(
            b"{not-json",
            expected_event="PostToolUse",
            host=self.audncode_hosts[blocked_id],
        )
        for _session_id, event in sessions:
            self.run_audncode_hook(event)
            self.run_audncode_hook(self.audncode_idle_event(event))

        worker = self.start_worker("powershell")
        try:
            payloads = self.wait_for_payloads(1, timeout=15)
            self.assertEqual(len(payloads), 1, self.state_debug())
            self.assertIn("healthy host", payloads[0]["message"])
            self.assertNotIn("blocked host", payloads[0]["message"])
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_durable_crons_require_exact_delete_while_owner_is_live(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-durable-cron-project"
        cron_dir = project_root / ".claude"
        cron_dir.mkdir(parents=True)
        cron_path = cron_dir / "scheduled_tasks.json"
        cron_ids = ("a1b2c3d4", "b1c2d3e4")
        created_at_by_id = {
            cron_id: int(time.time() * 1000) + index
            for index, cron_id in enumerate(cron_ids)
        }

        def write_cron_file(ids: tuple[str, ...]) -> None:
            cron_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": cron_id,
                                "cron": "*/5 * * * *",
                                "prompt": "Run the scheduled verification",
                                "createdAt": created_at_by_id[cron_id],
                            }
                            for cron_id in ids
                        ]
                    }
                ),
                encoding="utf-8",
            )

        def runtime_cron_ids() -> set[str]:
            runtime_paths = list((self.state / "claude-sessions").glob("audn-cron-*.json"))
            self.assertEqual(len(runtime_paths), 1, self.state_debug())
            runtime = json.loads(runtime_paths[0].read_text(encoding="utf-8-sig"))
            self.assertTrue(runtime["registry_valid"], runtime)
            return {item["id"] for item in runtime["crons"]}

        write_cron_file(cron_ids)
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        cron_lock = self.write_audncode_cron_lock(event)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Schedule two durable checks"))
        for cron_id in cron_ids:
            self.run_audncode_hook(
                self.audncode_cron_tool_event(
                    event,
                    action="create",
                    cron_id=cron_id,
                    durable=True,
                )
            )
        self.assertEqual(runtime_cron_ids(), set(cron_ids))
        self.run_audncode_hook({**event, "last_assistant_message": "Waiting for both durable timers."})
        self.run_audncode_hook(self.audncode_idle_event(event))

        # A one-shot disappears from the file immediately after its work is
        # queued in memory. Even a later arbitrary prompt and a delayed queue
        # log cannot identify which exact cron fired.
        write_cron_file((cron_ids[1],))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            self.assertEqual(runtime_cron_ids(), set(cron_ids))
            self.append_audncode_queue_operation(event, "enqueue", content="Delayed cron work")
            time.sleep(0.5)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
            self.append_audncode_queue_operation(event, "dequeue")
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="An unrelated next prompt"))
        self.assertEqual(runtime_cron_ids(), set(cron_ids))
        first_delete = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_ids[0]
        )
        self.append_audncode_cron_tool_proof(event, first_delete)
        self.run_audncode_hook(first_delete)
        # CronDelete reports the requested ID even when the scheduler already
        # fired and removed it. It is diagnostic only, never terminal proof.
        self.assertEqual(runtime_cron_ids(), set(cron_ids))
        self.run_audncode_hook({**event, "last_assistant_message": "Only one timer was cancelled."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        blocked_worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if blocked_worker.poll() is None:
                blocked_worker.terminate()
            blocked_worker.communicate(timeout=10)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Cancel the exact remaining timer"))
        write_cron_file(())
        second_delete = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_ids[1]
        )
        self.append_audncode_cron_tool_proof(event, second_delete)
        self.run_audncode_hook(second_delete)
        self.assertEqual(runtime_cron_ids(), set(cron_ids))
        self.run_audncode_hook({**event, "last_assistant_message": "Both durable timers were cancelled."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        live_worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if live_worker.poll() is None:
                live_worker.terminate()
            live_worker.communicate(timeout=10)

        self.exit_audncode_host(session_id)
        cron_lock.unlink(missing_ok=True)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Both durable timers were cancelled", payloads[0]["message"])
        self.assertNotIn("Only one timer", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_exited_observer_waits_for_exact_native_cron_owner(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        project_root = self.temp / "audncode-cross-host-cron-owner"
        cron_directory = project_root / ".claude"
        cron_directory.mkdir(parents=True)
        cron_path = cron_directory / "scheduled_tasks.json"
        cron_id = "c0ffee12"
        cron_path.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": cron_id,
                            "cron": "*/5 * * * *",
                            "prompt": "Run owner-bound work",
                            "createdAt": int(time.time() * 1000),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        observer_id = str(uuid.uuid4())
        observer_transcript = self.audncode_home / "projects" / f"{observer_id}.jsonl"
        observer_transcript.write_text("", encoding="utf-8")
        observer = self.audncode_event(
            session_id=observer_id,
            transcript_path=observer_transcript,
            project_root=project_root,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(observer, prompt="Observe before scheduler lock")
        )
        observer_pid = self.audncode_hosts[observer_id][0].pid
        runtime_path = next(
            path
            for path in (self.state / "claude-sessions").glob("audn-cron-*.json")
            if json.loads(path.read_text(encoding="utf-8-sig")).get("host_pid")
            == observer_pid
        )
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        pending = next(item for item in runtime["crons"] if item["id"] == cron_id)
        self.assertEqual(pending["scheduler_owner_state"], "pending", pending)

        owner_id = str(uuid.uuid4())
        owner_transcript = self.audncode_home / "projects" / f"{owner_id}.jsonl"
        owner_transcript.write_text("", encoding="utf-8")
        owner = self.audncode_event(
            session_id=owner_id,
            transcript_path=owner_transcript,
            project_root=project_root,
        )
        owner_pid = self.audncode_hosts[owner_id][0].pid
        cron_lock = self.write_audncode_cron_lock(
            owner,
            owner_session_id=owner_id,
            owner_pid=owner_pid,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(observer, prompt="Bind the exact scheduler owner")
        )
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        bound = next(item for item in runtime["crons"] if item["id"] == cron_id)
        self.assertEqual(bound["scheduler_owner_state"], "bound", bound)
        self.assertEqual(bound["scheduler_owner_pid"], owner_pid, bound)

        self.run_audncode_hook(
            {**observer, "last_assistant_message": "Observer finished; owner is still working."}
        )
        self.run_audncode_hook(self.audncode_idle_event(observer))
        cron_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        self.exit_audncode_host(observer_id)

        blocked_worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if blocked_worker.poll() is None:
                blocked_worker.terminate()
            blocked_worker.communicate(timeout=10)

        self.exit_audncode_host(owner_id)
        cron_lock.unlink(missing_ok=True)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("owner is still working", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_native_cron_lock_may_be_acquired_long_after_process_start(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        old_process_script = r"""
$cutoff = [DateTime]::UtcNow.AddMinutes(-3)
$candidate = Get-Process | ForEach-Object {
  try {
    $started = $_.StartTime.ToUniversalTime()
    if ($_.Id -gt 4 -and $started -lt $cutoff) {
      [pscustomobject]@{
        pid = [int]$_.Id
        process_started_unix_ms = ([DateTimeOffset]$started).ToUnixTimeMilliseconds()
      }
    }
  } catch { }
} | Sort-Object process_started_unix_ms | Select-Object -First 1
if ($null -ne $candidate) { $candidate | ConvertTo-Json -Compress }
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                old_process_script,
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if not result.stdout.strip():
            self.skipTest("no stable process older than three minutes")
        old_owner = json.loads(result.stdout.strip().splitlines()[-1])

        project_root = self.temp / "audncode-late-native-cron-lock"
        cron_directory = project_root / ".claude"
        cron_directory.mkdir(parents=True)
        cron_id = "fa11bac0"
        (cron_directory / "scheduled_tasks.json").write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": cron_id,
                            "cron": "*/5 * * * *",
                            "prompt": "Bind a late scheduler lease",
                            "createdAt": int(time.time() * 1000),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        owner_session_id = str(uuid.uuid4())
        owner_marker = self.audncode_home / "sessions" / f"{old_owner['pid']}.json"
        owner_marker.write_text(
            json.dumps(
                {
                    "pid": old_owner["pid"],
                    "sessionId": owner_session_id,
                    "cwd": str(project_root),
                    "startedAt": old_owner["process_started_unix_ms"] + 1000,
                    "kind": "interactive",
                    "entrypoint": "cli",
                }
            ),
            encoding="utf-8",
        )
        acquired_at = int(time.time() * 1000)
        self.assertGreater(
            acquired_at - old_owner["process_started_unix_ms"],
            120_000,
        )
        self.write_audncode_cron_lock(
            event,
            owner_session_id=owner_session_id,
            owner_pid=old_owner["pid"],
            acquired_at=acquired_at,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Accept a late but causal lease")
        )
        runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(runtime["registry_valid"], runtime)
        cron = next(item for item in runtime["crons"] if item["id"] == cron_id)
        self.assertEqual(cron["scheduler_owner_state"], "bound", cron)
        self.assertEqual(cron["scheduler_owner_pid"], old_owner["pid"], cron)
        owner_marker.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cron_create_accepts_defaults_and_durable_kill_switch_downgrade(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=self.temp / "audncode-cron-effective-outcome",
        )
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Create effective crons"))

        defaults = self.audncode_cron_tool_event(
            event,
            action="create",
            cron_id="defa0175",
            recurring=True,
            durable=False,
        )
        del defaults["tool_input"]["recurring"]
        del defaults["tool_input"]["durable"]
        self.run_audncode_hook(defaults)

        downgraded = self.audncode_cron_tool_event(
            event,
            action="create",
            cron_id="d0a0fade",
            recurring=False,
            durable=False,
        )
        downgraded["tool_input"]["durable"] = True
        self.run_audncode_hook(downgraded)

        runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(runtime["registry_valid"], runtime)
        crons = {item["id"]: item for item in runtime["crons"]}
        self.assertEqual(set(crons), {"defa0175", "d0a0fade"}, runtime)
        self.assertTrue(crons["defa0175"]["recurring"], crons["defa0175"])
        self.assertFalse(crons["d0a0fade"]["durable"], crons["d0a0fade"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_durable_cron_id_reuse_stays_sticky_ambiguous(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-cron-id-reuse"
        cron_directory = project_root / ".claude"
        cron_directory.mkdir(parents=True)
        cron_path = cron_directory / "scheduled_tasks.json"
        cron_id = "decafbad"

        def write_incarnation(prompt: str, created_at: int) -> None:
            cron_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": cron_id,
                                "cron": "*/5 * * * *",
                                "prompt": prompt,
                                "createdAt": created_at,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

        created_at = int(time.time() * 1000)
        write_incarnation("First incarnation", created_at)
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        cron_lock = self.write_audncode_cron_lock(event)
        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Track the first incarnation"))
        self.run_audncode_hook(
            {**event, "last_assistant_message": "A reused cron ID is not final."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))

        cron_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        first_worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if first_worker.poll() is None:
                first_worker.terminate()
            first_worker.communicate(timeout=10)

        write_incarnation("Second incarnation", created_at + 1)
        self.run_audncode_hook(
            self.audncode_prompt_event(event, prompt="Observe the reused upstream ID")
        )
        runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertFalse(runtime["registry_valid"], runtime)

        cron_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        delete_event = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_id
        )
        self.append_audncode_cron_tool_proof(event, delete_event)
        self.run_audncode_hook(delete_event)
        self.run_audncode_hook(
            {**event, "last_assistant_message": "Delete claim cannot heal ID reuse."}
        )
        self.run_audncode_hook(self.audncode_idle_event(event))
        self.exit_audncode_host(session_id)
        cron_lock.unlink(missing_ok=True)
        blocked_worker = self.start_worker("powershell")
        try:
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if blocked_worker.poll() is None:
                blocked_worker.terminate()
            blocked_worker.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_durable_cron_successor_boundaries_are_causal(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        successor_hosts: list[tuple[str, Path]] = []

        def prepare_successor(label: str, *, mutate_after_successor_start: bool) -> None:
            owner_id = str(uuid.uuid4())
            owner_transcript = self.audncode_home / "projects" / f"{owner_id}.jsonl"
            owner_transcript.write_text("", encoding="utf-8")
            project_root = self.temp / f"audncode-successor-{label}"
            cron_directory = project_root / ".claude"
            cron_directory.mkdir(parents=True)
            cron_path = cron_directory / "scheduled_tasks.json"
            cron_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "a11ce123",
                                "cron": "*/5 * * * *",
                                "prompt": f"Run {label} successor work",
                                "createdAt": int(time.time() * 1000),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            owner = self.audncode_event(
                session_id=owner_id,
                transcript_path=owner_transcript,
                project_root=project_root,
            )
            owner_lock = self.write_audncode_cron_lock(owner)
            self.run_audncode_hook(
                self.audncode_prompt_event(owner, prompt=f"Prepare {label} boundary")
            )
            self.run_audncode_hook(
                {**owner, "last_assistant_message": f"{label} successor boundary result."}
            )
            self.run_audncode_hook(self.audncode_idle_event(owner))
            cron_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
            self.exit_audncode_host(owner_id)
            owner_lock.unlink(missing_ok=True)

            if not mutate_after_successor_start:
                old = time.time() - 60
                os.utime(cron_path, (old, old))
            successor_id = str(uuid.uuid4())
            successor_transcript = self.audncode_home / "projects" / f"{successor_id}.jsonl"
            successor_transcript.write_text("", encoding="utf-8")
            successor = self.audncode_event(
                session_id=successor_id,
                transcript_path=successor_transcript,
                project_root=project_root,
            )
            if mutate_after_successor_start:
                time.sleep(0.02)
                cron_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
            successor_lock = self.write_audncode_cron_lock(successor)
            successor_hosts.append((successor_id, successor_lock))

        prepare_successor("safe", mutate_after_successor_start=False)
        prepare_successor("unsafe", mutate_after_successor_start=True)

        # A passive window in the same project is not the O_EXCL lease owner.
        # An unchanged, old empty file is still a clean no-cron baseline.
        passive_project = self.temp / "audncode-passive-clean-cron-owner"
        passive_cron_directory = passive_project / ".claude"
        passive_cron_directory.mkdir(parents=True)
        passive_cron_path = passive_cron_directory / "scheduled_tasks.json"
        passive_cron_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        passive_old = time.time() - 60
        os.utime(passive_cron_path, (passive_old, passive_old))
        lease_id = str(uuid.uuid4())
        lease_transcript = self.audncode_home / "projects" / f"{lease_id}.jsonl"
        lease_transcript.write_text("", encoding="utf-8")
        lease = self.audncode_event(
            session_id=lease_id,
            transcript_path=lease_transcript,
            project_root=passive_project,
        )
        lease_lock = self.write_audncode_cron_lock(lease)
        passive_id = str(uuid.uuid4())
        passive_transcript = self.audncode_home / "projects" / f"{passive_id}.jsonl"
        passive_transcript.write_text("", encoding="utf-8")
        passive = self.audncode_event(
            session_id=passive_id,
            transcript_path=passive_transcript,
            project_root=passive_project,
        )
        self.run_audncode_hook(
            self.audncode_prompt_event(passive, prompt="Passive host with clean project")
        )
        self.run_audncode_hook(
            {**passive, "last_assistant_message": "Passive host clean result."}
        )
        self.run_audncode_hook(self.audncode_idle_event(passive))

        worker = subprocess.Popen(
            self.continuous_worker_command("powershell"),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            payloads = self.wait_for_payloads(2, timeout=60)
            self.assertEqual(len(payloads), 2, self.state_debug())
            messages = [payload["message"] for payload in payloads]
            self.assertTrue(any("safe successor" in message for message in messages), messages)
            self.assertTrue(any("Passive host clean" in message for message in messages), messages)
            self.assertFalse(any("unsafe successor" in message for message in messages), messages)
            time.sleep(2.0)
            with self.server.lock:
                self.assertEqual(len(self.server.payloads), 2, self.state_debug())
        finally:
            self.stop_continuous_worker(worker)
            for successor_id, successor_lock in successor_hosts:
                if successor_id in self.audncode_hosts:
                    self.exit_audncode_host(successor_id)
                successor_lock.unlink(missing_ok=True)
            if passive_id in self.audncode_hosts:
                self.exit_audncode_host(passive_id)
            if lease_id in self.audncode_hosts:
                self.exit_audncode_host(lease_id)
            lease_lock.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_session_start_imports_preexisting_durable_cron(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-preexisting-durable-project"
        cron_directory = project_root / ".claude"
        cron_directory.mkdir(parents=True)
        cron_path = cron_directory / "scheduled_tasks.json"
        cron_id = "c1d2e3f4"
        cron_path.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": cron_id,
                            "cron": "*/5 * * * *",
                            "prompt": "Run preexisting durable work",
                            "createdAt": int(time.time() * 1000),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        event = self.audncode_event(
            session_id=session_id,
            transcript_path=transcript,
            project_root=project_root,
        )
        cron_lock = self.write_audncode_cron_lock(event)
        self.run_audncode_hook(self.audncode_session_start_event(event, source="startup"))
        runtime_path = next((self.state / "claude-sessions").glob("audn-cron-*.json"))
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertEqual({item["id"] for item in runtime["crons"]}, {cron_id}, runtime)

        cron_path.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        self.run_audncode_hook({**event, "last_assistant_message": "Preexisting cron fired."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

        self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Unrelated prompt cannot clear cron identity"))
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertEqual({item["id"] for item in runtime["crons"]}, {cron_id}, runtime)
        delete_event = self.audncode_cron_tool_event(
            event, action="delete", cron_id=cron_id
        )
        self.append_audncode_cron_tool_proof(event, delete_event)
        self.run_audncode_hook(delete_event)
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        self.assertEqual({item["id"] for item in runtime["crons"]}, {cron_id}, runtime)
        self.run_audncode_hook({**event, "last_assistant_message": "Exact durable cron was deleted."})
        self.run_audncode_hook(self.audncode_idle_event(event))
        live_worker = self.start_worker("powershell")
        try:
            time.sleep(1.8)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if live_worker.poll() is None:
                live_worker.terminate()
            live_worker.communicate(timeout=10)

        self.exit_audncode_host(session_id)
        cron_lock.unlink(missing_ok=True)
        self.run_ok(self.worker_command("powershell"), timeout=90)
        payloads = self.wait_for_payloads(1)
        self.assertEqual(len(payloads), 1, self.state_debug())
        self.assertIn("Exact durable cron was deleted", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cron_directory_junction_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-cron-junction-project"
        project_root.mkdir()
        outside_cron = self.temp / "outside-audn-cron-directory"
        outside_cron.mkdir()
        (outside_cron / "scheduled_tasks.json").write_text(
            json.dumps({"tasks": []}), encoding="utf-8"
        )
        junction = project_root / ".claude"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside_cron)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(f"directory junction unavailable: {created.stdout}{created.stderr}")
        try:
            event = self.audncode_event(
                session_id=session_id,
                transcript_path=transcript,
                project_root=project_root,
            )
            self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Reject cron junction"))
            self.run_audncode_hook({**event, "last_assistant_message": "Unsafe cron directory."})
            self.run_audncode_hook(self.audncode_idle_event(event))
            worker = self.start_worker("powershell")
            try:
                time.sleep(1.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)
        finally:
            os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cron_file_symlink_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)
        session_id = str(uuid.uuid4())
        transcript = self.audncode_home / "projects" / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        project_root = self.temp / "audncode-cron-symlink-project"
        cron_directory = project_root / ".claude"
        cron_directory.mkdir(parents=True)
        outside_file = self.temp / "outside-scheduled-tasks.json"
        outside_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        cron_path = cron_directory / "scheduled_tasks.json"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", str(cron_path), str(outside_file)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(f"file symlink unavailable: {created.stdout}{created.stderr}")
        try:
            event = self.audncode_event(
                session_id=session_id,
                transcript_path=transcript,
                project_root=project_root,
            )
            self.run_audncode_hook(self.audncode_prompt_event(event, prompt="Reject cron symlink"))
            self.run_audncode_hook({**event, "last_assistant_message": "Unsafe cron file."})
            self.run_audncode_hook(self.audncode_idle_event(event))
            worker = self.start_worker("powershell")
            try:
                time.sleep(1.8)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [], self.state_debug())
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)
        finally:
            cron_path.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_cron_missing_or_malformed_evidence_fails_closed(self) -> None:
        self.configure(idle_detection_mode="strict", idle_grace_seconds=0)

        malformed_id = str(uuid.uuid4())
        malformed_transcript = self.audncode_home / "projects" / f"{malformed_id}.jsonl"
        malformed_transcript.write_text("", encoding="utf-8")
        malformed = self.audncode_event(
            session_id=malformed_id,
            transcript_path=malformed_transcript,
            project_root=self.temp / "audncode-malformed-cron-project",
        )
        self.run_audncode_hook(self.audncode_prompt_event(malformed))
        malformed_create = self.audncode_cron_tool_event(
            malformed, action="create", cron_id="c1d2e3f4"
        )
        del malformed_create["tool_response"]["data"]["durable"]
        self.run_audncode_hook(malformed_create)
        self.run_audncode_hook({**malformed, "last_assistant_message": "Malformed output."})
        self.run_audncode_hook(self.audncode_idle_event(malformed))

        missing_id = str(uuid.uuid4())
        missing_transcript = self.audncode_home / "projects" / f"{missing_id}.jsonl"
        missing_transcript.write_text("", encoding="utf-8")
        missing = self.audncode_event(
            session_id=missing_id,
            transcript_path=missing_transcript,
            project_root=self.temp / "audncode-missing-cron-project",
        )
        self.run_audncode_hook(self.audncode_prompt_event(missing))
        self.run_audncode_hook(
            self.audncode_cron_tool_event(
                missing,
                action="create",
                cron_id="d1e2f3a4",
                durable=True,
            )
        )
        self.run_audncode_hook({**missing, "last_assistant_message": "Missing durable file."})
        self.run_audncode_hook(self.audncode_idle_event(missing))

        invalid_file_id = str(uuid.uuid4())
        invalid_file_transcript = self.audncode_home / "projects" / f"{invalid_file_id}.jsonl"
        invalid_file_transcript.write_text("", encoding="utf-8")
        invalid_project = self.temp / "audncode-invalid-cron-file-project"
        (invalid_project / ".claude").mkdir(parents=True)
        (invalid_project / ".claude" / "scheduled_tasks.json").write_text(
            json.dumps({"unexpected": []}), encoding="utf-8"
        )
        invalid_file = self.audncode_event(
            session_id=invalid_file_id,
            transcript_path=invalid_file_transcript,
            project_root=invalid_project,
        )
        self.run_audncode_hook(self.audncode_prompt_event(invalid_file))
        self.run_audncode_hook({**invalid_file, "last_assistant_message": "Invalid durable file."})
        self.run_audncode_hook(self.audncode_idle_event(invalid_file))

        invalid_utf8_id = str(uuid.uuid4())
        invalid_utf8_transcript = self.audncode_home / "projects" / f"{invalid_utf8_id}.jsonl"
        invalid_utf8_transcript.write_text("", encoding="utf-8")
        invalid_utf8_project = self.temp / "audncode-invalid-utf8-cron-project"
        invalid_utf8_directory = invalid_utf8_project / ".claude"
        invalid_utf8_directory.mkdir(parents=True)
        (invalid_utf8_directory / "scheduled_tasks.json").write_bytes(b'{"tasks":[]}\xff')
        invalid_utf8 = self.audncode_event(
            session_id=invalid_utf8_id,
            transcript_path=invalid_utf8_transcript,
            project_root=invalid_utf8_project,
        )
        self.run_audncode_hook(self.audncode_prompt_event(invalid_utf8))
        self.run_audncode_hook({**invalid_utf8, "last_assistant_message": "Invalid UTF-8 cron file."})
        self.run_audncode_hook(self.audncode_idle_event(invalid_utf8))

        oversized_id = str(uuid.uuid4())
        oversized_transcript = self.audncode_home / "projects" / f"{oversized_id}.jsonl"
        oversized_transcript.write_text("", encoding="utf-8")
        oversized_project = self.temp / "audncode-oversized-cron-project"
        oversized_directory = oversized_project / ".claude"
        oversized_directory.mkdir(parents=True)
        oversized_path = oversized_directory / "scheduled_tasks.json"
        with oversized_path.open("wb") as stream:
            stream.seek(16 * 1024 * 1024)
            stream.write(b"x")
        oversized = self.audncode_event(
            session_id=oversized_id,
            transcript_path=oversized_transcript,
            project_root=oversized_project,
        )
        self.run_audncode_hook(self.audncode_prompt_event(oversized))
        self.run_audncode_hook({**oversized, "last_assistant_message": "Oversized cron file."})
        self.run_audncode_hook(self.audncode_idle_event(oversized))

        worker = self.start_worker("powershell")
        try:
            time.sleep(2.5)
            with self.server.lock:
                self.assertEqual(self.server.payloads, [], self.state_debug())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=10)

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
                self.run_ok(self.worker_command(implementation), timeout=60)

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

    def test_watcher_sparse_gigabyte_replay_is_io_bounded(self) -> None:
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_initial_replay_seconds=60,
            suppress_subagents=False,
        )
        logical_size = 1 << 30
        cap = 512
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0010-7000-8000-000000000001"
                rollout = self.write_session_meta(thread_id, subagent=False)
                tail = b"\n" + b"".join(
                    (
                        (
                            json.dumps(
                                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}
                            )
                            + "\n"
                        ).encode(),
                        (
                            json.dumps(
                                {
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "task_complete",
                                        "turn_id": turn_id,
                                        "last_agent_message": "SPARSE FINAL",
                                    },
                                }
                            )
                            + "\n"
                        ).encode(),
                    )
                )
                self.assertLess(len(tail), cap)
                with rollout.open("r+b") as handle:
                    handle.seek(logical_size - len(tail))
                    handle.write(tail)
                self.assertEqual(rollout.stat().st_size, logical_size)
                scan_env = self.env.copy()
                scan_env["CODEX_NTFY_TEST_WATCH_MAX_BYTES"] = str(cap)
                result = subprocess.run(
                    self.rollout_scan_command(implementation),
                    env=scan_env,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                health = json.loads((self.state / "watch-health.json").read_text(encoding="utf-8-sig"))
                self.assertEqual(health.get("status"), "completed")
                self.assertLessEqual(int(health.get("bytes_read", 0)), cap * 2)
                self.assertEqual(int(health.get("truncated_replays", 0)), 1)
                self.assertEqual(len(list((self.state / "outbox").glob("*.json"))), 1)
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_watcher_backlog_never_promotes_a_terminal_before_caught_up(self) -> None:
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_initial_replay_seconds=0,
            suppress_subagents=False,
        )
        cap = 512
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                first_turn = "00000000-0011-7000-8000-000000000001"
                second_turn = "00000000-0011-7000-8000-000000000002"
                rollout = self.write_session_meta(thread_id, subagent=False)
                scan_env = {**self.env, "CODEX_NTFY_TEST_WATCH_MAX_BYTES": str(cap)}

                baseline = subprocess.run(
                    self.rollout_scan_command(implementation), env=scan_env, text=True, capture_output=True, timeout=60
                )
                self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
                self.append_rollout(rollout, "task_started", turn_id=first_turn)
                self.append_rollout(rollout, "task_complete", turn_id=first_turn, message="INTERMEDIATE")
                self.append_rollout(rollout, "user_message", message="x" * 430)
                self.append_rollout(rollout, "task_started", turn_id=second_turn)

                caught_up = False
                staged_seen = False
                for _ in range(10):
                    result = subprocess.run(
                        self.rollout_scan_command(implementation),
                        env=scan_env,
                        text=True,
                        capture_output=True,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(list((self.state / "pending").glob("*.json")))
                    self.assertFalse(list((self.state / "outbox").glob("*.json")))
                    cursor_path = next((self.state / "watch").glob("*.json"))
                    cursor = json.loads(cursor_path.read_text(encoding="utf-8-sig"))
                    staged_seen = staged_seen or cursor.get("staged_type") == "task_complete"
                    if int(cursor.get("offset", 0)) == rollout.stat().st_size:
                        caught_up = True
                        break
                self.assertTrue(staged_seen)
                self.assertTrue(caught_up)
                self.assertEqual(cursor.get("staged_type"), "")

                self.append_rollout(rollout, "task_complete", turn_id=second_turn, message="ACTUAL FINAL")
                result = subprocess.run(
                    self.rollout_scan_command(implementation), env=scan_env, text=True, capture_output=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                records = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "outbox").glob("*.json")
                ]
                self.assertEqual([record.get("turn_id") for record in records], [second_turn])
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_watcher_partial_terminal_is_promoted_once_after_newline(self) -> None:
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_initial_replay_seconds=0,
            suppress_subagents=False,
        )
        cap = 512
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                turn_id = "00000000-0012-7000-8000-000000000001"
                rollout = self.write_session_meta(thread_id, subagent=False)
                scan_env = {**self.env, "CODEX_NTFY_TEST_WATCH_MAX_BYTES": str(cap)}
                baseline = subprocess.run(
                    self.rollout_scan_command(implementation), env=scan_env, text=True, capture_output=True, timeout=60
                )
                self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
                self.append_rollout(rollout, "task_started", turn_id=turn_id)
                terminal = json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": turn_id,
                            "last_agent_message": "PARTIAL FINAL",
                        },
                    }
                ).encode()
                with rollout.open("ab") as handle:
                    handle.write(terminal)
                first = subprocess.run(
                    self.rollout_scan_command(implementation), env=scan_env, text=True, capture_output=True, timeout=60
                )
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                cursor_path = next((self.state / "watch").glob("*.json"))
                cursor = json.loads(cursor_path.read_text(encoding="utf-8-sig"))
                self.assertTrue(cursor.get("incomplete_tail"))
                with rollout.open("ab") as handle:
                    handle.write(b"\n")
                for _ in range(2):
                    result = subprocess.run(
                        self.rollout_scan_command(implementation),
                        env=scan_env,
                        text=True,
                        capture_output=True,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(list((self.state / "outbox").glob("*.json"))), 1)
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_watcher_oversize_line_is_sticky_until_a_new_epoch(self) -> None:
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_initial_replay_seconds=0,
            suppress_subagents=False,
        )
        cap = 512
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                first_turn = "00000000-0013-7000-8000-000000000001"
                second_turn = "00000000-0013-7000-8000-000000000002"
                rollout = self.write_session_meta(thread_id, subagent=False)
                scan_env = {**self.env, "CODEX_NTFY_TEST_WATCH_MAX_BYTES": str(cap)}
                baseline = subprocess.run(
                    self.rollout_scan_command(implementation), env=scan_env, text=True, capture_output=True, timeout=60
                )
                self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
                self.append_rollout(rollout, "task_started", turn_id=first_turn)
                oversized = (
                    b'{"type":"event_msg","payload":{"type":"tool_output","blob":"'
                    + b"z" * (cap * 4)
                    + b'"}}\n'
                )
                terminal = (
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "turn_id": first_turn,
                                "last_agent_message": "MUST NOT PROMOTE",
                            },
                        }
                    )
                    + "\n"
                ).encode()
                with rollout.open("ab") as handle:
                    handle.write(oversized + terminal)
                offsets: list[int] = []
                for _ in range(12):
                    result = subprocess.run(
                        self.rollout_scan_command(implementation),
                        env=scan_env,
                        text=True,
                        capture_output=True,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    cursor_path = next((self.state / "watch").glob("*.json"))
                    cursor = json.loads(cursor_path.read_text(encoding="utf-8-sig"))
                    offsets.append(int(cursor.get("offset", 0)))
                    self.assertFalse(list((self.state / "outbox").glob("*.json")))
                    if offsets[-1] == rollout.stat().st_size and not cursor.get("discard_mode"):
                        break
                self.assertEqual(offsets, sorted(offsets))
                self.assertGreater(len(set(offsets)), 2)
                self.assertEqual(offsets[-1], rollout.stat().st_size)
                self.assertTrue(cursor.get("corrupt_epoch"))

                self.append_rollout(rollout, "task_started", turn_id=second_turn)
                self.append_rollout(rollout, "task_complete", turn_id=second_turn, message="RECOVERED FINAL")
                result = subprocess.run(
                    self.rollout_scan_command(implementation), env=scan_env, text=True, capture_output=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                records = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "outbox").glob("*.json")
                ]
                self.assertEqual([record.get("turn_id") for record in records], [second_turn])
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_watcher_unicode_escaped_start_invalidates_staged_terminal(self) -> None:
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_initial_replay_seconds=60,
            suppress_subagents=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                completed_turn = "00000000-0014-7000-8000-000000000001"
                open_turn = "00000000-0014-7000-8000-000000000002"
                rollout = self.write_session_meta(thread_id, subagent=False)
                self.append_rollout(rollout, "task_started", turn_id=completed_turn)
                self.append_rollout(rollout, "task_complete", turn_id=completed_turn, message="INTERMEDIATE")
                with rollout.open("a", encoding="utf-8") as handle:
                    handle.write(
                        '{"type":"event_msg","payload":{"type":"task_\\u0073tarted","turn_id":"'
                        + open_turn
                        + '"}}\n'
                    )
                result = subprocess.run(
                    self.rollout_scan_command(implementation),
                    env=self.env,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
                self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_event_classification_has_only_bounded_exact_fallbacks(self) -> None:
        powershell = POWERSHELL_NOTIFIER.read_text(encoding="utf-8-sig")
        ps_classifier = powershell.split("function Get-EventClassification {", 1)[1].split(
            "function Get-RawNotification {", 1
        )[0]
        self.assertNotIn("-Recurse", ps_classifier)
        python = PYTHON_NOTIFIER.read_text(encoding="utf-8")
        py_classifier = python.split("def event_classification(", 1)[1].split("\ndef parse_event(", 1)[0]
        self.assertNotIn(".rglob(", py_classifier)
        self.assertIn("bounded_session_index_contains", py_classifier)

    def test_python_exact_rollout_path_is_contained_and_identity_bound(self) -> None:
        spec = importlib.util.spec_from_file_location("codex_ntfy_rollout_path_under_test", PYTHON_NOTIFIER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        thread_id = str(uuid.uuid4())
        valid = self.write_session_meta(thread_id, subagent=False)
        self.assertEqual(
            module.exact_rollout_path(str(valid), self.codex_home, thread_id),
            valid.resolve(),
        )
        relative = valid.relative_to(self.codex_home).as_posix()
        self.assertEqual(
            module.exact_rollout_path(f"/home/test/.codex/{relative}", self.codex_home, thread_id),
            valid.resolve(),
        )

        outside = self.temp / "outside-rollout.jsonl"
        outside.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": thread_id, "source": "vscode"}}) + "\n",
            encoding="utf-8",
        )
        self.assertIsNone(module.exact_rollout_path(str(outside), self.codex_home, thread_id))

        mismatched = valid.with_name(f"rollout-mismatch-{thread_id}.jsonl")
        mismatched.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": str(uuid.uuid4()), "source": "vscode"}})
            + "\n",
            encoding="utf-8",
        )
        self.assertIsNone(module.exact_rollout_path(str(mismatched), self.codex_home, thread_id))

    def test_event_classification_session_index_fallback_fails_closed(self) -> None:
        index = self.codex_home / "session_index.jsonl"
        cases = (
            ("valid", lambda thread_id: (json.dumps({"id": thread_id, "thread_name": "Root task"}) + "\n").encode(), "root"),
            ("oversized", lambda _thread_id: b" " * (4 * 1024 * 1024 + 1), "unknown"),
            ("invalid-utf8", lambda _thread_id: b"\xff\n", "unknown"),
        )
        for implementation in self.implementations():
            for case_name, content, expected in cases:
                with self.subTest(implementation=implementation, case=case_name):
                    thread_id = str(uuid.uuid4())
                    index.write_bytes(content(thread_id))
                    self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                    records = [
                        json.loads(path.read_text(encoding="utf-8-sig"))
                        for path in (self.state / "outbox").glob("*.json")
                    ]
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0].get("session_classification"), expected)
                    shutil.rmtree(self.state, ignore_errors=True)

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
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline and not list((self.state / "watch").glob("*.json")):
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            self.fail(
                                "continuous worker exited before creating a rollout cursor: "
                                f"returncode={process.returncode}\nstdout={stdout}\nstderr={stderr}"
                            )
                        time.sleep(0.05)
                    self.assertTrue(list((self.state / "watch").glob("*.json")))
                    self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="Recovered final")
                    self.assertEqual(len(self.wait_for_payloads(1, timeout=60)), 1)
                    time.sleep(0.2)
                    with self.server.lock:
                        self.assertEqual(len(self.server.payloads), 1)
                finally:
                    stdout, stderr = self.stop_continuous_worker(process)
                    self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)
                with self.server.lock:
                    self.server.payloads.clear()

    def test_continuous_worker_delivers_while_rollout_scan_is_busy(self) -> None:
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_scan_seconds=60,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                worker_env = self.env.copy()
                worker_env["CODEX_NTFY_TEST_SCAN_DELAY_MS"] = "30000"
                worker_env["CODEX_NTFY_SCAN_ONCE"] = "1"
                process = subprocess.Popen(
                    self.continuous_worker_command(implementation),
                    env=worker_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                health_path = self.state / "watch-health.json"
                try:
                    deadline = time.monotonic() + 15
                    health: dict[str, object] = {}
                    while time.monotonic() < deadline:
                        try:
                            health = json.loads(health_path.read_text(encoding="utf-8-sig"))
                        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                            health = {}
                        if health.get("status") == "running":
                            break
                        time.sleep(0.05)
                    self.assertEqual(health.get("status"), "running")

                    self.run_ok(self.hook_command(implementation, self.event()))
                    started = time.monotonic()
                    payloads = self.wait_for_payloads(1, timeout=8)
                    self.assertEqual(len(payloads), 1)
                    self.assertLess(time.monotonic() - started, 8)
                    deadline = time.monotonic() + 1
                    while True:
                        try:
                            health = json.loads(health_path.read_text(encoding="utf-8-sig"))
                            break
                        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.05)
                    self.assertEqual(health.get("status"), "running")

                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        try:
                            health = json.loads(health_path.read_text(encoding="utf-8-sig"))
                        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                            time.sleep(0.05)
                            continue
                        if health.get("status") != "running":
                            break
                        time.sleep(0.1)
                    self.assertEqual(health.get("status"), "completed")
                finally:
                    stdout, stderr = self.stop_continuous_worker(process)
                    self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")
                shutil.rmtree(self.state, ignore_errors=True)
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
                    self.assertTrue(
                        all(int(state.get("offset", 0) or 0) == 0 for state in cursor_states),
                        msg=f"unexpected cursor state before metadata recovery: {cursor_states}",
                    )

                    metadata = json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"id": thread_id, "source": "vscode", "cwd": "/work/metadata-retry"},
                        }
                    )
                    rollout.write_text(metadata + "\n" + lifecycle_tail, encoding="utf-8")
                    payloads = self.wait_for_payloads(1, timeout=30)
                    self.assertEqual(len(payloads), 1)
                    self.assertIn("METADATA RECOVERED", payloads[0]["message"])
                finally:
                    stdout, stderr = self.stop_continuous_worker(process)
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
                    database = sqlite3.connect(self.state_database)
                    try:
                        database.execute(
                            "INSERT OR REPLACE INTO threads(id, rollout_path, source, thread_source, title) "
                            "VALUES (?, ?, 'vscode', 'user', 'Discovery test')",
                            (thread_id, str(rollout)),
                        )
                        database.commit()
                    finally:
                        database.close()
                    process = subprocess.Popen(
                        self.continuous_worker_command(implementation),
                        env=self.env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        payloads = self.wait_for_payloads(1, timeout=30)
                        self.assertEqual(len(payloads), 1)
                        self.assertIn("DISCOVERED", payloads[0]["message"])
                    finally:
                        stdout, stderr = self.stop_continuous_worker(process)
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
            payloads = self.wait_for_payloads(1, timeout=45)
            self.assertEqual(len(payloads), 1)
            self.assertIn("WSL RECOVERED", payloads[0]["message"])
            self.assertIn("WSL:test", payloads[0]["message"])
            self.assertNotIn("Source:", payloads[0]["message"])
        finally:
            stdout, stderr = self.stop_continuous_worker(process)
            self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows remote cursor scan test")
    def test_windows_remote_scan_does_not_share_the_local_cursor_batch(self) -> None:
        thread_id = str(uuid.uuid4())
        turn_id = "00000000-0000-7000-8000-000000000083"
        rollout = self.write_session_meta(thread_id, subagent=False)
        self.append_rollout(rollout, "task_started", turn_id=turn_id)
        self.append_rollout(rollout, "user_message", message="Resumed old WSL task")
        self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="REMOTE CURSOR RECOVERED")

        discovery_seconds = 1_000_000
        global_batch_index = int(time.time() * 1000 // (discovery_seconds * 1000)) % 3
        remote_cursor_index = (global_batch_index + 1) % 3
        # Use the loopback administrative share so the cursor is genuinely UNC
        # while still resolving below the exact session home it declares. The
        # old fake WSL home/local-rollout pairing is correctly rejected by
        # Resolve-TrustedRolloutPath and therefore cannot test cursor batching.
        resolved_home = self.codex_home.resolve()
        resolved_rollout = rollout.resolve()
        if len(resolved_home.drive) != 2 or resolved_home.drive[1] != ":":
            self.skipTest("remote cursor test requires a drive-backed temporary directory")
        loopback_root = rf"\\localhost\{resolved_home.drive[0]}$"
        remote_session_home = loopback_root + str(resolved_home)[2:]
        remote_rollout = loopback_root + str(resolved_rollout)[2:]
        if not Path(remote_rollout).is_file():
            self.skipTest("Windows loopback administrative share is unavailable")
        watch = self.state / "watch"
        watch.mkdir(parents=True)
        for index in range(3):
            if index == remote_cursor_index:
                cursor = {
                    "schema": 1,
                    "rollout_path": remote_rollout,
                    "session_codex_home": remote_session_home,
                    "session_sqlite_home": str(self.codex_home),
                    "origin": "WSL:test",
                    "offset": 0,
                    "seen_unix_ms": 0,
                }
            else:
                cursor = {
                    "schema": 1,
                    "rollout_path": str(self.temp / f"missing-local-{index}.jsonl"),
                    "session_codex_home": str(self.codex_home),
                    "session_sqlite_home": str(self.codex_home),
                    "origin": "local",
                    "offset": 0,
                    "seen_unix_ms": 0,
                }
            (watch / f"{index:02d}-cursor.json").write_text(json.dumps(cursor), encoding="utf-8")

        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=True,
            watch_discovery_seconds=discovery_seconds,
            watch_cursor_batch_size=1,
            watch_initial_replay_seconds=60,
            suppress_technical_turns=True,
        )
        scan = self.run_ok(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(POWERSHELL_NOTIFIER),
                "-ScanRollouts",
                "-ScanScope",
                "Remote",
            ],
            timeout=60,
        )
        self.assertEqual(scan.stdout.strip(), "")
        self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
        self.run_ok(self.worker_command("powershell"), timeout=60)
        payloads = self.wait_for_payloads(1, timeout=10)
        self.assertEqual(len(payloads), 1)
        self.assertIn("REMOTE CURSOR RECOVERED", payloads[0]["message"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows remote watcher isolation test")
    def test_windows_unavailable_remote_root_cannot_block_local_recovery(self) -> None:
        thread_id = str(uuid.uuid4())
        turn_id = "00000000-0000-7000-8000-000000000082"
        rollout = self.write_session_meta(thread_id, subagent=False)
        self.append_rollout(rollout, "task_started", turn_id=turn_id)
        self.append_rollout(rollout, "user_message", message="Local task beside unavailable WSL")
        self.append_rollout(rollout, "task_complete", turn_id=turn_id, message="LOCAL RECOVERED")
        unavailable = rf"\\127.0.0.1\codex-ntfy-unavailable-{uuid.uuid4().hex}"
        self.configure(
            idle_detection_mode="strict",
            idle_grace_seconds=0,
            goal_poll_seconds=0.05,
            watch_rollouts=True,
            watch_scan_seconds=0.1,
            watch_discovery_seconds=5,
            watch_initial_replay_seconds=60,
            watch_remote_timeout_seconds=5,
            watch_roots=[{"path": unavailable, "sqlite_path": unavailable, "origin": "WSL:unavailable"}],
            suppress_technical_turns=True,
        )
        watch = self.state / "watch"
        watch.mkdir(parents=True, exist_ok=True)
        (watch / "remote-cursor.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "rollout_path": unavailable + r"\sessions\2026\07\13\rollout-missing.jsonl",
                    "session_codex_home": unavailable,
                    "session_sqlite_home": unavailable,
                    "offset": 0,
                }
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            self.continuous_worker_command("powershell"),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            payloads = self.wait_for_payloads(1, timeout=60)
            self.assertEqual(len(payloads), 1)
            self.assertIn("LOCAL RECOVERED", payloads[0]["message"])
        finally:
            stdout, stderr = self.stop_continuous_worker(process)
            self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows remote timeout clock test")
    def test_windows_remote_timeout_starts_after_child_creation(self) -> None:
        unavailable = rf"\\127.0.0.1\codex-ntfy-unavailable-{uuid.uuid4().hex}"
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_scan_seconds=60,
            watch_discovery_seconds=60,
            watch_remote_timeout_seconds=20,
            watch_roots=[{"path": unavailable, "sqlite_path": unavailable, "origin": "remote-test"}],
        )
        worker_env = {
            **self.env,
            "CODEX_NTFY_SCAN_ONCE": "1",
            # Process creation is outside both budgets. Its delay exceeds the
            # whole twenty-second budget (so the old clock fails immediately),
            # while each child phase retains ample headroom under full-suite
            # PowerShell/Defender load.
            "CODEX_NTFY_TEST_REMOTE_SCAN_START_DELAY_MS": "21000",
            "CODEX_NTFY_TEST_SCAN_DELAY_MS": "3000",
            "CODEX_NTFY_TEST_REMOTE_CHILD_STARTUP_DELAY_MS": "3000",
        }
        process = subprocess.Popen(
            self.continuous_worker_command("powershell"),
            env=worker_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        health_path = self.state / "remote-watch-health.json"
        try:
            health = self.wait_for_process_health(
                health_path, process, {"completed", "timed-out", "failed"}, timeout=60
            )
            self.assertEqual(health.get("status"), "completed", health)
            self.assertGreaterEqual(int(health.get("duration_ms", 0)), 2500)
        finally:
            stdout, stderr = self.stop_continuous_worker(process)
            self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows remote startup timeout test")
    def test_windows_remote_startup_timeout_is_bounded(self) -> None:
        unavailable = rf"\\127.0.0.1\codex-ntfy-unavailable-{uuid.uuid4().hex}"
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_scan_seconds=60,
            watch_discovery_seconds=60,
            watch_remote_timeout_seconds=5,
            watch_roots=[{"path": unavailable, "sqlite_path": unavailable, "origin": "remote-test"}],
        )
        worker_env = {
            **self.env,
            "CODEX_NTFY_SCAN_ONCE": "1",
            "CODEX_NTFY_TEST_REMOTE_CHILD_STARTUP_DELAY_MS": "6500",
        }
        process = subprocess.Popen(
            self.continuous_worker_command("powershell"),
            env=worker_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            health = self.wait_for_process_health(
                self.state / "remote-watch-health.json",
                process,
                {"completed", "timed-out", "failed"},
                timeout=30,
            )
            self.assertEqual(health.get("status"), "timed-out", health)
            self.assertEqual(health.get("timeout_phase"), "startup", health)
        finally:
            stdout, stderr = self.stop_continuous_worker(process)
            self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows remote scan timeout test")
    def test_windows_remote_active_scan_timeout_is_bounded(self) -> None:
        unavailable = rf"\\127.0.0.1\codex-ntfy-unavailable-{uuid.uuid4().hex}"
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_scan_seconds=60,
            watch_discovery_seconds=60,
            watch_remote_timeout_seconds=5,
            watch_roots=[{"path": unavailable, "sqlite_path": unavailable, "origin": "remote-test"}],
        )
        worker_env = {
            **self.env,
            "CODEX_NTFY_SCAN_ONCE": "1",
            "CODEX_NTFY_TEST_SCAN_DELAY_MS": "6500",
        }
        process = subprocess.Popen(
            self.continuous_worker_command("powershell"),
            env=worker_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            health = self.wait_for_process_health(
                self.state / "remote-watch-health.json",
                process,
                {"completed", "timed-out", "failed"},
                timeout=30,
            )
            self.assertEqual(health.get("status"), "timed-out", health)
            self.assertEqual(health.get("timeout_phase"), "scan", health)
        finally:
            stdout, stderr = self.stop_continuous_worker(process)
            self.assertIn(process.returncode, (0, 1, -15), msg=f"stdout={stdout}\nstderr={stderr}")

    def test_watcher_does_not_rewrite_an_unchanged_cursor(self) -> None:
        self.configure(
            idle_detection_mode="off",
            watch_rollouts=True,
            watch_discovery_seconds=5,
            watch_initial_replay_seconds=0,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                thread_id = str(uuid.uuid4())
                self.write_session_meta(thread_id, subagent=False)
                command = (
                    [
                        str(WINDOWS_POWERSHELL),
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(POWERSHELL_NOTIFIER),
                        "-ScanRollouts",
                    ]
                    if implementation == "powershell"
                    else [sys.executable, str(PYTHON_NOTIFIER), "--scan-rollouts"]
                )
                self.run_ok(command, timeout=60)
                cursors = list((self.state / "watch").glob("*.json"))
                self.assertEqual(len(cursors), 1)
                original = cursors[0].read_bytes()
                original_mtime = cursors[0].stat().st_mtime_ns
                time.sleep(0.2)

                self.run_ok(command, timeout=60)
                self.assertEqual(cursors[0].read_bytes(), original)
                self.assertEqual(cursors[0].stat().st_mtime_ns, original_mtime)
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                shutil.rmtree(self.state, ignore_errors=True)
                shutil.rmtree(self.codex_home / "sessions", ignore_errors=True)

    def test_strict_unverifiable_candidate_expires_without_delivery(self) -> None:
        self.configure(
            idle_detection_mode="strict",
            idle_probe_grace_seconds=0.1,
            goal_poll_seconds=0.02,
            suppress_technical_turns=False,
        )
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                self.run_ok(self.hook_command(implementation, self.event()))
                self.assertEqual(len(list((self.state / "pending").glob("*.json"))), 1)
                time.sleep(0.2)
                self.run_ok(self.worker_command(implementation), timeout=60)

                self.assertFalse(list((self.state / "pending").glob("*.json")))
                receipts = [
                    json.loads(path.read_text(encoding="utf-8-sig"))
                    for path in (self.state / "suppressed").glob("*.json")
                ]
                self.assertEqual([receipt.get("reason") for receipt in receipts], ["unverifiable"])
                with self.server.lock:
                    self.assertEqual(self.server.payloads, [])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_cleanup_removes_only_explicit_test_records(self) -> None:
        synthetic_thread = "00000000-0000-4000-8000-000000000001"
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                sent = self.state / "sent"
                sent.mkdir(parents=True, exist_ok=True)
                (sent / "synthetic.json").write_text(
                    json.dumps({"thread_id": synthetic_thread}), encoding="utf-8"
                )
                (sent / "real.json").write_text(
                    json.dumps({"thread_id": str(uuid.uuid4())}), encoding="utf-8"
                )
                command = (
                    [
                        str(WINDOWS_POWERSHELL),
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(POWERSHELL_NOTIFIER),
                        "-CleanupTestState",
                    ]
                    if implementation == "powershell"
                    else [sys.executable, str(PYTHON_NOTIFIER), "--cleanup-test-state"]
                )
                result = self.run_ok(command)
                self.assertEqual(result.stdout.strip(), "1")
                self.assertFalse((sent / "synthetic.json").exists())
                self.assertTrue((sent / "real.json").exists())
                shutil.rmtree(self.state, ignore_errors=True)

    def test_wsl_classification_is_side_effect_free(self) -> None:
        wrapper = (ROOT / "src" / "notify-ntfy-wsl.sh").read_text(encoding="utf-8")
        classification_line = next(line for line in wrapper.splitlines() if "--classify" in line and "detected=" in line)
        self.assertNotIn("--kick-worker", classification_line)

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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
                with self.server.lock:
                    self.assertEqual(len(self.server.payloads), 1)
                    self.server.payloads.clear()
                    self.server.statuses.clear()
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                self.assertEqual(len(list((self.state / "dead").glob("*.json"))), 1)
                self.run_ok(self.hook_command(implementation, event))
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                    self.run_ok(self.worker_command(implementation), timeout=60)
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

                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
                self.run_ok(self.worker_command(implementation), timeout=60)
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
        self.configure(markdown=True)
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
                self.assertIn("test-host", payload["message"])
                self.assertNotIn("Turn completed.", payload["message"])
                shutil.rmtree(self.state, ignore_errors=True)

    def test_message_opt_out_applies_to_an_already_queued_record(self) -> None:
        secret = "queued-message-that-must-not-leave-the-host"
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                event = self.event()
                event["last-assistant-message"] = secret
                self.run_ok(self.hook_command(implementation, event))
                queued = list((self.state / "outbox").glob("*.json"))
                self.assertEqual(len(queued), 1)
                self.assertIn(secret, queued[0].read_text(encoding="utf-8-sig"))
                self.configure(include_message=False)
                self.run_ok(self.worker_command(implementation))
                with self.server.lock:
                    payload = self.server.payloads.pop()
                self.assertNotIn(secret, payload["message"])
                self.assertEqual(payload["message"].split(" · ")[0], "test-host")
                shutil.rmtree(self.state, ignore_errors=True)
                self.configure(include_message=True)

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

    def test_database_thread_title_is_authoritative_when_session_index_is_missing_or_stale(self) -> None:
        thread_id = "66666666-6666-7666-8666-666666666666"
        turn_id = "77777777-7777-7777-8777-777777777777"
        title = "Titolo conversazione dal database"
        connection = sqlite3.connect(self.state_database)
        try:
            connection.execute(
                "INSERT INTO threads(id, rollout_path, source, thread_source, title) VALUES (?, ?, 'vscode', 'user', ?)",
                (thread_id, str(self.codex_home / "missing-rollout.jsonl"), title),
            )
            connection.commit()
        finally:
            connection.close()
        self.configure(include_thread_title=True)
        index_path = self.codex_home / "session_index.jsonl"
        for index_state in ("missing", "stale"):
            if index_state == "missing":
                index_path.unlink(missing_ok=True)
            else:
                index_path.write_text(
                    json.dumps({"id": thread_id, "thread_name": "Titolo obsoleto dall'indice"}) + "\n",
                    encoding="utf-8",
                )
            captured: dict[str, dict] = {}
            for implementation in self.implementations():
                with self.subTest(index_state=index_state, implementation=implementation):
                    self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id, turn_id=turn_id)))
                    self.run_ok(self.worker_command(implementation))
                    with self.server.lock:
                        captured[implementation] = self.server.payloads.pop()
                    self.assertEqual(captured[implementation]["title"], title)
                    self.assertEqual(captured[implementation]["tags"], ["white_check_mark"])
                    shutil.rmtree(self.state, ignore_errors=True)
            if "powershell" in captured:
                self.assertEqual(captured["powershell"], captured["python"])

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
                rollout = self.write_session_meta(thread_id, subagent=True)
                self.index_thread_rollout(thread_id, rollout, subagent=True)
                self.run_ok(self.hook_command(implementation, self.event(thread_id=thread_id)))
                self.assertFalse(list((self.state / "outbox").glob("*.json")))
                self.assertEqual(len(list((self.state / "suppressed").glob("*.json"))), 1)
                shutil.rmtree(self.state, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows detached worker regression")
    def test_preclassified_subagent_duplicate_restarts_worker_for_existing_queue_record(self) -> None:
        thread_id = str(uuid.uuid4())
        rollout = self.write_session_meta(thread_id, subagent=False)
        self.index_thread_rollout(thread_id, rollout, subagent=False)
        event = self.event(thread_id=thread_id)

        # Model a hook that durably committed its canonical record and crashed
        # before Start-DetachedWorker. The duplicate must not suppress or replace
        # that record, but it must restore worker liveness.
        self.run_ok(self.hook_command("powershell", event))
        queued = list((self.state / "outbox").glob("*.json"))
        self.assertEqual(len(queued), 1, self.state_debug())
        canonical = json.loads(queued[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(canonical.get("session_classification"), "root")
        self.assertFalse(list((self.state / "sent").glob("*.json")))

        duplicate_command = self.hook_command("powershell", event)
        duplicate_command.remove("-NoSpawn")
        raw_event = duplicate_command.pop()
        duplicate_command.extend(["-SessionClassification", "subagent", raw_event])
        spawn_env = self.env.copy()
        spawn_env.pop("CODEX_NTFY_NO_SPAWN", None)
        result = subprocess.run(
            duplicate_command,
            env=spawn_env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            log_path = self.state / "notify.log"
            log = log_path.read_text(encoding="utf-8-sig", errors="replace") if log_path.exists() else ""
            with self.server.lock:
                payload_count = len(self.server.payloads)
            if (
                payload_count == 1
                and len(list((self.state / "sent").glob("*.json"))) == 1
                and not list((self.state / "outbox").glob("*.json"))
                and "worker stopped" in log
            ):
                break
            time.sleep(0.05)
        else:
            health_path = self.state / "worker-health.json"
            if health_path.exists():
                with contextlib.suppress(OSError, json.JSONDecodeError, TypeError, ValueError):
                    worker_pid = int(json.loads(health_path.read_text(encoding="utf-8-sig")).get("pid", 0) or 0)
                    if self.windows_pid_is_alive(worker_pid):
                        self.taskkill_tree_if_running(worker_pid)
            self.fail("detached worker did not dispose the pre-existing queue record\n" + self.state_debug())

        with self.server.lock:
            self.assertEqual(len(self.server.payloads), 1)
        self.assertEqual(len(list((self.state / "sent").glob("*.json"))), 1, self.state_debug())
        self.assertFalse(list((self.state / "pending").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "suppressed").glob("*.json")), self.state_debug())
        self.assertFalse(list((self.state / "dead").glob("*.json")), self.state_debug())

    def test_spawn_edge_overrides_generic_root_source(self) -> None:
        for implementation in self.implementations():
            with self.subTest(implementation=implementation):
                child_id = str(uuid.uuid4())
                parent_id = str(uuid.uuid4())
                rollout = self.write_session_meta(child_id, subagent=False)
                connection = sqlite3.connect(self.state_database)
                try:
                    connection.execute(
                        "INSERT INTO threads(id, rollout_path, source, thread_source, title) "
                        "VALUES (?, ?, 'vscode', '', '')",
                        (child_id, str(rollout)),
                    )
                    connection.execute(
                        "INSERT INTO thread_spawn_edges(parent_thread_id, child_thread_id, status) "
                        "VALUES (?, ?, 'open')",
                        (parent_id, child_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
                self.run_ok(self.hook_command(implementation, self.event(thread_id=child_id)))
                self.assertFalse(list((self.state / "outbox").glob("*.json")), self.state_debug())
                receipts = list((self.state / "suppressed").glob("*.json"))
                self.assertEqual(len(receipts), 1, self.state_debug())
                self.assertEqual(json.loads(receipts[0].read_text(encoding="utf-8-sig")).get("reason"), "subagent")
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
                rollout = self.write_session_meta(thread_id, subagent=True)
                self.index_thread_rollout(thread_id, rollout, subagent=True)
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
        self.index_thread_rollout(thread_id, session, subagent=True)
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
    def test_installer_strict_preflight_accepts_bom_and_rejects_invalid_text_without_mutation(self) -> None:
        home = self.temp / "strict-preflight-home"
        home.mkdir()
        config_path = home / "ntfy-config.json"
        toml_path = home / "config.toml"
        valid_json = json.dumps({"topic": "attività-già"}, ensure_ascii=False).encode("utf-8")
        config_path.write_bytes(b"\xef\xbb\xbf" + valid_json)
        toml_path.write_bytes(b"\xef\xbb\xbf" + "# attività già\n".encode("utf-8"))
        target = self.temp / "must-not-exist" / "settings.json"
        script = r"""
$ErrorActionPreference = 'Stop'
$Utf8StrictNoBom = New-Object Text.UTF8Encoding($false, $true)
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Test-SafeUnicodeScalarText',
    'Assert-SafeUnicodeScalarText',
    'Assert-JsonUnicodeScalars',
    'ConvertFrom-StrictJsonText',
    'Read-StrictUtf8Text',
    'Read-StrictJsonFile',
    'Assert-ManagedInstallerInputs',
    'Write-TextAtomic'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}
Assert-ManagedInstallerInputs -HomePath $env:CODEX_NTFY_TEST_HOME
$valid = Read-StrictJsonFile -Path (Join-Path $env:CODEX_NTFY_TEST_HOME 'ntfy-config.json')
if ([string]$valid.topic -ne 'attività-già') { throw 'valid BOM/accent content was not preserved' }
try {
  Write-TextAtomic -Path $env:CODEX_NTFY_TEST_TARGET -Content ([string][char]0xFFFD)
  throw 'invalid atomic content unexpectedly succeeded'
} catch {
  if ($_.Exception.Message -eq 'invalid atomic content unexpectedly succeeded') { throw }
}
if (Test-Path -LiteralPath (Split-Path -Parent $env:CODEX_NTFY_TEST_TARGET)) {
  throw 'invalid atomic content mutated the target directory'
}
"VALID"
"""
        base_env = {
            **os.environ,
            "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
            "CODEX_NTFY_TEST_HOME": str(home),
            "CODEX_NTFY_TEST_TARGET": str(target),
        }
        valid = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env=base_env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        self.assertIn("VALID", valid.stdout)
        original_toml = toml_path.read_bytes()
        invalid_cases = (
            b'{"topic":"bad"}\xff',
            b'{"topic":"\\ud800"}',
            json.dumps({"topic": "\ufffd"}, ensure_ascii=False).encode("utf-8"),
        )
        for invalid in invalid_cases:
            with self.subTest(invalid=invalid):
                config_path.write_bytes(invalid)
                before = {path.name: path.read_bytes() for path in home.iterdir() if path.is_file()}
                failed = subprocess.run(
                    [
                        str(WINDOWS_POWERSHELL),
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        script,
                    ],
                    env=base_env,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
                after = {path.name: path.read_bytes() for path in home.iterdir() if path.is_file()}
                self.assertEqual(after, before)
                self.assertFalse(target.parent.exists())
        self.assertEqual(toml_path.read_bytes(), original_toml)

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
        self.assertEqual(private_config["tags"], ["white_check_mark"])
        self.assertEqual(private_config["max_message_chars"], 180)
        self.assertFalse(private_config["markdown"])
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
        self.assertFalse(config["include_task_link"])
        self.assertFalse(config["include_task_link_action"])
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
        self.assertEqual(config["tags"], ["white_check_mark"])
        self.assertEqual(config["max_message_chars"], 180)
        self.assertFalse(config["markdown"])
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
        config["tags"] = ["computer", "white_check_mark"]
        config["max_message_chars"] = 900
        config["markdown"] = True
        config.pop("include_message")
        config.pop("include_thread_title")
        (install_home / "ntfy-config.json").write_text(json.dumps(config), encoding="utf-8")
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
        migrated = json.loads((install_home / "ntfy-config.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(migrated["tags"], ["white_check_mark"])
        self.assertEqual(migrated["max_message_chars"], 900)
        self.assertTrue(migrated["markdown"])
        self.assertFalse(migrated["include_message"])
        self.assertFalse(migrated["include_thread_title"])
        vbs = (install_home / "watch-codex-ntfy-hidden.vbs").read_text(encoding="utf-8-sig")
        self.assertIn("WScript.ScriptFullName", vbs)
        self.assertNotIn("C:\\Windows", vbs)
        self.assertIn("\\notify-ntfy.ps1", vbs)
        self.assertIn("-Worker -Continuous", vbs)
        self.assertNotIn("\\watch-codex-ntfy.ps1", vbs)
        watcher = (install_home / "watch-codex-ntfy.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("while ($true)", watcher)
        self.assertIn("Start-Sleep -Seconds $restartDelay", watcher)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_rejects_old_claude_surface_even_with_current_path(self) -> None:
        install_home = self.temp / "claude-version-codex-home"
        claude_home = self.temp / "claude-version-home"
        fake_user = self.temp / "fake-user"
        fake_appdata = self.temp / "fake-appdata"
        fake_localappdata = self.temp / "fake-localappdata"
        current_bin = self.temp / "current-claude-bin"
        old_vscode_bin = (
            fake_user
            / ".vscode"
            / "extensions"
            / "anthropic.claude-code-version-test"
            / "resources"
            / "native-binary"
        )
        for directory in (
            install_home,
            claude_home,
            fake_user,
            fake_appdata,
            fake_localappdata,
            current_bin,
            old_vscode_bin,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        seed_executable = self.temp / "fake-claude.exe"
        compiler_script = r"""
$source = @'
using System;
using System.IO;
public static class FakeClaudeVersion {
    public static void Main() {
        string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "version.txt");
        Console.WriteLine(File.ReadAllText(path).Trim() + " (Claude Code)");
    }
}
'@
Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $env:FAKE_CLAUDE_EXE -OutputType ConsoleApplication -ErrorAction Stop
"""
        compiled = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                compiler_script,
            ],
            env={**os.environ, "FAKE_CLAUDE_EXE": str(seed_executable)},
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)

        shutil.copy2(seed_executable, current_bin / "claude.exe")
        (current_bin / "version.txt").write_text("2.1.209\n", encoding="utf-8")
        shutil.copy2(seed_executable, old_vscode_bin / "claude.exe")
        (old_vscode_bin / "version.txt").write_text("2.1.197\n", encoding="utf-8")
        (install_home / "ntfy-config.json").write_text(
            json.dumps({"server": "http://127.0.0.1:9", "topic": "unused-version-test"}),
            encoding="utf-8",
        )

        env = {
            **os.environ,
            "USERPROFILE": str(fake_user),
            "APPDATA": str(fake_appdata),
            "LOCALAPPDATA": str(fake_localappdata),
            "PATH": str(current_bin) + os.pathsep + os.environ.get("PATH", ""),
        }
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
                "-EnableClaudeCode",
                "-ClaudeHome",
                str(claude_home),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("VS Code 2.1.197", output)
        self.assertIn("2.1.198", output)
        self.assertNotIn("PATH 2.1.209", output)
        self.assertFalse((claude_home / "settings.json").exists())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_merges_claude_hooks_idempotently(self) -> None:
        install_home = self.temp / "claude-install-codex-home"
        claude_home = self.temp / "claude-home"
        install_home.mkdir()
        claude_home.mkdir()
        settings_path = claude_home / "settings.json"
        existing = {
            "model": "preserve-model",
            "custom": {"preserve": True},
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|PowerShell",
                        "hooks": [{"type": "command", "command": "keep-guard"}],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "keep-stop"}]}],
                "UserPromptExpansion": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": str(WINDOWS_POWERSHELL),
                                "args": [
                                    "-File",
                                    str(install_home / "notify-ntfy.ps1"),
                                    "-ClaudeHook",
                                ],
                            }
                        ]
                    }
                ],
                "SessionEnd": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": str(WINDOWS_POWERSHELL),
                                "args": ["-File", r"C:\old\notify-ntfy.ps1", "-ClaudeHook"],
                            }
                        ]
                    }
                ],
            },
        }
        settings_path.write_text(json.dumps(existing), encoding="utf-8")
        original_settings_acl = self.read_windows_acl_summary(settings_path)
        command = [
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
            "-EnableClaudeCode",
            "-ClaudeHome",
            str(claude_home),
        ]
        env = {**os.environ, "CODEX_NTFY_TOPIC": "claude-installer-test-topic"}
        first = subprocess.run(command, env=env, text=True, capture_output=True, timeout=90)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.read_windows_acl_summary(settings_path), original_settings_acl)

        installed = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(installed["model"], "preserve-model")
        self.assertTrue(installed["custom"]["preserve"])
        self.assertEqual(installed["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "keep-guard")
        self.assertEqual(installed["hooks"]["Stop"][0]["hooks"][0]["command"], "keep-stop")
        self.assertEqual(
            installed["hooks"]["SessionEnd"][0]["hooks"][0]["args"],
            ["-File", r"C:\old\notify-ntfy.ps1", "-ClaudeHook"],
        )
        managed: list[tuple[str, dict]] = []
        for event_name in ("Stop", "StopFailure", "UserPromptSubmit", "Notification"):
            for group in installed["hooks"].get(event_name, []):
                for handler in group.get("hooks", []):
                    args = [str(value) for value in handler.get("args", [])]
                    if "-ClaudeHook" in args:
                        managed.append((event_name, handler))
        self.assertEqual(
            [event for event, _handler in managed],
            ["Stop", "StopFailure", "UserPromptSubmit", "Notification", "Notification"],
        )
        self.assertNotIn("UserPromptExpansion", installed["hooks"])
        self.assertEqual(
            [group["matcher"] for group in installed["hooks"]["Notification"][-2:]],
            ["idle_prompt", "agent_completed"],
        )
        for managed_event, handler in managed:
            self.assertEqual(handler["command"], str(WINDOWS_POWERSHELL))
            self.assertIn("-ReadStdin", handler["args"])
            file_index = handler["args"].index("-File")
            self.assertTrue(
                os.path.samefile(
                    handler["args"][file_index + 1],
                    install_home / "notify-ntfy.ps1",
                )
            )
            self.assertEqual(handler["args"][-2:], ["-Origin", "Claude Code"])
            self.assertEqual(handler["async"], managed_event == "Notification")
            self.assertEqual(handler["timeout"], 30)

        first_bytes = settings_path.read_bytes()
        second = subprocess.run(command, env=env, text=True, capture_output=True, timeout=90)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(settings_path.read_bytes(), first_bytes)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_worker_stop_is_path_and_lifetime_scoped(self) -> None:
        install_home = self.temp / "owned-worker-home"
        other_home = self.temp / "other-worker-home"
        install_home.mkdir()
        other_home.mkdir()
        script = r"""
$ErrorActionPreference = 'Stop'
$TaskName = 'CodexNtfyWatcher'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Get-ObjectValue',
    'Get-CimProcessCreationUtc',
    'Get-PowerShellCommandTokenPattern',
    'Test-LegacyNotifierProcessShape',
    'Test-OwnedScheduledTask',
    'Stop-LegacyTask'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}

$ownedHome = [IO.Path]::GetFullPath($env:CODEX_NTFY_TEST_OWNED_HOME)
$otherHome = [IO.Path]::GetFullPath($env:CODEX_NTFY_TEST_OTHER_HOME)
$ownedNotifier = Join-Path $ownedHome 'notify-ntfy.ps1'
$otherNotifier = Join-Path $otherHome 'notify-ntfy.ps1'
$ownedHidden = Join-Path $ownedHome 'watch-codex-ntfy-hidden.vbs'
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$oldCreation = [DateTime]::UtcNow.AddMinutes(-5)
$newCreation = $oldCreation.AddSeconds(1)
$script:stopped = New-Object 'System.Collections.Generic.List[int]'
$script:initial = @(
  [pscustomobject]@{
    Name = 'powershell.exe'; ProcessId = 101; CreationDate = $oldCreation
    CommandLine = ('powershell.exe -NoProfile -File "{0}" -Worker -Continuous' -f $ownedNotifier)
  },
  [pscustomobject]@{
    Name = 'powershell.exe'; ProcessId = 102; CreationDate = $oldCreation
    CommandLine = ('powershell.exe -NoProfile -File "{0}" -Worker -Continuous' -f $otherNotifier)
  },
  [pscustomobject]@{
    Name = 'powershell.exe'; ProcessId = 103; CreationDate = $oldCreation
    CommandLine = ('powershell.exe -NoProfile -File "{0}" -AudnCodeHook' -f $ownedNotifier)
  },
  [pscustomobject]@{
    Name = 'powershell.exe'; ProcessId = 104; CreationDate = $oldCreation
    CommandLine = ('powershell.exe -NoProfile -File "{0}" -Worker -Continuous' -f $ownedNotifier)
  },
  [pscustomobject]@{
    Name = 'wscript.exe'; ProcessId = 105; CreationDate = $oldCreation
    ExecutablePath = $wscript
    CommandLine = ('"{0}" //B //Nologo "{1}"' -f $wscript, $ownedHidden)
  }
)

function Get-ScheduledTask {
  param([string]$TaskName, [object]$ErrorAction)
  return $null
}
function Start-Sleep { param([int]$Milliseconds) }
function Stop-Process {
  param([int]$Id, [switch]$Force, [object]$ErrorAction)
  $script:stopped.Add($Id)
}
function Get-CimInstance {
  param([string]$ClassName, [string]$Filter, [object]$ErrorAction)
  if ([string]::IsNullOrWhiteSpace($Filter)) { return $script:initial }
  $targetPid = [int]([regex]::Match($Filter, '\d+').Value)
  if ($script:stopped.Contains($targetPid)) { return @() }
  $row = @($script:initial | Where-Object { [int]$_.ProcessId -eq $targetPid })
  if ($targetPid -eq 104 -and $row.Count -eq 1) {
    return [pscustomobject]@{
      Name = $row[0].Name; ProcessId = 104; CreationDate = $newCreation
      CommandLine = $row[0].CommandLine
    }
  }
  return $row
}

$ownTask = [pscustomobject]@{ Actions = @([pscustomobject]@{
  Execute = $wscript
  Arguments = ('//B //Nologo "{0}"' -f $ownedHidden)
  WorkingDirectory = $ownedHome
}) }
$otherTask = [pscustomobject]@{ Actions = @([pscustomobject]@{
  Execute = $wscript
  Arguments = ('//B //Nologo "{0}"' -f (Join-Path $otherHome 'watch-codex-ntfy-hidden.vbs'))
  WorkingDirectory = $otherHome
}) }
$extraArgsTask = [pscustomobject]@{ Actions = @([pscustomobject]@{
  Execute = $wscript
  Arguments = ('//B //Nologo "{0}" extra' -f $ownedHidden)
  WorkingDirectory = $ownedHome
}) }

Stop-LegacyTask -HomePath $ownedHome -GraceMilliseconds 0 -ExitWaitMilliseconds 25
[pscustomobject]@{
  stopped = @($script:stopped)
  own_task = Test-OwnedScheduledTask -Task $ownTask -HomePath $ownedHome
  other_task = Test-OwnedScheduledTask -Task $otherTask -HomePath $ownedHome
  extra_args_task = Test-OwnedScheduledTask -Task $extraArgsTask -HomePath $ownedHome
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_TEST_OWNED_HOME": str(install_home),
                "CODEX_NTFY_TEST_OTHER_HOME": str(other_home),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["stopped"], [101, 105])
        self.assertTrue(summary["own_task"])
        self.assertFalse(summary["other_task"])
        self.assertFalse(summary["extra_args_task"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_stops_only_exact_orphaned_legacy_audncode_hooks(self) -> None:
        install_home = self.temp / "audncode-orphan-install-home"
        audncode_home = self.temp / "audncode-orphan-home"
        install_home.mkdir()
        audncode_home.mkdir()
        legacy_script = install_home / "notify-ntfy.ps1"
        legacy_script.write_text(
            """
param(
  [switch]$AudnCodeHook,
  [switch]$ReadStdin,
  [string]$Origin,
  [string]$AudnCodeHome,
  [string]$AudnCodeExpectedEvent
)
Start-Sleep -Seconds 120
""",
            encoding="utf-8",
        )

        def legacy_command(*, current_shape: bool = False) -> list[str]:
            command = [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(legacy_script),
                "-AudnCodeHook",
                "-ReadStdin",
                "-Origin",
                "AudnCode",
                "-AudnCodeHome",
                str(audncode_home),
            ]
            if current_shape:
                command.extend(["-AudnCodeExpectedEvent", "PostToolUse"])
            return command

        orphan_launcher = r"""
import json
import subprocess
import sys

process = subprocess.Popen(
    json.loads(sys.argv[1]),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
print(process.pid, flush=True)
"""

        def start_orphan(command: list[str]) -> int:
            launched = subprocess.run(
                [sys.executable, "-c", orphan_launcher, json.dumps(command)],
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(launched.returncode, 0, launched.stdout + launched.stderr)
            return int(launched.stdout.strip().splitlines()[-1])

        def process_is_alive(pid: int) -> bool:
            probe = subprocess.run(
                [
                    str(WINDOWS_POWERSHELL),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "if (Get-Process -Id $env:CODEX_NTFY_TEST_PID -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }",
                ],
                env={**os.environ, "CODEX_NTFY_TEST_PID": str(pid)},
                capture_output=True,
                timeout=20,
            )
            return probe.returncode == 0

        orphan_legacy_pid = start_orphan(legacy_command())
        orphan_current_pid = start_orphan(legacy_command(current_shape=True))
        live_legacy = subprocess.Popen(
            legacy_command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            time.sleep(1.5)
            self.assertTrue(process_is_alive(orphan_legacy_pid))
            self.assertTrue(process_is_alive(orphan_current_pid))
            self.assertIsNone(live_legacy.poll())
            command = [
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
                "-EnableAudnCode",
                "-AudnCodeHome",
                str(audncode_home),
            ]
            installed = subprocess.run(
                command,
                env={
                    **os.environ,
                    "CODEX_NTFY_TOPIC": "audncode-orphan-cleanup-test",
                    "CODEX_NTFY_TEST_MODE": "1",
                    "CODEX_NTFY_TEST_LEGACY_ORPHAN_MIN_AGE_SECONDS": "1",
                },
                text=True,
                capture_output=True,
                timeout=90,
            )
            self.assertEqual(
                installed.returncode,
                0,
                installed.stdout + installed.stderr,
            )
            deadline = time.time() + 10
            while time.time() < deadline and process_is_alive(orphan_legacy_pid):
                time.sleep(0.05)
            self.assertFalse(process_is_alive(orphan_legacy_pid))
            self.assertTrue(process_is_alive(orphan_current_pid))
            self.assertIsNone(live_legacy.poll())
            self.assertIn(
                "Stopped 1 verified orphaned legacy AudnCode hook process(es).",
                installed.stdout + installed.stderr,
            )
        finally:
            if live_legacy.poll() is None:
                live_legacy.terminate()
            live_legacy.communicate(timeout=10)
            for pid in (orphan_legacy_pid, orphan_current_pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    text=True,
                    capture_output=True,
                    timeout=20,
                )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_merges_audncode_hooks_and_idle_delay_idempotently(self) -> None:
        install_home = self.temp / "audncode-install-O'Brien"
        audncode_home = self.temp / "audncode-home-D'Angelo"
        install_home.mkdir()
        audncode_home.mkdir()
        settings_path = audncode_home / "settings.json"
        global_config_path = audncode_home / ".openclaude.json"
        installed_script = install_home / "notify-ntfy.ps1"
        escaped_installed_script = str(installed_script).replace("'", "''")
        old_managed_command = (
            f"& '{escaped_installed_script}' -AudnCodeHook -ReadStdin -Origin 'AudnCode'"
        )
        existing = {
            "model": "preserve-model",
            "custom": {"preserve": True},
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|PowerShell",
                        "hooks": [{"type": "command", "command": "keep-guard"}],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "keep-stop"}]}],
                "StopFailure": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": old_managed_command,
                                "shell": "powershell",
                            }
                        ]
                    }
                ],
                "Notification": [
                    {
                        "matcher": "idle_prompt",
                        "hooks": [
                            {
                                "type": "command",
                                "command": old_managed_command,
                                "shell": "powershell",
                            }
                        ],
                    },
                    {
                        "matcher": "idle_prompt",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "& 'C:\\old\\notify-ntfy.ps1' -AudnCodeHook",
                                "shell": "powershell",
                            }
                        ],
                    },
                ],
            },
        }
        settings_path.write_text(json.dumps(existing), encoding="utf-8")
        original_settings_acl = self.read_windows_acl_summary(settings_path)
        global_config_path.write_text(
            json.dumps({"numStartups": 7, "cache": {"preserve": [1, 2, 3]}}),
            encoding="utf-8",
        )
        command = [
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
            "-EnableAudnCode",
            "-AudnCodeHome",
            str(audncode_home),
            "-AudnCodeIdleThresholdMs",
            "1000",
        ]
        env = {**os.environ, "CODEX_NTFY_TOPIC": "audncode-installer-test-topic"}
        first = subprocess.run(command, env=env, text=True, capture_output=True, timeout=90)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.read_windows_acl_summary(settings_path), original_settings_acl)
        self.assertIn("Close and reopen every running AudnCode window once", first.stdout + first.stderr)
        hook_marker_path = audncode_home / ".codex-ntfy-hooks.json"
        first_hook_marker_bytes = hook_marker_path.read_bytes()
        first_hook_marker = json.loads(first_hook_marker_bytes.decode("utf-8-sig"))
        self.assertEqual(first_hook_marker["kind"], "codex-ntfy-audncode-hooks")
        self.assertEqual(first_hook_marker["notifier_version"], "2.6.0")
        self.assertEqual(first_hook_marker["hook_shape_version"], 9)
        self.assertRegex(first_hook_marker["generation"], r"^[a-f0-9]{32}$")

        installed = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(installed["model"], "preserve-model")
        self.assertTrue(installed["custom"]["preserve"])
        self.assertEqual(installed["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "keep-guard")
        self.assertEqual(installed["hooks"]["Stop"][0]["hooks"][0]["command"], "keep-stop")
        managed: list[tuple[str, dict]] = []
        for event_name in (
            "SessionStart",
            "Stop",
            "StopFailure",
            "UserPromptSubmit",
            "Notification",
            "PostToolUse",
            "SubagentStart",
        ):
            for group in installed["hooks"].get(event_name, []):
                for handler in group.get("hooks", []):
                    if "-AudnCodeHook" in str(handler.get("command", "")) and (
                        escaped_installed_script.lower()
                        in str(handler.get("command", "")).lower()
                    ):
                        managed.append((event_name, handler))
        self.assertEqual(
            [event for event, _handler in managed],
            [
                "SessionStart",
                "Stop",
                "StopFailure",
                "UserPromptSubmit",
                "Notification",
                "PostToolUse",
                "SubagentStart",
            ],
        )
        session_start_matchers = [
            group.get("matcher")
            for group in installed["hooks"]["SessionStart"]
            if any(
                escaped_installed_script.lower() in str(handler.get("command", "")).lower()
                for handler in group.get("hooks", [])
            )
        ]
        self.assertEqual(session_start_matchers, ["^(startup|resume|clear)$"])
        self.assertNotIn("compact", session_start_matchers[0])
        notification_matchers = [
            group.get("matcher")
            for group in installed["hooks"]["Notification"]
            if any(
                escaped_installed_script.lower() in str(handler.get("command", "")).lower()
                for handler in group.get("hooks", [])
            )
        ]
        self.assertEqual(notification_matchers, ["^(idle_prompt|permission_prompt)$"])
        post_tool_matchers = [
            group.get("matcher")
            for group in installed["hooks"]["PostToolUse"]
            if any(
                escaped_installed_script.lower() in str(handler.get("command", "")).lower()
                for handler in group.get("hooks", [])
            )
        ]
        self.assertEqual(
            post_tool_matchers,
            ["^(Agent|AskUserQuestion|Bash|PowerShell|Monitor|TaskStop|KillShell|CronCreate|CronDelete|SendMessage)$"],
        )
        for event_name, handler in managed:
            self.assertEqual(handler["type"], "command")
            self.assertEqual(handler["shell"], "powershell")
            self.assertFalse(handler["async"])
            self.assertEqual(handler["timeout"], 60)
            self.assertNotIn("args", handler)
            self.assertTrue(
                handler["command"].startswith(
                    "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; & "
                ),
                handler["command"],
            )
            self.assertNotIn(str(WINDOWS_POWERSHELL).replace("'", "''"), handler["command"])
            self.assertIn("-ReadStdin", handler["command"])
            self.assertIn("-Origin 'AudnCode'", handler["command"])
            self.assertIn(str(audncode_home).replace("'", "''"), handler["command"])
            self.assertIn(
                f"-AudnCodeExpectedEvent '{event_name}'", handler["command"]
            )

        self.assertFalse(
            any(
                r"C:\old\notify-ntfy.ps1".lower() in str(handler.get("command", "")).lower()
                for groups in installed["hooks"].values()
                for group in groups
                for handler in group.get("hooks", [])
            ),
            "an AudnCode hook from a previous CodexHome would duplicate notifications",
        )

        global_config = json.loads(global_config_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(global_config["numStartups"], 7)
        self.assertEqual(global_config["cache"], {"preserve": [1, 2, 3]})
        self.assertEqual(global_config["messageIdleNotifThresholdMs"], 1000)

        first_settings_bytes = settings_path.read_bytes()
        first_global_bytes = global_config_path.read_bytes()
        second = subprocess.run(command, env=env, text=True, capture_output=True, timeout=90)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(settings_path.read_bytes(), first_settings_bytes)
        self.assertEqual(self.read_windows_acl_summary(settings_path), original_settings_acl)
        self.assertEqual(global_config_path.read_bytes(), first_global_bytes)
        self.assertEqual(hook_marker_path.read_bytes(), first_hook_marker_bytes)
        self.assertIn("observation generation was preserved", second.stdout + second.stderr)
        self.assertNotIn("Close and reopen every running AudnCode window once", second.stdout + second.stderr)
        if POWERSHELL_7 is not None:
            pwsh_command = [str(POWERSHELL_7), *command[1:]]
            pwsh_result = subprocess.run(
                pwsh_command,
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
            )
            self.assertEqual(
                pwsh_result.returncode,
                0,
                pwsh_result.stdout + pwsh_result.stderr,
            )
            self.assertEqual(settings_path.read_bytes(), first_settings_bytes)
            self.assertEqual(self.read_windows_acl_summary(settings_path), original_settings_acl)
            self.assertEqual(global_config_path.read_bytes(), first_global_bytes)
            self.assertEqual(hook_marker_path.read_bytes(), first_hook_marker_bytes)
            self.assertIn(
                "observation generation was preserved",
                pwsh_result.stdout + pwsh_result.stderr,
            )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_defaults_audncode_home_to_claude_config_dir(self) -> None:
        install_home = self.temp / "audncode-env-codex-home"
        audncode_home = self.temp / "audncode-env-home-O'Brien"
        install_home.mkdir()
        audncode_home.mkdir()
        command = [
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
            "-EnableAudnCode",
        ]
        result = subprocess.run(
            command,
            env={
                **os.environ,
                "CLAUDE_CONFIG_DIR": str(audncode_home),
                "CODEX_NTFY_TOPIC": "audncode-env-installer-test-topic",
            },
            text=True,
            capture_output=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        settings_path = audncode_home / "settings.json"
        marker_path = audncode_home / ".codex-ntfy-hooks.json"
        global_config_path = audncode_home / ".openclaude.json"
        self.assertTrue(settings_path.is_file())
        self.assertTrue(self.read_windows_acl_summary(settings_path)["protected"])
        self.assertTrue(marker_path.is_file())
        self.assertTrue(global_config_path.is_file())

        installed = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        managed_commands = [
            str(handler.get("command", ""))
            for event_name in (
                "SessionStart",
                "Stop",
                "StopFailure",
                "UserPromptSubmit",
                "Notification",
                "PostToolUse",
                "SubagentStart",
            )
            for group in installed["hooks"].get(event_name, [])
            for handler in group.get("hooks", [])
            if "-AudnCodeHook" in str(handler.get("command", ""))
        ]
        self.assertEqual(len(managed_commands), 7)
        escaped_home = str(audncode_home.resolve()).replace("'", "''")
        self.assertTrue(all(f"-AudnCodeHome '{escaped_home}'" in command for command in managed_commands))
        self.assertEqual(
            json.loads(global_config_path.read_text(encoding="utf-8-sig"))[
                "messageIdleNotifThresholdMs"
            ],
            1000,
        )

        first_settings_bytes = settings_path.read_bytes()
        explicit_home = self.temp / "audncode-explicit-profile"
        explicit_home.mkdir()
        explicit = subprocess.run(
            command + ["-AudnCodeHome", str(explicit_home)],
            env={
                **os.environ,
                "CLAUDE_CONFIG_DIR": str(audncode_home),
                "CODEX_NTFY_TOPIC": "audncode-explicit-installer-test-topic",
            },
            text=True,
            capture_output=True,
            timeout=90,
        )
        self.assertEqual(explicit.returncode, 0, explicit.stdout + explicit.stderr)
        self.assertEqual(settings_path.read_bytes(), first_settings_bytes)
        explicit_settings = json.loads(
            (explicit_home / "settings.json").read_text(encoding="utf-8-sig")
        )
        explicit_commands = [
            str(handler.get("command", ""))
            for event_name in (
                "SessionStart",
                "Stop",
                "StopFailure",
                "UserPromptSubmit",
                "Notification",
                "PostToolUse",
                "SubagentStart",
            )
            for group in explicit_settings["hooks"].get(event_name, [])
            for handler in group.get("hooks", [])
            if "-AudnCodeHook" in str(handler.get("command", ""))
        ]
        escaped_explicit_home = str(explicit_home.resolve()).replace("'", "''")
        self.assertEqual(len(explicit_commands), 7)
        self.assertTrue(
            all(
                f"-AudnCodeHome '{escaped_explicit_home}'" in hook_command
                for hook_command in explicit_commands
            )
        )
        self.assertTrue((explicit_home / ".codex-ntfy-hooks.json").is_file())
        self.assertTrue((explicit_home / ".openclaude.json").is_file())

    def test_audncode_selective_uninstall_keeps_full_json_depth(self) -> None:
        document = (ROOT / "docs" / "uninstall.md").read_text(encoding="utf-8")
        audncode_section = document.split("#### Remove the optional AudnCode handlers", 1)[1]
        audncode_section = audncode_section.split("### 3. Remove managed files", 1)[0]
        self.assertIn("ConvertTo-Json -Depth 100", audncode_section)
        self.assertNotIn("ConvertTo-Json -Depth 32", audncode_section)
        create_temp = audncode_section.index("[IO.File]::Open($TempPath")
        protect_temp = audncode_section.index("[IO.File]::SetAccessControl($TempPath")
        write_temp = audncode_section.index("[IO.File]::WriteAllText($TempPath")
        self.assertLess(create_temp, protect_temp)
        self.assertLess(protect_temp, write_temp)
        self.assertIn("[int]$ShapeProperty.Value -le 9", audncode_section)
        self.assertNotIn("[int]$ShapeProperty.Value -le 8", audncode_section)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_atomic_writer_secures_temp_before_content_in_permissive_directory(self) -> None:
        directory = self.temp / "permissive-atomic-writer"
        directory.mkdir()
        target = directory / "settings.json"
        secret = "provider-secret-that-must-never-enter-a-readable-temp"
        script = r"""
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object Text.UTF8Encoding($false, $true)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @('Write-TextAtomic', 'Protect-PrivatePath')) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$grants = @("${identity}:(OI)(CI)F", '*S-1-1-0:(OI)(CI)R')
& icacls.exe $env:CODEX_NTFY_TEST_DIRECTORY /inheritance:r /grant:r $grants | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not prepare permissive test directory" }
Write-TextAtomic -Path $env:CODEX_NTFY_TEST_TARGET -Content $env:CODEX_NTFY_TEST_SECRET
$everyone = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
$directoryRules = ([IO.Directory]::GetAccessControl($env:CODEX_NTFY_TEST_DIRECTORY)).GetAccessRules(
  $true, $true, [Security.Principal.SecurityIdentifier]
)
$fileRules = ([IO.File]::GetAccessControl($env:CODEX_NTFY_TEST_TARGET)).GetAccessRules(
  $true, $true, [Security.Principal.SecurityIdentifier]
)
$directoryEveryone = @($directoryRules | Where-Object { $_.IdentityReference -eq $everyone }).Count
$fileEveryone = @($fileRules | Where-Object { $_.IdentityReference -eq $everyone }).Count
[pscustomobject]@{
  directory_everyone = $directoryEveryone
  file_everyone = $fileEveryone
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_TEST_DIRECTORY": str(directory),
                "CODEX_NTFY_TEST_TARGET": str(target),
                "CODEX_NTFY_TEST_SECRET": secret,
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observation = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreater(observation["directory_everyone"], 0)
        self.assertEqual(observation["file_everyone"], 0)
        self.assertEqual(target.read_text(encoding="utf-8-sig"), secret)
        self.assertFalse(list(directory.glob(".settings.json.*.tmp")))

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_refuses_live_audncode_without_writing_settings(self) -> None:
        install_home = self.temp / "audncode-live-refusal-codex-home"
        audncode_home = self.temp / "audncode-live-refusal-home"
        install_home.mkdir()
        (audncode_home / "sessions").mkdir(parents=True)
        settings_path = audncode_home / "settings.json"
        global_path = audncode_home / ".openclaude.json"
        original_settings = b'{"model":"keep-live","hooks":{}}\n'
        original_global = b'{"numStartups":17}\n'
        settings_path.write_bytes(original_settings)
        global_path.write_bytes(original_global)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        host = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=creationflags,
        )
        def stop_host() -> None:
            if host.poll() is None:
                host.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                host.communicate(timeout=5)

        self.addCleanup(stop_host)
        (audncode_home / "sessions" / f"{host.pid}.json").write_text(
            json.dumps(
                {
                    "pid": host.pid,
                    "sessionId": str(uuid.uuid4()),
                    "cwd": str(self.temp),
                    "startedAt": int(time.time() * 1000),
                    "kind": "interactive",
                    "entrypoint": "cli",
                }
            ),
            encoding="utf-8",
        )
        command = [
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
            "-EnableAudnCode",
            "-AudnCodeHome",
            str(audncode_home),
        ]
        result = subprocess.run(
            command,
            env={**os.environ, "CODEX_NTFY_TOPIC": "audncode-live-refusal-test-topic"},
            text=True,
            capture_output=True,
            timeout=90,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Close every AudnCode process", result.stdout + result.stderr)
        self.assertEqual(settings_path.read_bytes(), original_settings)
        self.assertEqual(global_path.read_bytes(), original_global)
        self.assertFalse((audncode_home / ".codex-ntfy-hooks.json").exists())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_ignores_stale_audncode_pid_marker(self) -> None:
        install_home = self.temp / "audncode-stale-marker-codex-home"
        audncode_home = self.temp / "audncode-stale-marker-home"
        install_home.mkdir()
        (audncode_home / "sessions").mkdir(parents=True)
        settings_path = audncode_home / "settings.json"
        settings_path.write_text('{"model":"keep-stale","hooks":{}}\n', encoding="utf-8")
        (audncode_home / ".openclaude.json").write_text("{}\n", encoding="utf-8")
        # The PID is live, but startedAt is from an impossible prior lifetime.
        # This exercises PID-reuse protection rather than only a missing PID.
        (audncode_home / "sessions" / f"{os.getpid()}.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "sessionId": str(uuid.uuid4()),
                    "cwd": str(self.temp),
                    "startedAt": 1,
                    "kind": "interactive",
                    "entrypoint": "cli",
                }
            ),
            encoding="utf-8",
        )
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
                "-EnableAudnCode",
                "-AudnCodeHome",
                str(audncode_home),
            ],
            env={**os.environ, "CODEX_NTFY_TOPIC": "audncode-stale-marker-test-topic"},
            text=True,
            capture_output=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(installed["model"], "keep-stale")
        self.assertIn("-AudnCodeHook", settings_path.read_text(encoding="utf-8-sig"))

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_quiet_check_detects_writer_and_rolls_back_hooks(self) -> None:
        install_home = self.temp / "audncode-quiet-codex-home"
        audncode_home = self.temp / "audncode-quiet-home"
        install_home.mkdir()
        audncode_home.mkdir()
        settings_path = audncode_home / "settings.json"
        global_path = audncode_home / ".openclaude.json"
        original = {
            "model": "keep-quiet",
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "keep-me"}]}]},
        }
        settings_path.write_text(json.dumps(original, separators=(",", ":")), encoding="utf-8")
        global_path.write_text('{"numStartups":19}\n', encoding="utf-8")
        command = [
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
            "-EnableAudnCode",
            "-AudnCodeHome",
            str(audncode_home),
        ]
        process = subprocess.Popen(
            command,
            env={**os.environ, "CODEX_NTFY_TOPIC": "audncode-quiet-writer-test-topic"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        managed_seen = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            try:
                current = settings_path.read_text(encoding="utf-8-sig")
                managed_seen = "-AudnCodeHook" in current
            except OSError:
                managed_seen = False
            if managed_seen:
                concurrent = json.loads(current)
                concurrent["concurrentSetting"] = {"preserve": True}
                settings_path.write_text(
                    json.dumps(concurrent, separators=(",", ":")), encoding="utf-8"
                )
                break
            time.sleep(0.01)
        stdout, stderr = process.communicate(timeout=90)
        self.assertTrue(managed_seen, f"quiet window was not observable\n{stdout}\n{stderr}")
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertIn("quiet verification", stdout + stderr)
        rolled_back = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(rolled_back["model"], "keep-quiet")
        self.assertEqual(rolled_back["concurrentSetting"], {"preserve": True})
        self.assertEqual(rolled_back["hooks"], original["hooks"])
        self.assertEqual(json.loads(global_path.read_text(encoding="utf-8-sig")), {"numStartups": 19})

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_audncode_lock_and_field_rollback_preserve_concurrent_config(self) -> None:
        install_home = self.temp / "audncode-rollback-codex-home"
        audncode_home = self.temp / "audncode-rollback-home"
        install_home.mkdir()
        audncode_home.mkdir()
        original_settings = json.dumps(
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}},
            separators=(",", ":"),
        )
        original_global = json.dumps({"numStartups": 11}, separators=(",", ":"))
        settings_path = audncode_home / "settings.json"
        global_config_path = audncode_home / ".openclaude.json"
        settings_path.write_text(original_settings, encoding="utf-8")
        global_config_path.write_text(original_global, encoding="utf-8")
        (install_home / "ntfy-config.json").write_text(
            json.dumps({"server": "https://ntfy.sh", "topic": "test-topic", "tags": [42]}),
            encoding="utf-8",
        )
        command = [
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
            "-EnableAudnCode",
            "-AudnCodeHome",
            str(audncode_home),
        ]
        lock_path = Path(f"{global_config_path}.lock")
        lock_path.mkdir()
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        managed_hooks_seen = False
        waited_for_lock = False
        threshold_absent_while_locked = False
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    managed_hooks_seen = "-AudnCodeHook" in settings_path.read_text(
                        encoding="utf-8-sig"
                    )
                except OSError:
                    managed_hooks_seen = False
                if managed_hooks_seen:
                    break
                time.sleep(0.05)
            waited_for_lock = process.poll() is None
            if managed_hooks_seen and waited_for_lock:
                concurrent_settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
                concurrent_settings["concurrentSetting"] = {"preserve": True}
                settings_path.write_text(
                    json.dumps(concurrent_settings, separators=(",", ":")),
                    encoding="utf-8",
                )
                global_before_release = json.loads(
                    global_config_path.read_text(encoding="utf-8-sig")
                )
                threshold_absent_while_locked = (
                    "messageIdleNotifThresholdMs" not in global_before_release
                )
                global_config_path.write_text(
                    json.dumps(
                        {
                            "numStartups": 11,
                            "concurrentSession": {"preserve": True},
                        },
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
        finally:
            if lock_path.exists():
                lock_path.rmdir()
        stdout, stderr = process.communicate(timeout=90)
        self.assertTrue(
            managed_hooks_seen,
            f"installer did not reach the locked global config\nstdout={stdout}\nstderr={stderr}",
        )
        self.assertTrue(waited_for_lock, "installer did not wait for the proper-lockfile directory")
        self.assertTrue(threshold_absent_while_locked, "installer wrote through an active lock")
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        rolled_back_settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(rolled_back_settings["hooks"], json.loads(original_settings)["hooks"])
        self.assertEqual(rolled_back_settings["concurrentSetting"], {"preserve": True})
        self.assertEqual(
            json.loads(global_config_path.read_text(encoding="utf-8-sig")),
            {"numStartups": 11, "concurrentSession": {"preserve": True}},
        )
        self.assertFalse(lock_path.exists())

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_audncode_hook_marker_rollback_is_compare_and_swap(self) -> None:
        restored_path = self.temp / "marker-restored.json"
        restored_backup = self.temp / "marker-restored.backup.json"
        concurrent_path = self.temp / "marker-concurrent.json"
        created_path = self.temp / "marker-created.json"
        installed_content = "installed-generation\n"
        restored_path.write_text(installed_content, encoding="utf-8")
        restored_backup.write_text("previous-generation\n", encoding="utf-8")
        concurrent_path.write_text("newer-concurrent-generation\n", encoding="utf-8")
        created_path.write_text(installed_content, encoding="utf-8")
        script = r"""
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object Text.UTF8Encoding($false, $true)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Test-SafeUnicodeScalarText',
    'Assert-SafeUnicodeScalarText',
    'Assert-JsonUnicodeScalars',
    'ConvertFrom-StrictJsonText',
    'Read-StrictUtf8Text',
    'Read-StrictJsonFile',
    'Get-ObjectValue',
    'Write-TextAtomic',
    'Protect-PrivatePath',
    'Get-AudnCodeHookMarkerMutexName',
    'Enter-AudnCodeHookMarkerLock',
    'Exit-AudnCodeHookMarkerLock',
    'Invoke-WithAudnCodeHookMarkerLock',
    'Restore-AudnCodeHookObservationMarker'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}
$installedContent = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_MARKER_RESTORED)
$mutation = [pscustomobject]@{
  rotated = $true
  installed_content = $installedContent
  previous_present = $true
  previous_content = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_MARKER_RESTORED_BACKUP)
}
$createdMutation = [pscustomobject]@{
  rotated = $true
  installed_content = $installedContent
  previous_present = $false
  previous_content = ''
}
Restore-AudnCodeHookObservationMarker `
  -MarkerPath $env:CODEX_NTFY_TEST_MARKER_RESTORED `
  -MutationState $mutation
Restore-AudnCodeHookObservationMarker `
  -MarkerPath $env:CODEX_NTFY_TEST_MARKER_CONCURRENT `
  -MutationState $mutation
Restore-AudnCodeHookObservationMarker `
  -MarkerPath $env:CODEX_NTFY_TEST_MARKER_CREATED `
  -MutationState $createdMutation
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_TEST_MARKER_RESTORED": str(restored_path),
                "CODEX_NTFY_TEST_MARKER_RESTORED_BACKUP": str(restored_backup),
                "CODEX_NTFY_TEST_MARKER_CONCURRENT": str(concurrent_path),
                "CODEX_NTFY_TEST_MARKER_CREATED": str(created_path),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(restored_path.read_text(encoding="utf-8-sig"), "previous-generation\n")
        self.assertEqual(
            concurrent_path.read_text(encoding="utf-8-sig"),
            "newer-concurrent-generation\n",
        )
        self.assertFalse(created_path.exists())
        self.assertIn("preserved the newer value", result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_audncode_hook_marker_post_write_failure_rolls_back_inside_lock(self) -> None:
        existing_home = self.temp / "marker-fault-existing-home"
        created_home = self.temp / "marker-fault-created-home"
        existing_home.mkdir()
        created_home.mkdir()
        existing_marker = existing_home / ".codex-ntfy-hooks.json"
        created_marker = created_home / ".codex-ntfy-hooks.json"
        previous = (
            json.dumps(
                {
                    "schema": 1,
                    "kind": "codex-ntfy-audncode-hooks",
                    "notifier_version": "2.6.0",
                    "hook_shape_version": 8,
                    "audncode_home": str(existing_home.resolve()),
                    "generation": "b" * 32,
                    "installed_unix_ms": int(time.time() * 1000) - 1000,
                },
                indent=2,
            )
            + "\n"
        )
        existing_marker.write_text(previous, encoding="utf-8")
        previous_bytes = existing_marker.read_bytes()
        script = r"""
$ErrorActionPreference = 'Stop'
$NotifierVersion = '2.6.0'
$AudnCodeHookShapeVersion = 9
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object Text.UTF8Encoding($false, $true)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Test-SafeUnicodeScalarText',
    'Assert-SafeUnicodeScalarText',
    'Assert-JsonUnicodeScalars',
    'ConvertFrom-StrictJsonText',
    'Read-StrictUtf8Text',
    'Read-StrictJsonFile',
    'Write-TextAtomic',
    'Get-ObjectValue',
    'Get-AudnCodeHookObservationMarker',
    'Get-AudnCodeHookMarkerMutexName',
    'Enter-AudnCodeHookMarkerLock',
    'Exit-AudnCodeHookMarkerLock',
    'Invoke-WithAudnCodeHookMarkerLock',
    'Ensure-AudnCodeHookObservationMarker'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}

# Write-TextAtomic also protects its empty temporary file. Inject only on the
# final marker path, after the atomic write has returned.
function Protect-PrivatePath {
  param([string]$Path)
  $full = [IO.Path]::GetFullPath($Path)
  if ([string]::Equals($full, [IO.Path]::GetFullPath($env:CODEX_NTFY_TEST_EXISTING_MARKER), [StringComparison]::OrdinalIgnoreCase) -or
      [string]::Equals($full, [IO.Path]::GetFullPath($env:CODEX_NTFY_TEST_CREATED_MARKER), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'INJECTED_AFTER_MARKER_WRITE'
  }
}

$existingError = ''
$createdError = ''
try {
  [void](Ensure-AudnCodeHookObservationMarker `
      -HomePath $env:CODEX_NTFY_TEST_EXISTING_HOME `
      -MarkerPath $env:CODEX_NTFY_TEST_EXISTING_MARKER `
      -ManagedHookEventsChanged $true)
} catch { $existingError = $_.Exception.Message }
try {
  [void](Ensure-AudnCodeHookObservationMarker `
      -HomePath $env:CODEX_NTFY_TEST_CREATED_HOME `
      -MarkerPath $env:CODEX_NTFY_TEST_CREATED_MARKER `
      -ManagedHookEventsChanged $true)
} catch { $createdError = $_.Exception.Message }
[pscustomobject]@{
  existing_error = $existingError
  created_error = $createdError
  existing_content = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_EXISTING_MARKER)
  created_exists = Test-Path -LiteralPath $env:CODEX_NTFY_TEST_CREATED_MARKER
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_TEST_EXISTING_HOME": str(existing_home),
                "CODEX_NTFY_TEST_EXISTING_MARKER": str(existing_marker),
                "CODEX_NTFY_TEST_CREATED_HOME": str(created_home),
                "CODEX_NTFY_TEST_CREATED_MARKER": str(created_marker),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertIn("INJECTED_AFTER_MARKER_WRITE", summary["existing_error"])
        self.assertIn("INJECTED_AFTER_MARKER_WRITE", summary["created_error"])
        self.assertTrue(summary["existing_content"])
        self.assertEqual(existing_marker.read_bytes(), previous_bytes)
        self.assertFalse(summary["created_exists"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_audncode_hook_marker_rotation_restores_immediate_previous_generation(self) -> None:
        audncode_home = self.temp / "marker-interleaving-home"
        audncode_home.mkdir()
        marker_path = audncode_home / ".codex-ntfy-hooks.json"
        preflight_x = json.dumps({"generation": "x" * 32}) + "\n"
        current_y = (
            json.dumps(
                {
                    "schema": 1,
                    "kind": "codex-ntfy-audncode-hooks",
                    "notifier_version": "2.6.0",
                    "hook_shape_version": 8,
                    "audncode_home": str(audncode_home.resolve()),
                    "generation": "a" * 32,
                    "installed_unix_ms": int(time.time() * 1000) - 1000,
                },
                indent=2,
            )
            + "\n"
        )
        (self.temp / "preflight-marker-x.json").write_bytes(preflight_x.encode("utf-8"))
        marker_path.write_bytes(current_y.encode("utf-8"))
        script = r"""
$ErrorActionPreference = 'Stop'
$NotifierVersion = '2.6.0'
$AudnCodeHookShapeVersion = 9
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object Text.UTF8Encoding($false, $true)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Test-SafeUnicodeScalarText',
    'Assert-SafeUnicodeScalarText',
    'Assert-JsonUnicodeScalars',
    'ConvertFrom-StrictJsonText',
    'Read-StrictUtf8Text',
    'Read-StrictJsonFile',
    'Write-TextAtomic',
    'Protect-PrivatePath',
    'Get-ObjectValue',
    'Get-AudnCodeHookObservationMarker',
    'Get-AudnCodeHookMarkerMutexName',
    'Enter-AudnCodeHookMarkerLock',
    'Exit-AudnCodeHookMarkerLock',
    'Invoke-WithAudnCodeHookMarkerLock',
    'Ensure-AudnCodeHookObservationMarker',
    'Restore-AudnCodeHookObservationMarker'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}
$state = Ensure-AudnCodeHookObservationMarker `
  -HomePath $env:CODEX_NTFY_TEST_AUDN_HOME `
  -MarkerPath $env:CODEX_NTFY_TEST_MARKER `
  -ManagedHookEventsChanged $true
$rotated = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_MARKER)
Restore-AudnCodeHookObservationMarker `
  -MarkerPath $env:CODEX_NTFY_TEST_MARKER `
  -MutationState $state
[pscustomobject]@{
  generation = $state.generation
  captured_previous = $state.previous_content
  installed_matches = [string]::Equals($rotated, $state.installed_content, [StringComparison]::Ordinal)
  restored = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_MARKER)
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_TEST_AUDN_HOME": str(audncode_home),
                "CODEX_NTFY_TEST_MARKER": str(marker_path),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertRegex(summary["generation"], r"^[a-f0-9]{32}$")
        self.assertNotEqual(summary["generation"], "a" * 32)
        self.assertTrue(summary["installed_matches"])
        self.assertEqual(summary["captured_previous"], current_y)
        self.assertEqual(summary["restored"], current_y)

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_audncode_hook_marker_lock_serializes_compare_replace_window(self) -> None:
        marker_path = self.temp / "marker-lock-race.json"
        signal_path = self.temp / "marker-lock-held.signal"
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Get-AudnCodeHookMarkerMutexName',
    'Enter-AudnCodeHookMarkerLock',
    'Exit-AudnCodeHookMarkerLock',
    'Invoke-WithAudnCodeHookMarkerLock'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  Invoke-Expression $definition.Extent.Text
}
if ($env:CODEX_NTFY_TEST_MARKER_ROLE -eq 'compare-replace') {
  [void](Invoke-WithAudnCodeHookMarkerLock -MarkerPath $env:CODEX_NTFY_TEST_MARKER -Action {
      [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_MARKER_SIGNAL, 'held')
      Start-Sleep -Milliseconds 750
      [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_MARKER, 'restored-by-first')
    })
} else {
  [void](Invoke-WithAudnCodeHookMarkerLock -MarkerPath $env:CODEX_NTFY_TEST_MARKER -Action {
      [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_MARKER, 'newer-by-second')
    })
}
"""
        command = [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]
        base_env = {
            **os.environ,
            "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
            "CODEX_NTFY_TEST_MARKER": str(marker_path),
            "CODEX_NTFY_TEST_MARKER_SIGNAL": str(signal_path),
        }
        first = subprocess.Popen(
            command,
            env={**base_env, "CODEX_NTFY_TEST_MARKER_ROLE": "compare-replace"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 15
        while not signal_path.exists() and time.monotonic() < deadline and first.poll() is None:
            time.sleep(0.02)
        self.assertTrue(signal_path.exists(), "first marker writer never acquired the shared lock")
        second = subprocess.run(
            command,
            env={**base_env, "CODEX_NTFY_TEST_MARKER_ROLE": "newer-writer"},
            text=True,
            capture_output=True,
            timeout=30,
        )
        first_stdout, first_stderr = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, first_stdout + first_stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(marker_path.read_text(encoding="utf-8"), "newer-by-second")

    def _assert_audncode_transaction_lock_scenario(self, first_outcome: str) -> None:
        scenario = self.temp / f"audncode-transaction-{first_outcome}"
        scenario.mkdir()
        codex_home = scenario / "shared-codex-home"
        profile_a = scenario / "audn-profile-a"
        profile_b = scenario / "audn-profile-b"
        codex_home.mkdir()
        profile_a.mkdir()
        profile_b.mkdir()
        shared_path = codex_home / "shared-install-state.txt"
        marker_a = profile_a / ".codex-ntfy-hooks.json"
        marker_b = profile_b / ".codex-ntfy-hooks.json"
        signal_path = scenario / "first-held.signal"
        observed_path = scenario / "second-observed.txt"
        shared_path.write_text("initial", encoding="utf-8")
        script = r"""
$ErrorActionPreference = 'Stop'
$TaskName = 'CodexNtfyWatcher'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Get-InstallerTransactionMutexName',
    'Enter-InstallerTransactionLock',
    'Exit-InstallerTransactionLock',
    'Get-AudnCodeHookMarkerMutexName',
    'Enter-AudnCodeHookMarkerLock',
    'Exit-AudnCodeHookMarkerLock'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}
$transaction = Enter-InstallerTransactionLock `
  -ScheduledTaskName $TaskName `
  -TimeoutSeconds 15
$profileMarker = if ($env:CODEX_NTFY_TEST_TRANSACTION_ROLE -eq 'first') {
  $env:CODEX_NTFY_TEST_PROFILE_A_MARKER
} else {
  $env:CODEX_NTFY_TEST_PROFILE_B_MARKER
}
$profileLock = $null
try {
  $profileLock = Enter-AudnCodeHookMarkerLock -MarkerPath $profileMarker -TimeoutSeconds 15
  if ($env:CODEX_NTFY_TEST_TRANSACTION_ROLE -eq 'first') {
    [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_SHARED_STATE, 'first-success')
    [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_TRANSACTION_SIGNAL, 'held')
    Start-Sleep -Milliseconds 900
    if ($env:CODEX_NTFY_TEST_FIRST_OUTCOME -eq 'rollback') {
      [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_SHARED_STATE, 'initial')
    }
  } else {
    $observed = [IO.File]::ReadAllText($env:CODEX_NTFY_TEST_SHARED_STATE)
    [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_TRANSACTION_OBSERVED, $observed)
    # This is the second profile's successful shared CodexHome commit.
    [IO.File]::WriteAllText($env:CODEX_NTFY_TEST_SHARED_STATE, 'second-success')
  }
} finally {
  try {
    if ($null -ne $profileLock) { Exit-AudnCodeHookMarkerLock -Mutex $profileLock }
  } finally {
    Exit-InstallerTransactionLock -Mutex $transaction
  }
}
"""
        command = [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]
        base_env = {
            **os.environ,
            "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
            "CODEX_NTFY_TEST_SHARED_STATE": str(shared_path),
            "CODEX_NTFY_TEST_PROFILE_A_MARKER": str(marker_a),
            "CODEX_NTFY_TEST_PROFILE_B_MARKER": str(marker_b),
            "CODEX_NTFY_TEST_TRANSACTION_SIGNAL": str(signal_path),
            "CODEX_NTFY_TEST_TRANSACTION_OBSERVED": str(observed_path),
            "CODEX_NTFY_TEST_FIRST_OUTCOME": first_outcome,
        }
        first = subprocess.Popen(
            command,
            env={**base_env, "CODEX_NTFY_TEST_TRANSACTION_ROLE": "first"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            while not signal_path.exists() and time.monotonic() < deadline and first.poll() is None:
                time.sleep(0.02)
            self.assertTrue(signal_path.exists(), "first installer never acquired its transaction lock")
            second_started = time.monotonic()
            second = subprocess.run(
                command,
                env={**base_env, "CODEX_NTFY_TEST_TRANSACTION_ROLE": "second"},
                text=True,
                capture_output=True,
                timeout=30,
            )
            second_elapsed = time.monotonic() - second_started
            first_stdout, first_stderr = first.communicate(timeout=30)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_stdout + first_stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertGreaterEqual(second_elapsed, 0.35, "second installer did not wait for resolution")
        if first_outcome == "success":
            self.assertEqual(observed_path.read_text(encoding="utf-8"), "first-success")
        else:
            self.assertEqual(observed_path.read_text(encoding="utf-8"), "initial")
        self.assertEqual(
            shared_path.read_text(encoding="utf-8"),
            "second-success",
            "the first profile rollback overwrote the later successful profile install",
        )

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_audncode_transaction_lock_delays_idempotent_adoption_until_success(self) -> None:
        self._assert_audncode_transaction_lock_scenario("success")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_audncode_transaction_lock_finishes_rollback_before_next_success(self) -> None:
        self._assert_audncode_transaction_lock_scenario("rollback")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_audncode_threshold_rollback_is_field_level_compare_and_swap(self) -> None:
        changed_path = self.temp / "audncode-cas-changed.json"
        installed_path = self.temp / "audncode-cas-installed.json"
        created_path = self.temp / "audncode-cas-created.json"
        changed_path.write_text(
            json.dumps(
                {
                    "messageIdleNotifThresholdMs": 2500,
                    "concurrent": {"keep": True},
                }
            ),
            encoding="utf-8",
        )
        installed_path.write_text(
            json.dumps(
                {
                    "messageIdleNotifThresholdMs": 1000,
                    "concurrent": {"keep": True},
                }
            ),
            encoding="utf-8",
        )
        created_path.write_text(
            json.dumps({"messageIdleNotifThresholdMs": 1000}),
            encoding="utf-8",
        )
        script = r"""
$ErrorActionPreference = 'Stop'
$WarningPreference = 'SilentlyContinue'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object System.Text.UTF8Encoding($false, $true)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Test-SafeUnicodeScalarText',
    'Assert-SafeUnicodeScalarText',
    'Assert-JsonUnicodeScalars',
    'ConvertFrom-StrictJsonText',
    'Read-StrictUtf8Text',
    'Write-TextAtomic',
    'Get-ObjectValue',
    'Invoke-WithAudnCodeGlobalConfigLock',
    'Test-AudnCodeThresholdNumber',
    'Restore-AudnCodeIdleThreshold'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  if ($null -eq $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}
$mutation = [pscustomobject]@{
  Applied = $true
  FilePreviouslyPresent = $true
  HadProperty = $true
  PreviousValue = 5000
  InstalledValue = 1000
}
$createdMutation = [pscustomobject]@{
  Applied = $true
  FilePreviouslyPresent = $false
  HadProperty = $false
  PreviousValue = $null
  InstalledValue = 1000
}
Restore-AudnCodeIdleThreshold `
  -GlobalConfigPath $env:CODEX_NTFY_TEST_CHANGED_CONFIG `
  -MutationState $mutation
Restore-AudnCodeIdleThreshold `
  -GlobalConfigPath $env:CODEX_NTFY_TEST_INSTALLED_CONFIG `
  -MutationState $mutation
Restore-AudnCodeIdleThreshold `
  -GlobalConfigPath $env:CODEX_NTFY_TEST_CREATED_CONFIG `
  -MutationState $createdMutation
$changed = Get-Content -LiteralPath $env:CODEX_NTFY_TEST_CHANGED_CONFIG -Raw -Encoding UTF8 | ConvertFrom-Json
$installed = Get-Content -LiteralPath $env:CODEX_NTFY_TEST_INSTALLED_CONFIG -Raw -Encoding UTF8 | ConvertFrom-Json
[pscustomobject]@{
  changed_threshold = $changed.messageIdleNotifThresholdMs
  changed_concurrent = $changed.concurrent.keep
  installed_threshold = $installed.messageIdleNotifThresholdMs
  installed_concurrent = $installed.concurrent.keep
  created_exists = Test-Path -LiteralPath $env:CODEX_NTFY_TEST_CREATED_CONFIG
  changed_lock_exists = Test-Path -LiteralPath ($env:CODEX_NTFY_TEST_CHANGED_CONFIG + '.lock')
  installed_lock_exists = Test-Path -LiteralPath ($env:CODEX_NTFY_TEST_INSTALLED_CONFIG + '.lock')
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_TEST_CHANGED_CONFIG": str(changed_path),
                "CODEX_NTFY_TEST_INSTALLED_CONFIG": str(installed_path),
                "CODEX_NTFY_TEST_CREATED_CONFIG": str(created_path),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["changed_threshold"], 2500)
        self.assertTrue(summary["changed_concurrent"])
        self.assertEqual(summary["installed_threshold"], 5000)
        self.assertTrue(summary["installed_concurrent"])
        self.assertFalse(summary["created_exists"])
        self.assertFalse(summary["changed_lock_exists"])
        self.assertFalse(summary["installed_lock_exists"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_global_config_path_matches_effective_oauth_profile(self) -> None:
        cases = {
            "config": self.temp / "audn-profile-config",
            "custom": self.temp / "audn-profile-custom",
            "local": self.temp / "audn-profile-local",
            "staging": self.temp / "audn-profile-staging",
            "prod": self.temp / "audn-profile-prod",
        }
        for directory in cases.values():
            directory.mkdir()
        (cases["config"] / ".config.json").write_text("{}", encoding="utf-8")
        (cases["custom"] / ".openclaude-custom-oauth.json").write_text("{}", encoding="utf-8")
        (cases["local"] / ".claude-local-oauth.json").write_text("{}", encoding="utf-8")
        (cases["staging"] / ".openclaude-staging-oauth.json").write_text("{}", encoding="utf-8")
        (cases["prod"] / ".openclaude.json").write_text("{}", encoding="utf-8")

        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @('Test-AudnCodeEnvironmentTruthy', 'Resolve-AudnCodeGlobalConfigPath')) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  Invoke-Expression $definition.Extent.Text
}

$env:CLAUDE_CODE_CUSTOM_OAUTH_URL = 'https://claude.fedstart.com'
$env:USER_TYPE = 'ant'
$env:USE_LOCAL_OAUTH = 'yes'
$env:USE_STAGING_OAUTH = 'on'
$config = Resolve-AudnCodeGlobalConfigPath -HomePath $env:CODEX_NTFY_PROFILE_CONFIG
$custom = Resolve-AudnCodeGlobalConfigPath -HomePath $env:CODEX_NTFY_PROFILE_CUSTOM

$env:CLAUDE_CODE_CUSTOM_OAUTH_URL = $null
$local = Resolve-AudnCodeGlobalConfigPath -HomePath $env:CODEX_NTFY_PROFILE_LOCAL
$env:USE_LOCAL_OAUTH = 'off'
$staging = Resolve-AudnCodeGlobalConfigPath -HomePath $env:CODEX_NTFY_PROFILE_STAGING
$env:USER_TYPE = 'customer'
$env:USE_STAGING_OAUTH = $null
$prod = Resolve-AudnCodeGlobalConfigPath -HomePath $env:CODEX_NTFY_PROFILE_PROD

[pscustomobject]@{
  config = [IO.Path]::GetFileName($config)
  custom = [IO.Path]::GetFileName($custom)
  local = [IO.Path]::GetFileName($local)
  staging = [IO.Path]::GetFileName($staging)
  prod = [IO.Path]::GetFileName($prod)
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                **{f"CODEX_NTFY_PROFILE_{name.upper()}": str(path) for name, path in cases.items()},
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["config"], ".config.json")
        self.assertEqual(summary["custom"], ".openclaude-custom-oauth.json")
        self.assertEqual(summary["local"], ".claude-local-oauth.json")
        self.assertEqual(summary["staging"], ".openclaude-staging-oauth.json")
        self.assertEqual(summary["prod"], ".openclaude.json")

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_hook_rollback_recreates_removed_event_and_preserves_concurrent_state(self) -> None:
        settings_path = self.temp / "audncode-hook-cas.json"
        script_path = self.temp / "notify-ntfy.ps1"
        old_command = f"& '{str(script_path).replace(chr(39), chr(39) * 2)}' -AudnCodeHook"
        original = {
            "model": "preserve",
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "keep"}]}],
                "StopFailure": [
                    {
                        "hooks": [
                            {"type": "command", "command": old_command, "shell": "powershell"}
                        ]
                    }
                ],
            },
        }
        settings_path.write_text(json.dumps(original), encoding="utf-8")
        script = r"""
$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Utf8StrictNoBom = New-Object System.Text.UTF8Encoding($false, $true)
$AudnCodeSettingsQuietWindowMilliseconds = 750
$AudnCodeSessionMarkerMaxBytes = 64 * 1024
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Test-SafeUnicodeScalarText',
    'Assert-SafeUnicodeScalarText',
    'Assert-JsonUnicodeScalars',
    'ConvertFrom-StrictJsonText',
    'Read-StrictUtf8Text',
    'Read-StrictJsonFile',
    'Write-TextAtomic',
    'Protect-PrivatePath',
    'Get-ObjectValue',
    'ConvertTo-PowerShellSingleQuotedLiteral',
    'Test-ManagedAudnCodeHookHandler',
    'Get-LiveAudnCodeSessionMarkers',
    'Assert-NoLiveAudnCodeSessions',
    'Assert-AudnCodeSettingsQuiet',
    'Ensure-AudnCodeHooks',
    'Restore-AudnCodeHooks'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  Invoke-Expression $definition.Extent.Text
}

$mutation = $null
Ensure-AudnCodeHooks `
  -SettingsPath $env:CODEX_NTFY_TEST_SETTINGS `
  -PowerShellPath $env:CODEX_NTFY_TEST_POWERSHELL `
  -ScriptPath $env:CODEX_NTFY_TEST_SCRIPT `
  -AudnCodeHomePath $env:CODEX_NTFY_TEST_HOME `
  -MutationState ([ref]$mutation)
$concurrent = Get-Content -LiteralPath $env:CODEX_NTFY_TEST_SETTINGS -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Member -InputObject $concurrent -MemberType NoteProperty -Name 'concurrent' -Value ([pscustomobject]@{ keep = $true })
[IO.File]::WriteAllText(
  $env:CODEX_NTFY_TEST_SETTINGS,
  (($concurrent | ConvertTo-Json -Depth 100) + [Environment]::NewLine),
  $Utf8NoBom
)
Restore-AudnCodeHooks -SettingsPath $env:CODEX_NTFY_TEST_SETTINGS -MutationState $mutation
Get-Content -LiteralPath $env:CODEX_NTFY_TEST_SETTINGS -Raw -Encoding UTF8
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_TEST_SETTINGS": str(settings_path),
                "CODEX_NTFY_TEST_POWERSHELL": str(WINDOWS_POWERSHELL),
                "CODEX_NTFY_TEST_SCRIPT": str(script_path),
                "CODEX_NTFY_TEST_HOME": str(self.temp),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = json.loads(result.stdout)
        self.maxDiff = None
        self.assertEqual(restored["model"], "preserve")
        self.assertEqual(restored["hooks"], original["hooks"])
        self.assertEqual(restored["concurrent"], {"keep": True})

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_global_lock_heartbeats_and_never_deletes_replacement(self) -> None:
        first_config = self.temp / "heartbeat-config.json"
        replacement_config = self.temp / "replacement-config.json"
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
$definition = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq 'Invoke-WithAudnCodeGlobalConfigLock'
  }, $true))[0]
Invoke-Expression $definition.Extent.Text

$heartbeat = Invoke-WithAudnCodeGlobalConfigLock `
  -GlobalConfigPath $env:CODEX_NTFY_HEARTBEAT_CONFIG `
  -TimeoutMs 5000 `
  -Action {
    $lockPath = $env:CODEX_NTFY_HEARTBEAT_CONFIG + '.lock'
    $first = (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc.Ticks
    Start-Sleep -Milliseconds 2600
    $second = (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc.Ticks
    [pscustomobject]@{ advanced = $second -gt $first }
  }
$released = -not (Test-Path -LiteralPath ($env:CODEX_NTFY_HEARTBEAT_CONFIG + '.lock'))

$ownershipError = $null
try {
  Invoke-WithAudnCodeGlobalConfigLock `
    -GlobalConfigPath $env:CODEX_NTFY_REPLACEMENT_CONFIG `
    -TimeoutMs 5000 `
    -Action {
      $lockPath = $env:CODEX_NTFY_REPLACEMENT_CONFIG + '.lock'
      Remove-Item -LiteralPath $lockPath -Force -ErrorAction Stop
      New-Item -ItemType Directory -Path $lockPath -ErrorAction Stop | Out-Null
    } | Out-Null
} catch {
  $ownershipError = $_.Exception.Message
}
$replacementPath = $env:CODEX_NTFY_REPLACEMENT_CONFIG + '.lock'
$replacementPreserved = Test-Path -LiteralPath $replacementPath -PathType Container
if ($replacementPreserved) { Remove-Item -LiteralPath $replacementPath -Force }

[pscustomobject]@{
  heartbeat_advanced = [bool]$heartbeat.advanced
  released = $released
  ownership_failed = -not [string]::IsNullOrWhiteSpace($ownershipError)
  replacement_preserved = $replacementPreserved
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_HEARTBEAT_CONFIG": str(first_config),
                "CODEX_NTFY_REPLACEMENT_CONFIG": str(replacement_config),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(summary["heartbeat_advanced"])
        self.assertTrue(summary["released"])
        self.assertTrue(summary["ownership_failed"])
        self.assertTrue(summary["replacement_preserved"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_global_lock_recovers_stale_and_preserves_active_replacement(self) -> None:
        stale_config = self.temp / "stale-recovery-config.json"
        active_config = self.temp / "active-lock-config.json"
        replacement_config = self.temp / "stale-replacement-config.json"
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
foreach ($name in @(
    'Try-RecoverStaleAudnCodeGlobalConfigLock',
    'Invoke-WithAudnCodeGlobalConfigLock'
  )) {
  $definition = @($ast.FindAll({
      param($node)
      $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))[0]
  Invoke-Expression $definition.Extent.Text
}
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;

namespace CodexNtfyTest {
  public static class LockReplacement {
    public static Timer ReplaceAfter(string path, int delayMs) {
      return new Timer(_ => {
        try {
          if (Directory.Exists(path)) Directory.Delete(path, false);
          Directory.CreateDirectory(path);
          Directory.SetLastWriteTimeUtc(path, DateTime.UtcNow);
        } catch { }
      }, null, delayMs, Timeout.Infinite);
    }
  }
}
'@

$staleLock = $env:CODEX_NTFY_STALE_CONFIG + '.lock'
[IO.Directory]::CreateDirectory($staleLock) | Out-Null
[IO.Directory]::SetLastWriteTimeUtc($staleLock, [DateTime]::UtcNow.AddSeconds(-30))
$script:staleActionCalled = $false
Invoke-WithAudnCodeGlobalConfigLock `
  -GlobalConfigPath $env:CODEX_NTFY_STALE_CONFIG `
  -TimeoutMs 5000 `
  -Action { $script:staleActionCalled = $true } | Out-Null
$staleReleased = -not (Test-Path -LiteralPath $staleLock)
$staleQuarantineCount = @(
  Get-ChildItem -LiteralPath (Split-Path -Parent $staleLock) `
    -Filter ((Split-Path -Leaf $staleLock) + '.recover-*') `
    -Force -ErrorAction SilentlyContinue
).Count

$activeLock = $env:CODEX_NTFY_ACTIVE_CONFIG + '.lock'
[IO.Directory]::CreateDirectory($activeLock) | Out-Null
[IO.Directory]::SetLastWriteTimeUtc($activeLock, [DateTime]::UtcNow)
$script:activeActionCalled = $false
$activeError = $null
try {
  Invoke-WithAudnCodeGlobalConfigLock `
    -GlobalConfigPath $env:CODEX_NTFY_ACTIVE_CONFIG `
    -TimeoutMs 1100 `
    -Action { $script:activeActionCalled = $true } | Out-Null
} catch { $activeError = $_.Exception.Message }
$activePreserved = Test-Path -LiteralPath $activeLock -PathType Container

$replacementLock = $env:CODEX_NTFY_STALE_REPLACEMENT_CONFIG + '.lock'
[IO.Directory]::CreateDirectory($replacementLock) | Out-Null
[IO.Directory]::SetLastWriteTimeUtc($replacementLock, [DateTime]::UtcNow.AddSeconds(-30))
$replacementTimer = [CodexNtfyTest.LockReplacement]::ReplaceAfter($replacementLock, 40)
$script:replacementActionCalled = $false
$replacementError = $null
try {
  Invoke-WithAudnCodeGlobalConfigLock `
    -GlobalConfigPath $env:CODEX_NTFY_STALE_REPLACEMENT_CONFIG `
    -TimeoutMs 1100 `
    -Action { $script:replacementActionCalled = $true } | Out-Null
} catch { $replacementError = $_.Exception.Message }
$replacementTimer.Dispose()
$replacementPreserved = Test-Path -LiteralPath $replacementLock -PathType Container

[pscustomobject]@{
  stale_action_called = $script:staleActionCalled
  stale_released = $staleReleased
  stale_quarantine_count = $staleQuarantineCount
  active_action_called = $script:activeActionCalled
  active_timed_out = $activeError -match 'Timed out'
  active_preserved = $activePreserved
  replacement_action_called = $script:replacementActionCalled
  replacement_timed_out = $replacementError -match 'Timed out'
  replacement_preserved = $replacementPreserved
} | ConvertTo-Json -Compress

if ($activePreserved) { [IO.Directory]::Delete($activeLock, $false) }
if ($replacementPreserved) { [IO.Directory]::Delete($replacementLock, $false) }
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER),
                "CODEX_NTFY_STALE_CONFIG": str(stale_config),
                "CODEX_NTFY_ACTIVE_CONFIG": str(active_config),
                "CODEX_NTFY_STALE_REPLACEMENT_CONFIG": str(replacement_config),
            },
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(summary["stale_action_called"])
        self.assertTrue(summary["stale_released"])
        self.assertEqual(summary["stale_quarantine_count"], 0)
        self.assertFalse(summary["active_action_called"])
        self.assertTrue(summary["active_timed_out"])
        self.assertTrue(summary["active_preserved"])
        self.assertFalse(summary["replacement_action_called"])
        self.assertTrue(summary["replacement_timed_out"])
        self.assertTrue(summary["replacement_preserved"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "AudnCode Windows test")
    def test_audncode_settings_and_global_rollbacks_are_independent(self) -> None:
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:CODEX_NTFY_TEST_INSTALLER,
  [ref]$tokens,
  [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
$definition = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq 'Restore-AudnCodeConfiguration'
  }, $true))[0]
Invoke-Expression $definition.Extent.Text

$script:settingsCalls = 0
$script:globalCalls = 0
$script:settingsFail = $true
$script:globalFail = $false
function Restore-AudnCodeHooks {
  param([string]$SettingsPath, [object]$MutationState)
  $script:settingsCalls++
  if ($script:settingsFail) { throw 'settings failure' }
}
function Restore-AudnCodeIdleThreshold {
  param([string]$GlobalConfigPath, [object]$MutationState)
  $script:globalCalls++
  if ($script:globalFail) { throw 'global failure' }
}

$firstError = $null
try {
  Restore-AudnCodeConfiguration -SettingsPath 'settings' -GlobalConfigPath 'global' `
    -SettingsMutation ([pscustomobject]@{}) -GlobalConfigMutation ([pscustomobject]@{})
} catch { $firstError = $_.Exception.Message }
$script:settingsFail = $false
$script:globalFail = $true
$secondError = $null
try {
  Restore-AudnCodeConfiguration -SettingsPath 'settings' -GlobalConfigPath 'global' `
    -SettingsMutation ([pscustomobject]@{}) -GlobalConfigMutation ([pscustomobject]@{})
} catch { $secondError = $_.Exception.Message }

[pscustomobject]@{
  settings_calls = $script:settingsCalls
  global_calls = $script:globalCalls
  first_error = $firstError
  second_error = $secondError
} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [
                str(WINDOWS_POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            env={**os.environ, "CODEX_NTFY_TEST_INSTALLER": str(INSTALLER)},
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["settings_calls"], 2)
        self.assertEqual(summary["global_calls"], 2)
        self.assertIn("settings failure", summary["first_error"])
        self.assertIn("global failure", summary["second_error"])

    @unittest.skipUnless(os.name == "nt" and WINDOWS_POWERSHELL.exists(), "Windows installer test")
    def test_windows_installer_restores_claude_settings_after_late_failure(self) -> None:
        install_home = self.temp / "claude-rollback-codex-home"
        claude_home = self.temp / "claude-rollback-home"
        install_home.mkdir()
        claude_home.mkdir()
        original_settings = json.dumps(
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}},
            separators=(",", ":"),
        )
        settings_path = claude_home / "settings.json"
        settings_path.write_text(original_settings, encoding="utf-8")
        (install_home / "ntfy-config.json").write_text(
            json.dumps({"server": "https://ntfy.sh", "topic": "test-topic", "tags": [42]}),
            encoding="utf-8",
        )
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
                "-EnableClaudeCode",
                "-ClaudeHome",
                str(claude_home),
            ],
            text=True,
            capture_output=True,
            timeout=90,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(settings_path.read_text(encoding="utf-8-sig"), original_settings)

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
