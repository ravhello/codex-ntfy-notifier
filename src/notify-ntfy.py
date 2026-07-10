#!/usr/bin/env python3
"""Durable ntfy delivery hook for Codex on Linux, WSL, and Remote SSH."""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # Windows fallback, useful for validation and Windows SSH hosts
    fcntl = None  # type: ignore[assignment]
    import msvcrt


VERSION = "2.3.0"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def unix_ms() -> int:
    return time.time_ns() // 1_000_000


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, value: Any, *, no_overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(compact_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        if no_overwrite:
            os.link(temp, path)
            temp.unlink()
        else:
            os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def obj_value(value: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(value, dict):
        return default
    for name in names:
        candidate = value.get(name)
        if candidate is not None and candidate != "":
            return candidate
    return default


def sanitize(text: Any, max_length: int = 900, *, preserve_lines: bool = False) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if preserve_lines:
        value = re.sub(r"[\t\f\v ]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
    else:
        value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(
        r"(?i)\b(authorization)\s*[:=]\s*(?:bearer|basic)\s+\S+",
        r"\1=[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\b(password|passwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,})\b", "[REDACTED]", value)
    value = re.sub(r"(?i)https://ntfy\.sh/[A-Za-z0-9._~-]+", "https://ntfy.sh/[REDACTED]", value)
    if len(value) > max_length:
        return value[: max(0, max_length - 3)] + "..."
    return value


class Runtime:
    def __init__(self) -> None:
        self.codex_home = Path(os.environ.get("CODEX_HOME") or Path(__file__).resolve().parent)
        self.config_path = Path(os.environ.get("CODEX_NTFY_CONFIG") or self.codex_home / "ntfy-config.json")
        self.state_root = Path(os.environ.get("CODEX_NTFY_STATE_DIR") or self.codex_home / "ntfy-state")
        self.outbox = self.state_root / "outbox"
        self.sent = self.state_root / "sent"
        self.suppressed = self.state_root / "suppressed"
        self.dead = self.state_root / "dead"
        self.lock_path = self.state_root / "worker.lock"
        self.log_path = self.state_root / "notify.log"

    def ensure(self) -> None:
        for path in (self.state_root, self.outbox, self.sent, self.suppressed, self.dead):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                path.chmod(0o700)

    def log(self, message: str) -> None:
        try:
            self.ensure()
            if self.log_path.exists() and self.log_path.stat().st_size > 1_048_576:
                rotated = self.log_path.with_suffix(self.log_path.suffix + ".1")
                with contextlib.suppress(FileNotFoundError):
                    rotated.unlink()
                self.log_path.replace(rotated)
            stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message}\n")
        except OSError:
            pass


def load_config(runtime: Runtime) -> dict[str, Any]:
    file_config: dict[str, Any] = {}
    if runtime.config_path.exists():
        loaded = read_json(runtime.config_path)
        if not isinstance(loaded, dict):
            raise ValueError(f"invalid config object: {runtime.config_path}")
        file_config = loaded

    def setting(env_name: str, key: str, default: Any = "") -> Any:
        return os.environ.get(env_name) or file_config.get(key, default)

    tags = file_config.get("tags", ["computer", "white_check_mark"])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    return {
        "server": str(setting("CODEX_NTFY_SERVER", "server", "https://ntfy.sh")).rstrip("/"),
        "topic": str(setting("CODEX_NTFY_TOPIC", "topic", "")).strip("/"),
        "token": str(setting("CODEX_NTFY_TOKEN", "token", "")),
        "username": str(setting("CODEX_NTFY_USER", "username", "")),
        "password": str(setting("CODEX_NTFY_PASSWORD", "password", "")),
        "allow_insecure_auth": bool(file_config.get("allow_insecure_auth", False)),
        "priority": int(file_config.get("priority", 3)),
        "tags": list(tags),
        "max_message_chars": int(file_config.get("max_message_chars", 900)),
        "include_message": bool(file_config.get("include_message", False)),
        "include_thread_title": bool(file_config.get("include_thread_title", False)),
        "markdown": bool(file_config.get("markdown", True)),
        "include_full_path": bool(file_config.get("include_full_path", False)),
        "suppress_subagents": bool(file_config.get("suppress_subagents", True)),
        "subagent_classification_grace_seconds": float(file_config.get("subagent_classification_grace_seconds", 8)),
        "timeout_seconds": int(file_config.get("timeout_seconds", 12)),
        "max_attempts": int(file_config.get("max_attempts", 0)),
        "retry_max_seconds": float(file_config.get("retry_max_seconds", 900)),
        "sent_retention_days": int(file_config.get("sent_retention_days", 14)),
        "dead_retention_days": int(file_config.get("dead_retention_days", 30)),
    }


def project_name(cwd: str) -> str:
    normalized = str(cwd or "").strip().rstrip("/\\").replace("\\", "/")
    if re.fullmatch(r"[A-Za-z]:", normalized):
        return normalized
    parts = [part for part in normalized.split("/") if part]
    return parts[-1] if parts else "workspace"


def thread_title(runtime: Runtime, thread_id: str, session_home: str = "") -> str:
    if not thread_id:
        return ""
    index = Path(session_home or runtime.codex_home) / "session_index.jsonl"
    if not index.exists():
        return ""
    title = ""
    try:
        with index.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if thread_id not in line:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    item = json.loads(line)
                    if item.get("id") == thread_id and item.get("thread_name"):
                        title = str(item["thread_name"])
    except OSError:
        return ""
    return title


def event_classification(runtime: Runtime, event: dict[str, Any], thread_id: str, session_home: str = "") -> str:
    source = event.get("source")
    if isinstance(source, dict) and source.get("subagent") is not None:
        return "subagent"
    if any(event.get(name) is True or str(event.get(name, "")).lower() == "true" for name in ("is-subagent", "is_subagent")):
        return "subagent"
    if any(event.get(name) not in (None, "") for name in ("parent-thread-id", "parent_thread_id")):
        return "subagent"
    if not thread_id:
        return "unknown"
    codex_home = Path(session_home or runtime.codex_home)
    for root_name in ("sessions", "archived_sessions"):
        root = codex_home / root_name
        if not root.exists():
            continue
        try:
            matches = root.rglob(f"*{thread_id}*.jsonl")
            session = next(matches, None)
            if session is None:
                continue
            with session.open("r", encoding="utf-8", errors="replace") as handle:
                metadata = json.loads(handle.readline())
            session_source = obj_value(obj_value(metadata, "payload", default={}), "source", default={})
            if isinstance(session_source, dict) and session_source.get("subagent") is not None:
                return "subagent"
            return "root"
        except (OSError, json.JSONDecodeError):
            continue
    return "unknown"


def parse_event(raw: str) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def new_record(
    event: dict[str, Any],
    origin: str,
    session_home: str,
    classification: str,
    include_message: bool,
) -> dict[str, Any]:
    thread_id = str(obj_value(event, "thread-id", "thread_id", default=""))
    turn_id = str(obj_value(event, "turn-id", "turn_id", default=""))
    weak = not thread_id or not turn_id
    identity = f"codex-ntfy/v1|{thread_id}|{turn_id}" if not weak else f"codex-ntfy/v1|weak|{uuid.uuid4().hex}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    now = utc_now()
    stored_event = {
        "type": "agent-turn-complete",
        "cwd": sanitize(obj_value(event, "cwd", "working-directory", "working_directory", default=""), 1000),
        "last-assistant-message": (
            sanitize(
                obj_value(event, "last-assistant-message", "last_assistant_message", default=""),
                4000,
                preserve_lines=True,
            )
            if include_message
            else ""
        ),
    }
    return {
        "schema": 1,
        "key": key,
        "sequence_id": f"codex-{key[:32]}",
        "weak_identity": weak,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "origin": origin,
        "session_codex_home": session_home,
        "session_classification": classification,
        "created_at": now.isoformat(),
        "created_unix_ms": unix_ms(),
        "next_attempt_unix_ms": unix_ms(),
        "attempts": 0,
        "last_error": None,
        "event": stored_event,
    }


def enqueue(runtime: Runtime, record: dict[str, Any]) -> str:
    runtime.ensure()
    sent_path = runtime.sent / f"{record['key']}.json"
    suppressed_path = runtime.suppressed / f"{record['key']}.json"
    dead_path = runtime.dead / f"{record['key']}.json"
    outbox_path = runtime.outbox / f"{record['key']}.json"
    if sent_path.exists():
        runtime.log(f"deduplicated sent event key={record['key'][:12]}")
        return "sent"
    if suppressed_path.exists():
        runtime.log(f"deduplicated suppressed event key={record['key'][:12]}")
        return "suppressed"
    if dead_path.exists():
        runtime.log(f"deduplicated dead event key={record['key'][:12]}")
        return "dead"
    if outbox_path.exists():
        runtime.log(f"deduplicated queued event key={record['key'][:12]}")
        return "queued"
    try:
        atomic_write_json(outbox_path, record, no_overwrite=True)
    except FileExistsError:
        return "queued"
    runtime.log(f"queued event key={record['key'][:12]} origin={record['origin']}")
    return "queued"


def write_suppressed_receipt(runtime: Runtime, record: dict[str, Any]) -> None:
    receipt = runtime.suppressed / f"{record['key']}.json"
    try:
        atomic_write_json(
            receipt,
            {
                "schema": 1,
                "key": record["key"],
                "thread_id": record["thread_id"],
                "turn_id": record["turn_id"],
                "origin": record["origin"],
                "suppressed_at": utc_now().isoformat(),
                "reason": "subagent",
            },
            no_overwrite=True,
        )
    except FileExistsError:
        pass
    (runtime.outbox / f"{record['key']}.json").unlink(missing_ok=True)


def ntfy_payload(runtime: Runtime, record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    event = record["event"]
    cwd = str(obj_value(event, "cwd", "working-directory", "working_directory", default=""))
    project = sanitize(project_name(cwd), 50)
    title = project
    if config["include_thread_title"]:
        title = sanitize(thread_title(runtime, record.get("thread_id", ""), record.get("session_codex_home", "")), 58) or project
    last_message = sanitize(
        obj_value(event, "last-assistant-message", "last_assistant_message", default=""),
        config["max_message_chars"],
        preserve_lines=True,
    ) or "Turn completed."
    location = f"Folder: {sanitize(cwd, 180)}" if config["include_full_path"] and cwd else f"Project: {project}"
    metadata = [location, f"Source: {record['origin']}"]
    if record.get("thread_id"):
        metadata.append(f"Thread: {record['thread_id'][:8]}")
    return {
        "topic": config["topic"],
        "title": f"Codex finished - {title}",
        "message": last_message + "\n\n" + " | ".join(metadata),
        "tags": config["tags"],
        "priority": config["priority"],
        "markdown": config["markdown"],
        "sequence_id": record["sequence_id"],
    }


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def publish(runtime: Runtime, record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not config["topic"]:
        raise RuntimeError(f"ntfy topic is not configured in {runtime.config_path}")
    if not config["server"]:
        raise RuntimeError("ntfy server is empty")
    parsed_server = urllib.parse.urlsplit(config["server"])
    has_username = bool(config["username"])
    has_password = bool(config["password"])
    if not config["token"] and has_username != has_password:
        raise RuntimeError("basic authentication requires both username and password")
    uses_auth = bool(config["token"] or (config["username"] and config["password"]))
    is_loopback = (parsed_server.hostname or "").lower() in ("localhost", "127.0.0.1", "::1")
    if uses_auth and parsed_server.scheme != "https" and not is_loopback and not config["allow_insecure_auth"]:
        raise RuntimeError("refusing to send ntfy credentials over an insecure connection; use HTTPS or set allow_insecure_auth")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"codex-ntfy-notifier/{VERSION}",
    }
    if config["token"]:
        headers["Authorization"] = f"Bearer {config['token']}"
    elif config["username"] and config["password"]:
        encoded = base64.b64encode(f"{config['username']}:{config['password']}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    request = urllib.request.Request(
        config["server"],
        data=compact_json(ntfy_payload(runtime, record, config)).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirectHandler)
    with opener.open(request, timeout=config["timeout_seconds"]) as response:
        body = response.read().decode("utf-8", errors="replace")
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")
    with contextlib.suppress(json.JSONDecodeError):
        decoded = json.loads(body)
        if isinstance(decoded, dict):
            return decoded
    return {}


def retry_delay(attempt: int, base: float, maximum: float, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return max(0.05, min(maximum, retry_after))
    ceiling = min(maximum, base * (2 ** min(10, max(0, attempt - 1))))
    return max(0.05, ceiling * random.uniform(0.6, 1.0))


def clean_runtime_state(runtime: Runtime, receipt_retention_days: int, dead_retention_days: int) -> None:
    cutoff = time.time() - max(1, receipt_retention_days) * 86400
    for directory in (runtime.sent, runtime.suppressed):
        for path in directory.glob("*.json"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
    dead_cutoff = time.time() - max(1, dead_retention_days) * 86400
    for path in runtime.dead.glob("*.json"):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < dead_cutoff:
                path.unlink()


def validate_record(record: Any, expected_key: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("queue item is not an object")
    if record.get("schema") != 1:
        raise ValueError("queue item has an unsupported schema")
    key = record.get("key")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or key != expected_key:
        raise ValueError("queue item has an invalid key")
    sequence_id = record.get("sequence_id")
    if not isinstance(sequence_id, str) or not re.fullmatch(r"codex-[0-9a-f]{32}", sequence_id):
        raise ValueError("queue item has an invalid sequence ID")
    if not isinstance(record.get("event"), dict):
        raise ValueError("queue item has an invalid event")
    if int(record.get("created_unix_ms", 0)) < 0 or int(record.get("attempts", -1)) < 0:
        raise ValueError("queue item has invalid counters")
    return record


def worker(
    runtime: Runtime,
    *,
    continuous: bool,
    poll_seconds: float,
    retry_base_seconds: float,
) -> int:
    runtime.ensure()
    lock_handle = runtime.lock_path.open("a+b")
    lock_acquired = False
    try:
        try:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                lock_handle.seek(0, os.SEEK_END)
                if lock_handle.tell() == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            lock_acquired = True
        except (BlockingIOError, OSError):
            return 0
        cleaned = False
        runtime.log(f"worker started continuous={continuous}")
        while True:
            config = load_config(runtime)
            if not cleaned:
                clean_runtime_state(runtime, config["sent_retention_days"], config["dead_retention_days"])
                cleaned = True
            now = unix_ms()
            next_due: int | None = None
            for path in sorted(runtime.outbox.glob("*.json"), key=lambda item: (item.stat().st_mtime_ns, item.name)):
                try:
                    record = validate_record(read_json(path), path.stem)
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    dead = {"schema": 1, "key": path.stem, "last_error": sanitize(exc, 300), "failed_at": utc_now().isoformat()}
                    atomic_write_json(runtime.dead / path.name, dead)
                    path.unlink(missing_ok=True)
                    runtime.log(f"dead-letter invalid event key={path.stem[:12]}")
                    continue
                receipt = runtime.sent / f"{record['key']}.json"
                suppressed_receipt = runtime.suppressed / f"{record['key']}.json"
                if receipt.exists():
                    path.unlink(missing_ok=True)
                    continue
                if suppressed_receipt.exists():
                    path.unlink(missing_ok=True)
                    continue
                due = int(record.get("next_attempt_unix_ms") or 0)
                if due > now:
                    next_due = due if next_due is None else min(next_due, due)
                    continue
                if config["suppress_subagents"]:
                    classification = str(record.get("session_classification", "unknown"))
                    if classification not in ("root", "subagent"):
                        classification = event_classification(
                            runtime,
                            record.get("event") if isinstance(record.get("event"), dict) else {},
                            str(record.get("thread_id", "")),
                            str(record.get("session_codex_home", "")),
                        )
                    if classification == "subagent":
                        atomic_write_json(
                            suppressed_receipt,
                            {
                                "schema": 1,
                                "key": record["key"],
                                "thread_id": record.get("thread_id", ""),
                                "turn_id": record.get("turn_id", ""),
                                "origin": record.get("origin", ""),
                                "suppressed_at": utc_now().isoformat(),
                                "reason": "subagent",
                            },
                        )
                        path.unlink(missing_ok=True)
                        runtime.log(f"suppressed subagent event key={record['key'][:12]} thread={str(record.get('thread_id', ''))[:8]}")
                        continue
                    if classification == "unknown":
                        created = int(record.get("created_unix_ms") or now)
                        classification_due = created + int(max(0, config["subagent_classification_grace_seconds"]) * 1000)
                        if classification_due > now:
                            next_due = classification_due if next_due is None else min(next_due, classification_due)
                            continue
                retry_after: float | None = None
                try:
                    response = publish(runtime, record, config)
                    atomic_write_json(
                        receipt,
                        {
                            "schema": 1,
                            "key": record["key"],
                            "sequence_id": record["sequence_id"],
                            "thread_id": record.get("thread_id", ""),
                            "turn_id": record.get("turn_id", ""),
                            "origin": record.get("origin", ""),
                            "sent_at": utc_now().isoformat(),
                            "ntfy_id": response.get("id", ""),
                        },
                    )
                    path.unlink(missing_ok=True)
                    runtime.log(f"sent event key={record['key'][:12]} origin={record.get('origin', '')}")
                    continue
                except urllib.error.HTTPError as exc:
                    if exc.headers and exc.headers.get("Retry-After"):
                        with contextlib.suppress(ValueError):
                            retry_after = float(exc.headers["Retry-After"])
                    error = f"HTTP {exc.code}"
                    permanent = 300 <= exc.code < 400 or (
                        400 <= exc.code < 500 and exc.code not in (401, 403, 408, 409, 425, 429)
                    )
                except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
                    error = sanitize(exc, 400)
                    permanent = False

                record["attempts"] = int(record.get("attempts") or 0) + 1
                record["last_error"] = error
                if permanent or (config["max_attempts"] > 0 and record["attempts"] >= config["max_attempts"]):
                    atomic_write_json(runtime.dead / path.name, record)
                    path.unlink(missing_ok=True)
                    runtime.log(f"dead-letter event key={record['key'][:12]} attempts={record['attempts']} error={error}")
                    continue
                delay = retry_delay(record["attempts"], retry_base_seconds, config["retry_max_seconds"], retry_after)
                record["next_attempt_unix_ms"] = unix_ms() + int(delay * 1000)
                atomic_write_json(path, record)
                runtime.log(f"retry event key={record['key'][:12]} attempt={record['attempts']} in={delay:.2f}s error={error}")
                next_due = record["next_attempt_unix_ms"] if next_due is None else min(next_due, record["next_attempt_unix_ms"])

            remaining = sum(1 for _ in runtime.outbox.glob("*.json"))
            if not continuous and remaining == 0:
                return 0
            sleep_for = max(0.1, poll_seconds)
            if next_due is not None:
                sleep_for = min(sleep_for, max(0.1, (next_due - unix_ms()) / 1000))
            time.sleep(sleep_for)
    finally:
        if lock_acquired:
            with contextlib.suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                else:
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        lock_handle.close()
        runtime.log("worker stopped")


def start_worker(args: argparse.Namespace, runtime: Runtime) -> None:
    if args.no_spawn or os.environ.get("CODEX_NTFY_NO_SPAWN") == "1":
        return
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--poll-seconds",
        str(args.poll_seconds),
    ]
    with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as output:
        subprocess.Popen(command, stdin=stdin, stdout=output, stderr=output, start_new_session=True, close_fds=True)


def doctor(runtime: Runtime) -> int:
    runtime.ensure()
    config = load_config(runtime)
    print(
        json.dumps(
            {
                "version": VERSION,
                "codex_home": str(runtime.codex_home),
                "config_path": str(runtime.config_path),
                "config_exists": runtime.config_path.exists(),
                "server": config["server"],
                "topic_configured": bool(config["topic"]),
                "auth_mode": (
                    "token"
                    if config["token"]
                    else "invalid"
                    if bool(config["username"]) != bool(config["password"])
                    else "basic"
                    if config["username"]
                    else "anonymous"
                ),
                "state_dir": str(runtime.state_root),
                "queued": sum(1 for _ in runtime.outbox.glob("*.json")),
                "sent_receipts": sum(1 for _ in runtime.sent.glob("*.json")),
                "suppressed": sum(1 for _ in runtime.suppressed.glob("*.json")),
                "dead_letters": sum(1 for _ in runtime.dead.glob("*.json")),
                "dead_retention_days": config["dead_retention_days"],
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notification", nargs="*")
    parser.add_argument("--origin")
    parser.add_argument("--session-classification", choices=("root", "subagent", "unknown"), default="unknown")
    parser.add_argument("--read-stdin", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--kick-worker", action="store_true", help="Start an on-demand worker only when the outbox is nonempty")
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--no-spawn", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--classify", action="store_true", help="Classify one hook payload without queueing or network access")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--retry-base-seconds", type=float, default=5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = Runtime()
    try:
        runtime.ensure()
        if args.doctor:
            return doctor(runtime)
        if args.kick_worker:
            if any(runtime.outbox.glob("*.json")):
                start_worker(args, runtime)
            if not args.classify:
                return 0
        if args.worker or args.continuous:
            return worker(
                runtime,
                continuous=args.continuous,
                poll_seconds=args.poll_seconds,
                retry_base_seconds=args.retry_base_seconds,
            )
        if args.test:
            raw = compact_json(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "00000000-0000-4000-8000-000000000001",
                    "turn-id": str(uuid.uuid4()),
                    "cwd": os.getcwd(),
                    "last-assistant-message": "Test codex-ntfy-notifier: delivery, queueing, and deduplication work.",
                }
            )
        elif args.read_stdin or (not args.notification and not sys.stdin.isatty()):
            raw = sys.stdin.read()
        else:
            raw = " ".join(args.notification)
        event = parse_event(raw)
        if not event:
            if raw.strip():
                runtime.log("ignored malformed notify payload")
            return 0
        if event.get("type", "agent-turn-complete") != "agent-turn-complete":
            return 0
        thread_id = str(obj_value(event, "thread-id", "thread_id", default=""))
        detected_classification = event_classification(runtime, event, thread_id)
        classification = (
            "subagent"
            if detected_classification == "subagent"
            else args.session_classification
            if args.session_classification in ("root", "subagent")
            else detected_classification
        )
        if args.classify:
            print(classification)
            return 0
        config = load_config(runtime)
        origin = args.origin or os.environ.get("CODEX_NTFY_ORIGIN") or os.environ.get("WSL_DISTRO_NAME") or platform.node() or "Linux"
        record = new_record(event, origin, str(runtime.codex_home), classification, config["include_message"])
        if config["suppress_subagents"] and classification == "subagent":
            write_suppressed_receipt(runtime, record)
            runtime.log(f"suppressed subagent completion thread={thread_id[:8]}")
            return 0
        enqueue(runtime, record)
        start_worker(args, runtime)
        if args.test:
            deadline = time.monotonic() + 30
            receipt = runtime.sent / f"{record['key']}.json"
            while time.monotonic() < deadline:
                if receipt.exists():
                    print("Test notification delivered.")
                    return 0
                time.sleep(0.25)
            print(f"Test notification queued; delivery is still pending. Check {runtime.log_path}")
            return 2
        return 0
    except Exception as exc:  # hook boundary: log safely, return non-zero for bridge fallback
        runtime.log(f"hook error: {sanitize(exc, 500)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
