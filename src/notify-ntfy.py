#!/usr/bin/env python3
"""Durable ntfy delivery hook for Codex on Linux, WSL, and Remote SSH."""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
from pathlib import Path
from typing import Any

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # Windows fallback, useful for validation and Windows SSH hosts
    fcntl = None  # type: ignore[assignment]
    import msvcrt


VERSION = "2.6.0"
MAX_NTFY_MESSAGE_BYTES = 3500
MAX_NTFY_TITLE_BYTES = 240
SESSION_INDEX_TAIL_BYTES = 4 * 1024 * 1024
TITLE_JSONL_LINE_BYTES = 256 * 1024
FIRST_METADATA_LINE_BYTES = 1024 * 1024
MAX_FALLBACK_ROLLOUT_LINE_BYTES = 8 * 1024 * 1024
SYNTHETIC_TEST_THREAD_ID = "00000000-0000-4000-8000-000000000001"
CHATGPT_TASK_URL_PREFIX = "https://chatgpt.com/codex/tasks/"

BIDI_CONTROL_CODEPOINTS = {
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
}
ROLLOUT_WATCH_MAX_READ_BYTES = 8 * 1024 * 1024
SESSION_INDEX_MAX_BYTES = 4 * 1024 * 1024


def rollout_watch_max_read_bytes() -> int:
    """Return the production ceiling, allowing tests to shrink it only."""
    if os.environ.get("CODEX_NTFY_NO_SPAWN") == "1":
        with contextlib.suppress(ValueError):
            value = int(os.environ.get("CODEX_NTFY_TEST_WATCH_MAX_BYTES", "0"))
            if 256 <= value < ROLLOUT_WATCH_MAX_READ_BYTES:
                return value
    return ROLLOUT_WATCH_MAX_READ_BYTES


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def unix_ms() -> int:
    return time.time_ns() // 1_000_000


def safe_unicode_scalar_text(value: str) -> bool:
    return "\ufffd" not in value and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def validate_json_strings(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("JSON nesting exceeds the validation limit")
    if isinstance(value, str):
        if not safe_unicode_scalar_text(value):
            raise ValueError("JSON contains an invalid Unicode scalar or replacement character")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and not safe_unicode_scalar_text(key):
                raise ValueError("JSON contains an invalid Unicode object key")
            validate_json_strings(item, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_json_strings(item, depth + 1)


def strict_json_loads(value: str) -> Any:
    try:
        if not safe_unicode_scalar_text(value):
            raise ValueError("JSON text contains an invalid Unicode scalar or replacement character")
        parsed = json.loads(value)
        validate_json_strings(parsed)
        return parsed
    except (ValueError, RecursionError) as error:
        if isinstance(error, json.JSONDecodeError):
            raise
        raise json.JSONDecodeError(str(error), value, 0) from error


def compact_json(value: Any) -> str:
    validate_json_strings(value)
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if not safe_unicode_scalar_text(rendered):
        raise ValueError("serialized JSON contains an invalid Unicode scalar")
    return rendered


def read_json(path: Path) -> Any:
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise json.JSONDecodeError("file is not strict UTF-8", "", 0) from error
    return strict_json_loads(text)


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


def _is_grapheme_extension(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character) in ("Mn", "Mc", "Me")
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0020 <= codepoint <= 0xE007F
    )


def notification_clusters(value: str) -> list[str]:
    """Approximate extended grapheme clusters consistently with PowerShell 5.1."""
    if not safe_unicode_scalar_text(value):
        return []
    clusters: list[str] = []
    current = ""
    pending_join = False
    regional_count = 0

    def flush() -> None:
        nonlocal current, pending_join, regional_count
        current = current.rstrip("\u200c\u200d")
        if current:
            clusters.append(current)
        current = ""
        pending_join = False
        regional_count = 0

    for character in value:
        codepoint = ord(character)
        if _is_grapheme_extension(character):
            if current:
                current += character
            continue
        if codepoint in (0x200C, 0x200D):
            if current:
                current += character
                pending_join = True
            continue
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        if not current:
            current = character
            regional_count = 1 if regional else 0
        elif pending_join:
            current += character
            pending_join = False
            regional_count = 0
        elif regional and regional_count == 1:
            current += character
            regional_count = 2
        else:
            flush()
            current = character
            regional_count = 1 if regional else 0
    flush()
    return clusters


def truncate_text(value: str, max_length: int) -> str:
    """Truncate at a readable Unicode boundary without splitting a word when possible."""
    if not safe_unicode_scalar_text(value):
        return ""
    clusters = notification_clusters(value)
    if len(clusters) <= max_length:
        return value
    if max_length <= 1:
        return "…"[:max_length]
    keep = max_length - 1
    boundary = -1
    for index in range(keep - 1, -1, -1):
        if clusters[index].isspace():
            boundary = index
            break
    if boundary >= int(keep * 0.7):
        keep = boundary
    return "".join(clusters[:keep]).rstrip() + "…"


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Fit text into a byte budget while keeping valid UTF-8."""
    if not safe_unicode_scalar_text(value):
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix = "…"
    budget = max(0, max_bytes - len(suffix.encode("utf-8")))
    prefix_parts: list[str] = []
    used = 0
    for cluster in notification_clusters(value):
        cluster_bytes = len(cluster.encode("utf-8"))
        if used + cluster_bytes > budget:
            break
        prefix_parts.append(cluster)
        used += cluster_bytes
    prefix = "".join(prefix_parts).rstrip()
    return prefix + suffix if max_bytes >= len(suffix.encode("utf-8")) else ""


def normalize_display_text(text: Any) -> str:
    value = str(text or "")
    if not safe_unicode_scalar_text(value):
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    cleaned = "".join(
        character
        for character in value
        if not (
            (ord(character) < 0x20 and character not in ("\t", "\n"))
            or 0x7F <= ord(character) <= 0x9F
            or ord(character) in BIDI_CONTROL_CODEPOINTS
            or ord(character) == 0xFEFF
        )
    )
    return unicodedata.normalize("NFC", cleaned)


def sanitize(text: Any, max_length: int = 900, *, preserve_lines: bool = False) -> str:
    value = normalize_display_text(text)
    if not value:
        return ""
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
    return truncate_text(value, max_length)


def markdown_to_plain_text(text: Any) -> str:
    """Render the small Markdown subset used in completion summaries as compact text."""
    value = normalize_display_text(text)
    if not value:
        return ""
    literals: list[str] = []

    namespace = ""
    for nonce in range(32):
        digest = hashlib.sha256(
            f"codex-ntfy-markdown-literal/v1|{nonce}|{value}".encode("utf-8")
        ).hexdigest()
        left = chr(0xE000 + int(digest[:4], 16) % 6400)
        right = chr(0xE000 + int(digest[4:8], 16) % 6400)
        if right == left:
            right = chr(0xE000 + ((ord(right) - 0xE000 + 1) % 6400))
        candidate = left + digest[8:24] + right
        if candidate not in value:
            namespace = candidate
            break
    if not namespace:
        return value

    def protect(literal: str) -> str:
        token = f"{namespace}{len(literals)}{namespace}"
        literals.append(literal)
        return token

    prepared_lines: list[str] = []
    inside_fence = False
    for raw_line in value.split("\n"):
        if re.match(r"^\s{0,3}(?:`{3,}|~{3,})", raw_line):
            inside_fence = not inside_fence
            continue
        prepared_lines.append(protect(raw_line) if inside_fence and raw_line.strip() else raw_line)
    value = "\n".join(prepared_lines)
    value = re.sub(r"`([^`\r\n]+)`", lambda match: protect(match.group(1)), value)
    value = re.sub(r"\\([^\w\s])", lambda match: protect(match.group(1)), value)

    value = re.sub(r"!\[([^\]]*)\]\([^\r\n)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^\r\n)]*\)", r"\1", value)
    value = re.sub(r"!\[([^\]]*)\]\[[^\]\r\n]*\]", r"\1", value)
    value = re.sub(r"(?<!!)\[([^\]]+)\]\[[^\]\r\n]*\]", r"\1", value)
    value = re.sub(r"<(https?://[^>\s]+)>", r"\1", value, flags=re.IGNORECASE)

    plain_lines: list[str] = []
    for raw_line in value.split("\n"):
        line = raw_line
        if re.match(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", line):
            continue
        if re.match(r"^\s{0,3}(?:[-*_]\s*){3,}$", line):
            continue
        if re.match(r"^\s{0,3}\[[^\]]+\]:\s+\S+", line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^(?:(?:\s{0,3}>\s?)|(?:\s*[-*+]\s+)|(?:\s*\d+[.)]\s+))+", "", line)
        stripped = line.strip()
        looks_like_table = stripped.startswith("|") or stripped.endswith("|") or " | " in line
        if looks_like_table and "|" in line:
            cells = [cell.strip() for cell in stripped.strip("|").split("|") if cell.strip()]
            line = " · ".join(cells)
        line = line.strip()
        if line:
            plain_lines.append(line)

    value = " · ".join(plain_lines)
    for _ in range(2):
        value = re.sub(r"\*\*(.+?)\*\*", r"\1", value)
        value = re.sub(r"__(.+?)__", r"\1", value)
        value = re.sub(r"~~(.+?)~~", r"\1", value)
        value = re.sub(r"(?<![\w*])\*([^*\r\n]+)\*(?![\w*])", r"\1", value)
        value = re.sub(r"(?<![\w_])_([^_\r\n]+)_(?![\w_])", r"\1", value)
    for index, literal in enumerate(literals):
        value = value.replace(f"{namespace}{index}{namespace}", literal)
    return value.strip()


class Runtime:
    def __init__(self) -> None:
        self.codex_home = Path(os.environ.get("CODEX_HOME") or Path(__file__).resolve().parent)
        self.sqlite_home = Path(os.environ.get("CODEX_SQLITE_HOME") or self.codex_home)
        self.config_path = Path(os.environ.get("CODEX_NTFY_CONFIG") or self.codex_home / "ntfy-config.json")
        self.state_root = Path(os.environ.get("CODEX_NTFY_STATE_DIR") or self.codex_home / "ntfy-state")
        self.pending = self.state_root / "pending"
        self.outbox = self.state_root / "outbox"
        self.sent = self.state_root / "sent"
        self.suppressed = self.state_root / "suppressed"
        self.dead = self.state_root / "dead"
        self.watch = self.state_root / "watch"
        self.mutation_locks = self.state_root / "mutation-locks"
        self.lock_path = self.state_root / "worker.lock"
        self.delivery_lock_path = self.state_root / "delivery.lock"
        self.worker_health_path = self.state_root / "worker-health.json"
        self.delivery_health_path = self.state_root / "delivery-health.json"
        self.scan_lock_path = self.state_root / "watch-scan.lock"
        self.scan_health_path = self.state_root / "watch-health.json"
        self.log_path = self.state_root / "notify.log"
        self.last_watch_discovery_ms = 0
        self.watch_discovery_cache: dict[str, Path] = {}
        self.watch_force_replay_paths: set[str] = set()
        self.last_watch_cursor_refresh_ms = 0
        self.watch_cursor_cache: dict[str, Path] = {}
        self.cold_discovery_ran_this_scan = False
        self.watch_bytes_read = 0
        self.watch_backlog_files = 0
        self.watch_truncated_replays = 0
        self.watch_corrupt_files = 0

    def ensure(self) -> None:
        for path in (
            self.state_root,
            self.pending,
            self.outbox,
            self.sent,
            self.suppressed,
            self.dead,
            self.watch,
            self.mutation_locks,
        ):
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


@contextlib.contextmanager
def record_mutation_lock(runtime: Runtime, key: str) -> Any:
    """Serialize same-key hook/worker mutations across processes."""
    runtime.ensure()
    shard = key[:2] if re.fullmatch(r"[0-9a-f]{64}", key) else "weak"
    handle = (runtime.mutation_locks / f"{shard}.lock").open("a+b")
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        locked = True
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def load_config(runtime: Runtime) -> dict[str, Any]:
    file_config: dict[str, Any] = {}
    if runtime.config_path.exists():
        loaded = read_json(runtime.config_path)
        if not isinstance(loaded, dict):
            raise ValueError(f"invalid config object: {runtime.config_path}")
        file_config = loaded

    def setting(env_name: str, key: str, default: Any = "") -> Any:
        return os.environ.get(env_name) or file_config.get(key, default)

    raw_tags = file_config.get("tags", ["white_check_mark"])
    if raw_tags is None:
        raw_tags = []
    if isinstance(raw_tags, str):
        tag_items: list[Any] = raw_tags.split(",")
    elif isinstance(raw_tags, list):
        tag_items = list(raw_tags)
    else:
        raise ValueError("tags must be an array of strings or a comma-separated string")
    valid_tags: list[str] = []
    invalid_tag = False
    for item in tag_items:
        if not isinstance(item, str) or not safe_unicode_scalar_text(item):
            invalid_tag = True
            continue
        tag = unicodedata.normalize("NFC", item.strip())
        clusters = notification_clusters(tag)
        if (
            not tag
            or len(clusters) > 32
            or "".join(clusters) != tag
            or any(character.isspace() for character in tag)
            or any(
                ord(character) < 0x20
                or 0x7F <= ord(character) <= 0x9F
                or ord(character) in BIDI_CONTROL_CODEPOINTS
                or ord(character) == 0xFEFF
                for character in tag
            )
        ):
            invalid_tag = True
            continue
        valid_tags.append(tag)
    if len(tag_items) > 1 or invalid_tag:
        runtime.log("normalized ntfy tags to one valid status tag")
    tags = [valid_tags[0] if valid_tags else "white_check_mark"]
    priority = int(file_config.get("priority", 3))
    if not 1 <= priority <= 5:
        raise ValueError("priority must be between 1 and 5")
    max_message_chars = int(file_config.get("max_message_chars", 180))
    if not 32 <= max_message_chars <= 3000:
        raise ValueError("max_message_chars must be between 32 and 3000")
    idle_detection_mode = str(file_config.get("idle_detection_mode", "strict")).strip().lower()
    if idle_detection_mode not in ("strict", "balanced", "off"):
        raise ValueError("idle_detection_mode must be strict, balanced, or off")
    return {
        "server": str(setting("CODEX_NTFY_SERVER", "server", "https://ntfy.sh")).rstrip("/"),
        "topic": str(setting("CODEX_NTFY_TOPIC", "topic", "")).strip("/"),
        "token": str(setting("CODEX_NTFY_TOKEN", "token", "")),
        "username": str(setting("CODEX_NTFY_USER", "username", "")),
        "password": str(setting("CODEX_NTFY_PASSWORD", "password", "")),
        "allow_insecure_auth": bool(file_config.get("allow_insecure_auth", False)),
        "priority": priority,
        "tags": list(tags),
        "max_message_chars": max_message_chars,
        "include_message": bool(file_config.get("include_message", False)),
        "include_thread_title": bool(file_config.get("include_thread_title", False)),
        "include_task_link": bool(file_config.get("include_task_link", False)),
        "include_task_link_action": bool(file_config.get("include_task_link_action", False)),
        "markdown": bool(file_config.get("markdown", False)),
        "include_full_path": bool(file_config.get("include_full_path", False)),
        "suppress_subagents": bool(file_config.get("suppress_subagents", True)),
        "subagent_classification_grace_seconds": float(file_config.get("subagent_classification_grace_seconds", 8)),
        "idle_detection_mode": idle_detection_mode,
        "idle_grace_seconds": float(file_config.get("idle_grace_seconds", 1.5)),
        "idle_probe_grace_seconds": float(file_config.get("idle_probe_grace_seconds", 30)),
        "unknown_retry_max_seconds": float(file_config.get("unknown_retry_max_seconds", 60)),
        "goal_aware": bool(file_config.get("goal_aware", True)),
        "goal_poll_seconds": float(file_config.get("goal_poll_seconds", 1)),
        "subagent_orphan_seconds": float(file_config.get("subagent_orphan_seconds", 1800)),
        "suppress_technical_turns": bool(file_config.get("suppress_technical_turns", True)),
        "watch_rollouts": bool(file_config.get("watch_rollouts", True)),
        "watch_scan_seconds": float(file_config.get("watch_scan_seconds", 2)),
        "watch_discovery_seconds": float(file_config.get("watch_discovery_seconds", 60)),
        "watch_initial_replay_seconds": float(file_config.get("watch_initial_replay_seconds", 15)),
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


def safe_server_display(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "invalid"
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "invalid"


def _decode_jsonl_line(
    raw: bytes, *, allow_bom: bool = False, max_bytes: int = TITLE_JSONL_LINE_BYTES
) -> Any:
    if len(raw) > max_bytes:
        raise json.JSONDecodeError("JSONL line exceeds the title metadata limit", "", 0)
    try:
        text = raw.decode("utf-8-sig" if allow_bom else "utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise json.JSONDecodeError("JSONL line is not strict UTF-8", "", 0) from error
    return strict_json_loads(text.rstrip("\r"))


def read_first_json_line(path: Path, max_bytes: int = FIRST_METADATA_LINE_BYTES) -> Any:
    with path.open("rb") as handle:
        raw = handle.readline(max_bytes + 2)
    terminated = raw.endswith(b"\n")
    content = raw[:-1] if terminated else raw
    if len(content) > max_bytes or (len(raw) == max_bytes + 2 and not terminated):
        raise json.JSONDecodeError("first JSONL line exceeds the metadata limit", "", 0)
    return _decode_jsonl_line(content, allow_bom=True, max_bytes=max_bytes)


def thread_title(runtime: Runtime, thread_id: str, session_home: str = "", sqlite_home: str = "") -> str:
    if not thread_id:
        return ""
    _, database_title = sqlite_scalar(
        state_database_path(Path(sqlite_home or session_home or runtime.sqlite_home)),
        "SELECT COALESCE(title, '') FROM threads WHERE id = ? LIMIT 1",
        thread_id,
    )
    if database_title:
        return database_title
    index = Path(session_home or runtime.codex_home) / "session_index.jsonl"
    if not index.exists():
        return ""
    title = ""
    try:
        with index.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - SESSION_INDEX_TAIL_BYTES)
            handle.seek(start)
            data = handle.read(SESSION_INDEX_TAIL_BYTES)
        if start > 0:
            boundary = data.find(b"\n")
            data = data[boundary + 1 :] if boundary >= 0 else b""
        for index_number, raw_line in enumerate(data.splitlines()):
            if thread_id.encode("ascii", errors="ignore") not in raw_line:
                continue
            try:
                item = _decode_jsonl_line(raw_line, allow_bom=start == 0 and index_number == 0)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("id") == thread_id and item.get("thread_name"):
                candidate = item["thread_name"]
                if isinstance(candidate, str) and safe_unicode_scalar_text(candidate):
                    title = candidate
    except OSError:
        pass
    return title


def sqlite_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        text = bytes(value).decode("utf-8", errors="strict")
    else:
        text = str(value)
    if not safe_unicode_scalar_text(text):
        raise ValueError("SQLite text contains an invalid Unicode scalar or replacement character")
    return text


def sqlite_scalar(database: Path, sql: str, parameter: str) -> tuple[bool, str]:
    """Read one scalar without ever creating or mutating a Codex database."""
    if not database.is_file():
        return False, ""
    connection: sqlite3.Connection | None = None
    try:
        quoted = urllib.parse.quote(database.as_posix(), safe="/:")
        connection = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=0.5)
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(sql, (parameter,)).fetchone()
        return True, "" if row is None else sqlite_text(row[0])
    except (OSError, sqlite3.Error, UnicodeError, ValueError):
        return False, ""
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()


def sqlite_rows(database: Path, sql: str, parameter: str) -> tuple[bool, list[tuple[str, ...]]]:
    """Read string rows without creating or mutating a Codex database."""
    if not database.is_file():
        return False, []
    connection: sqlite3.Connection | None = None
    try:
        quoted = urllib.parse.quote(database.as_posix(), safe="/:")
        connection = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=0.5)
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(sql, (parameter,)).fetchall()
        return True, [tuple(sqlite_text(value) for value in row) for row in rows]
    except (OSError, sqlite3.Error, UnicodeError, ValueError):
        return False, []
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()


def state_database_path(sqlite_home: Path) -> Path:
    preferred = sqlite_home / "state_5.sqlite"
    with contextlib.suppress(OSError):
        def state_version(path: Path) -> int:
            match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
            return int(match.group(1)) if match else -1

        candidates = sorted(sqlite_home.glob("state_*.sqlite"), key=state_version, reverse=True)
        if candidates:
            return candidates[0]
    return preferred


def state_database_thread_info(codex_home: Path, thread_id: str) -> tuple[str, str]:
    """Return exact-PK classification and rollout path without filesystem walks."""
    database = state_database_path(codex_home)
    available, rows = sqlite_rows(
        database,
        "SELECT rollout_path, COALESCE(thread_source, ''), COALESCE(source, '') "
        "FROM threads WHERE id = ? LIMIT 1",
        thread_id,
    )
    if not available:
        available, rows = sqlite_rows(
            database,
            "SELECT rollout_path, '', COALESCE(source, '') FROM threads WHERE id = ? LIMIT 1",
            thread_id,
        )
    if not available or not rows:
        return "unknown", ""
    rollout_path, thread_source, source = rows[0]
    if thread_source.lower() == "subagent" or '"subagent"' in source.lower():
        return "subagent", rollout_path
    edge_available, edge = sqlite_scalar(
        database,
        "SELECT child_thread_id FROM thread_spawn_edges WHERE child_thread_id = ? LIMIT 1",
        thread_id,
    )
    if edge_available and edge:
        return "subagent", rollout_path
    if thread_source or (source and edge_available):
        return "root", rollout_path
    return "unknown", rollout_path


def state_database_classification(codex_home: Path, thread_id: str) -> str:
    return state_database_thread_info(codex_home, thread_id)[0]


def trusted_rollout_candidate(candidate: Path, session_home: Path, thread_id: str) -> Path | None:
    """Accept only an identity-matching rollout inside this Codex home."""
    if not thread_id:
        return None
    try:
        resolved_candidate = candidate.resolve(strict=True)
        if not resolved_candidate.is_file():
            return None
        resolved_home = session_home.resolve(strict=True)
        allowed_roots = [
            root.resolve(strict=True)
            for root in (resolved_home / "sessions", resolved_home / "archived_sessions")
            if root.is_dir()
        ]
        if not any(resolved_candidate.is_relative_to(root) for root in allowed_roots):
            return None
        metadata = read_first_json_line(resolved_candidate)
        payload = metadata.get("payload") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or payload.get("id") != thread_id
        ):
            return None
        return resolved_candidate
    except (OSError, json.JSONDecodeError, RuntimeError):
        return None


def exact_rollout_path(stored_path: str, session_home: Path, thread_id: str) -> Path | None:
    if not stored_path:
        return None
    candidate = Path(stored_path)
    trusted = trusted_rollout_candidate(candidate, session_home, thread_id)
    if trusted is not None:
        return trusted
    normalized = stored_path.replace("\\", "/")
    marker = normalized.lower().find("/.codex/")
    if marker < 0:
        return None
    relative_text = normalized[marker + len("/.codex/") :]
    relative = Path(relative_text)
    if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        return None
    translated = session_home.joinpath(*relative.parts)
    return trusted_rollout_candidate(translated, session_home, thread_id)


def bounded_session_index_contains(session_home: Path, thread_id: str) -> bool:
    index = session_home / "session_index.jsonl"
    try:
        with index.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - SESSION_INDEX_MAX_BYTES)
            handle.seek(start)
            data = handle.read(SESSION_INDEX_MAX_BYTES)
        if start > 0:
            boundary = data.find(b"\n")
            data = data[boundary + 1 :] if boundary >= 0 else b""
        for index_number, raw_line in enumerate(data.splitlines()):
            if thread_id.encode("ascii", errors="ignore") not in raw_line:
                continue
            try:
                item = _decode_jsonl_line(raw_line, allow_bom=start == 0 and index_number == 0)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("id") == thread_id:
                return True
    except OSError:
        return False
    return False


def goal_status(codex_home: Path, thread_id: str) -> tuple[bool, str]:
    available, status = sqlite_scalar(
        codex_home / "goals_1.sqlite",
        "SELECT status FROM thread_goals WHERE thread_id = ? LIMIT 1",
        thread_id,
    )
    if available:
        return available, status
    return sqlite_scalar(
        state_database_path(codex_home),
        "SELECT status FROM thread_goals WHERE thread_id = ? LIMIT 1",
        thread_id,
    )


def descendant_threads(codex_home: Path, thread_id: str) -> tuple[bool, list[tuple[str, str]]]:
    database = state_database_path(codex_home)
    if not database.is_file():
        return False, []
    schema_available, edge_table = sqlite_scalar(
        database,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        "thread_spawn_edges",
    )
    if not schema_available:
        return False, []
    if not edge_table:
        return True, []
    available, rows = sqlite_rows(
        database,
        """
        WITH RECURSIVE descendants(id, status) AS (
            SELECT child_thread_id, status FROM thread_spawn_edges WHERE parent_thread_id = ?
            UNION
            SELECT edge.child_thread_id, edge.status
            FROM thread_spawn_edges AS edge
            JOIN descendants ON edge.parent_thread_id = descendants.id
        )
        SELECT id, COALESCE(status, '') FROM descendants
        """,
        thread_id,
    )
    return available, [(row[0], row[1].lower()) for row in rows if row and row[0]]


def last_rollout_lifecycle(path: Path) -> tuple[str, str, int, bool, bool]:
    """Return lifecycle, turn, mtime, invalid evidence, and incomplete-tail state."""
    try:
        # Child rollouts are normally small. Reading from the end avoids parsing
        # prompt/tool bodies and finds the newest lifecycle marker quickly.
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            size = stat.st_size
            modified_ms = stat.st_mtime_ns // 1_000_000
            if size > 0:
                handle.seek(size - 1)
                if handle.read(1) != b"\n":
                    return "unknown", "", modified_ms, False, True
            window = min(size, 4 * 1024 * 1024)
            handle.seek(size - window)
            raw_tail = handle.read(window)
            after = os.fstat(handle.fileno())
        if after.st_size != size or after.st_mtime_ns != stat.st_mtime_ns:
            return "unknown", "", after.st_mtime_ns // 1_000_000, False, False
        if window < size:
            boundary = raw_tail.find(b"\n")
            raw_tail = raw_tail[boundary + 1 :] if boundary >= 0 else b""
        invalid_lifecycle = False
        latest_lifecycle = "unknown"
        latest_turn_id = ""
        lines = raw_tail.splitlines()
        for line in reversed(lines):
            if len(line) > MAX_FALLBACK_ROLLOUT_LINE_BYTES:
                invalid_lifecycle = True
                continue
            try:
                decoded = line.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                invalid_lifecycle = True
                continue
            has_literal_lifecycle = any(
                marker in decoded for marker in ("task_started", "task_complete", "turn_aborted")
            )
            try:
                envelope = strict_json_loads(decoded)
            except json.JSONDecodeError:
                invalid_lifecycle = invalid_lifecycle or has_literal_lifecycle
                continue
            if not isinstance(envelope, dict) or envelope.get("type") != "event_msg":
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                invalid_lifecycle = invalid_lifecycle or has_literal_lifecycle
                continue
            event_type_value = payload.get("type")
            if not isinstance(event_type_value, str) or not event_type_value.strip():
                invalid_lifecycle = invalid_lifecycle or has_literal_lifecycle
                continue
            event_type = event_type_value.strip()
            if event_type not in ("task_started", "task_complete", "turn_aborted"):
                continue
            raw_turn_id = obj_value(payload, "turn_id", "turn-id", "turnId", default="")
            turn_id = raw_turn_id.strip() if isinstance(raw_turn_id, str) else ""
            if not turn_id:
                invalid_lifecycle = True
                continue
            if latest_lifecycle == "unknown":
                latest_lifecycle = event_type
                latest_turn_id = turn_id
        return latest_lifecycle, latest_turn_id, modified_ms, invalid_lifecycle, False
    except OSError:
        # Read/stat races are transient unknowns. They retain the existing
        # descendant orphan handling instead of becoming permanent corruption.
        return "unknown", "", 0, False, False


def active_descendants(
    runtime: Runtime,
    record: dict[str, Any],
    config: dict[str, Any],
    now_ms: int,
) -> tuple[bool, int, bool]:
    codex_home = Path(str(record.get("session_codex_home") or runtime.codex_home))
    sqlite_home = Path(str(record.get("session_sqlite_home") or codex_home))
    available, descendants = descendant_threads(sqlite_home, str(record.get("thread_id", "")))
    if available and not descendants:
        return False, 0, False
    orphan_ms = int(max(0, float(config["subagent_orphan_seconds"])) * 1000)
    unknown_since = record.get("descendant_unknown_since")
    if not isinstance(unknown_since, dict):
        unknown_since = {}
    if not available:
        first_seen = int(unknown_since.get("__tree__", 0) or 0)
        if first_seen <= 0:
            first_seen = now_ms
            unknown_since["__tree__"] = first_seen
        record["descendant_unknown_since"] = unknown_since
        mode = str(config.get("idle_detection_mode", "strict")).lower()
        fallback_ms = int(max(0, float(config.get("idle_probe_grace_seconds", 30))) * 1000)
        blocked = mode == "strict" or (fallback_ms > 0 and now_ms - first_seen < fallback_ms)
        return blocked, 1 if blocked else 0, False

    unknown_since.pop("__tree__", None)
    active = 0
    invalid_evidence = False
    seen_children: set[str] = set()
    for child_id, edge_status in descendants:
        seen_children.add(child_id)
        if edge_status == "closed":
            unknown_since.pop(child_id, None)
            continue
        rollout = find_rollout(runtime, child_id, str(codex_home), str(sqlite_home))
        if rollout is None:
            first_seen = int(unknown_since.get(child_id, 0) or 0)
            if first_seen <= 0:
                first_seen = now_ms
                unknown_since[child_id] = first_seen
            if orphan_ms <= 0 or now_ms - first_seen < orphan_ms:
                active += 1
            continue
        lifecycle, _, mtime_ms, child_invalid, child_incomplete = last_rollout_lifecycle(rollout)
        fresh = orphan_ms <= 0 or (mtime_ms > 0 and now_ms - mtime_ms < orphan_ms)
        if child_incomplete and fresh:
            active += 1
            continue
        if child_invalid and fresh:
            invalid_evidence = True
            active += 1
            continue
        if lifecycle == "task_started" and (orphan_ms <= 0 or now_ms - mtime_ms < orphan_ms):
            active += 1
        elif lifecycle == "unknown" and mtime_ms and (orphan_ms <= 0 or now_ms - mtime_ms < orphan_ms):
            active += 1
        elif lifecycle == "unknown" and not mtime_ms and edge_status != "closed":
            first_seen = int(unknown_since.get(child_id, 0) or 0)
            if first_seen <= 0:
                first_seen = now_ms
                unknown_since[child_id] = first_seen
            if orphan_ms <= 0 or now_ms - first_seen < orphan_ms:
                active += 1
        else:
            unknown_since.pop(child_id, None)
    for child_id in list(unknown_since):
        if child_id != "__tree__" and child_id not in seen_children:
            unknown_since.pop(child_id, None)
    record["descendant_unknown_since"] = unknown_since
    return active > 0, active, invalid_evidence


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Map modern lifecycle-hook input onto the legacy completion envelope."""
    hook_name = str(obj_value(event, "hook_event_name", "hook-event-name", default=""))
    if not hook_name:
        return event
    normalized = dict(event)
    normalized["type"] = "agent-turn-complete"
    normalized["thread-id"] = str(obj_value(event, "session_id", "session-id", default=""))
    normalized["turn-id"] = str(obj_value(event, "turn_id", "turn-id", default=""))
    normalized["last-assistant-message"] = str(
        obj_value(event, "last_assistant_message", "last-assistant-message", default="")
    )
    normalized["hook-event-name"] = hook_name
    return normalized


def event_classification(
    runtime: Runtime,
    event: dict[str, Any],
    thread_id: str,
    session_home: str = "",
    session_sqlite_home: str = "",
) -> str:
    hook_name = str(obj_value(event, "hook_event_name", "hook-event-name", default=""))
    if hook_name == "SubagentStop":
        return "subagent"
    # Codex currently emits Stop for both root and descendant sessions without
    # a dedicated root/subagent field. Never treat the hook name as root proof;
    # use explicit metadata, the state database, or rollout session metadata.
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
    database_classification, stored_rollout = state_database_thread_info(
        Path(session_sqlite_home or codex_home), thread_id
    )
    if database_classification != "unknown":
        return database_classification
    session = exact_rollout_path(stored_rollout, codex_home, thread_id)
    if session is not None:
        try:
            metadata = read_first_json_line(session)
            session_source = obj_value(obj_value(metadata, "payload", default={}), "source", default={})
            if isinstance(session_source, dict) and session_source.get("subagent") is not None:
                return "subagent"
            return "root"
        except (OSError, json.JSONDecodeError):
            pass
    if bounded_session_index_contains(codex_home, thread_id):
        return "root"
    # A brand-new active session can precede both SQLite and session_index.jsonl.
    # Probe only the current/previous date buckets (plus the flat archive root),
    # cap candidates, and verify the embedded identity before trusting metadata.
    dates = (dt.datetime.now().date(), dt.datetime.now().date() - dt.timedelta(days=1))
    buckets = [codex_home / "sessions" / day.strftime("%Y/%m/%d") for day in dates]
    buckets.append(codex_home / "archived_sessions")
    for bucket in buckets:
        if not bucket.is_dir():
            continue
        try:
            candidates = list(candidate for _, candidate in zip(range(8), bucket.glob(f"*{thread_id}*.jsonl")))
        except OSError:
            continue
        for candidate in candidates:
            trusted = trusted_rollout_candidate(candidate, codex_home, thread_id)
            if trusted is None:
                continue
            try:
                metadata = read_first_json_line(trusted)
                payload = obj_value(metadata, "payload", default={})
                if not isinstance(payload, dict) or payload.get("id") != thread_id:
                    continue
                session_source = obj_value(payload, "source", default={})
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
        value = strict_json_loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def find_rollout(
    runtime: Runtime,
    thread_id: str,
    session_home: str = "",
    session_sqlite_home: str = "",
) -> Path | None:
    if not thread_id:
        return None
    codex_home = Path(session_home or runtime.codex_home)
    available, stored_path = sqlite_scalar(
        state_database_path(Path(session_sqlite_home or codex_home)),
        "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
        thread_id,
    )
    if available and stored_path:
        candidate = exact_rollout_path(stored_path, codex_home, thread_id)
        if candidate is not None:
            return candidate
    for root_name in ("sessions", "archived_sessions"):
        root = codex_home / root_name
        if not root.exists():
            continue
        with contextlib.suppress(OSError):
            for _, candidate in zip(range(64), root.rglob(f"*{thread_id}*.jsonl")):
                trusted = trusted_rollout_candidate(candidate, codex_home, thread_id)
                if trusted is not None:
                    return trusted
    return None


def update_idle_probe(runtime: Runtime, record: dict[str, Any], include_message: bool) -> dict[str, Any]:
    previous = record.get("idle_probe") if isinstance(record.get("idle_probe"), dict) else {}
    thread_id = str(record.get("thread_id", ""))
    rollout = find_rollout(
        runtime,
        thread_id,
        str(record.get("session_codex_home", "")),
        str(record.get("session_sqlite_home", "")),
    )
    if rollout is None:
        probe = {
            "status": "unknown",
            "reason": "rollout-not-found",
            "rollout_path": "",
            "offset": 0,
            "last_lifecycle": "",
            "last_lifecycle_turn_id": "",
            "candidate_completed": False,
            "candidate_user_message": False,
            "candidate_final_message": False,
            "goal_status": "",
            "mtime_unix_ms": 0,
            "incomplete_tail": False,
            "invalid_lifecycle": False,
        }
        record["idle_probe"] = probe
        return probe

    same_rollout = str(previous.get("rollout_path", "")) == str(rollout)
    last_lifecycle = str(previous.get("last_lifecycle", "")) if same_rollout else ""
    last_lifecycle_turn_id = str(previous.get("last_lifecycle_turn_id", "")) if same_rollout else ""
    candidate_completed = bool(previous.get("candidate_completed", False)) if same_rollout else False
    candidate_user_message = bool(previous.get("candidate_user_message", False)) if same_rollout else False
    candidate_final_message = bool(previous.get("candidate_final_message", False)) if same_rollout else False
    candidate_completion_end_offset = int(previous.get("candidate_completion_end_offset", 0) or 0) if same_rollout else 0
    latest_terminal_message = (
        str(previous.get("latest_terminal_message", "")) if same_rollout and include_message else ""
    )
    latest_terminal_event_type = str(previous.get("latest_terminal_event_type", "")) if same_rollout else ""
    latest_terminal_end_offset = int(previous.get("latest_terminal_end_offset", 0) or 0) if same_rollout else 0
    latest_terminal_timestamp = str(previous.get("latest_terminal_timestamp", "")) if same_rollout else ""
    rollout_goal_status = str(previous.get("goal_status", "")) if same_rollout else ""
    invalid_lifecycle = bool(previous.get("invalid_lifecycle", False)) if same_rollout else False
    previous_offset = int(previous.get("offset", 0) or 0) if same_rollout else 0
    candidate_turn = str(record.get("turn_id", ""))
    current_turn = last_lifecycle_turn_id if last_lifecycle == "task_started" else ""

    try:
        with rollout.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            if previous_offset < 0 or previous_offset > stat.st_size:
                previous_offset = 0
                last_lifecycle = ""
                last_lifecycle_turn_id = ""
                candidate_completed = False
                candidate_user_message = False
                candidate_final_message = False
                candidate_completion_end_offset = 0
                latest_terminal_message = ""
                latest_terminal_event_type = ""
                latest_terminal_end_offset = 0
                latest_terminal_timestamp = ""
                rollout_goal_status = ""
                invalid_lifecycle = False
            snapshot_size = stat.st_size
            if snapshot_size > 0:
                handle.seek(snapshot_size - 1)
                if handle.read(1) != b"\n":
                    after = os.fstat(handle.fileno())
                    probe = {
                        "status": (
                            "busy"
                            if last_lifecycle == "task_started"
                            else "idle"
                            if last_lifecycle in ("task_complete", "turn_aborted")
                            else "unknown"
                        ),
                        "reason": "",
                        "rollout_path": str(rollout),
                        "offset": previous_offset,
                        "last_lifecycle": last_lifecycle,
                        "last_lifecycle_turn_id": last_lifecycle_turn_id,
                        "candidate_completed": candidate_completed,
                        "candidate_user_message": candidate_user_message,
                        "candidate_final_message": candidate_final_message,
                        "candidate_completion_end_offset": candidate_completion_end_offset,
                        "latest_terminal_message": latest_terminal_message,
                        "latest_terminal_event_type": latest_terminal_event_type,
                        "latest_terminal_end_offset": latest_terminal_end_offset,
                        "latest_terminal_timestamp": latest_terminal_timestamp,
                        "goal_status": rollout_goal_status,
                        "mtime_unix_ms": after.st_mtime_ns // 1_000_000,
                        "snapshot_changed": (
                            after.st_size != snapshot_size or after.st_mtime_ns != stat.st_mtime_ns
                        ),
                        "incomplete_tail": True,
                        "invalid_lifecycle": invalid_lifecycle,
                    }
                    record["idle_probe"] = probe
                    return probe
            handle.seek(previous_offset)
            appended = handle.read(max(0, snapshot_size - previous_offset))
            last_newline = appended.rfind(b"\n")
            if last_newline >= 0:
                complete_bytes = appended[: last_newline + 1]
                observed_size = previous_offset + last_newline + 1
            else:
                # Keep the cursor before an incomplete JSONL record. This also
                # handles lifecycle records whose assistant message is larger
                # than any fixed overlap window.
                complete_bytes = b""
                observed_size = previous_offset
            incomplete_tail = observed_size < snapshot_size
            after = os.fstat(handle.fileno())
            snapshot_changed = after.st_size != snapshot_size or after.st_mtime_ns != stat.st_mtime_ns
            observed_mtime_ms = after.st_mtime_ns // 1_000_000
    except OSError:
        probe = {
            "status": "unknown",
            "reason": "rollout-unreadable",
            "rollout_path": str(rollout),
            "offset": previous_offset,
            "last_lifecycle": last_lifecycle,
            "last_lifecycle_turn_id": last_lifecycle_turn_id,
            "candidate_completed": candidate_completed,
            "candidate_user_message": candidate_user_message,
            "candidate_final_message": candidate_final_message,
            "candidate_completion_end_offset": candidate_completion_end_offset,
            "latest_terminal_message": latest_terminal_message,
            "latest_terminal_event_type": latest_terminal_event_type,
            "latest_terminal_end_offset": latest_terminal_end_offset,
            "latest_terminal_timestamp": latest_terminal_timestamp,
            "goal_status": rollout_goal_status,
            "mtime_unix_ms": 0,
            "snapshot_changed": False,
            "incomplete_tail": False,
            "invalid_lifecycle": invalid_lifecycle,
        }
        record["idle_probe"] = probe
        return probe

    line_offset = previous_offset
    for raw_line in complete_bytes.splitlines(keepends=True):
        line_offset += len(raw_line)
        if len(raw_line) > MAX_FALLBACK_ROLLOUT_LINE_BYTES:
            invalid_lifecycle = True
            continue
        try:
            line = raw_line.rstrip(b"\r\n").decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            invalid_lifecycle = True
            continue
        has_literal_relevant_event = any(
            marker in line
            for marker in ("task_started", "task_complete", "turn_aborted", "thread_goal_updated", "user_message")
        )
        try:
            envelope = strict_json_loads(line)
        except json.JSONDecodeError:
            invalid_lifecycle = invalid_lifecycle or has_literal_relevant_event
            continue
        if not isinstance(envelope, dict) or envelope.get("type") != "event_msg":
            continue
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            invalid_lifecycle = invalid_lifecycle or has_literal_relevant_event
            continue
        event_type_value = payload.get("type")
        if not isinstance(event_type_value, str) or not event_type_value.strip():
            invalid_lifecycle = invalid_lifecycle or has_literal_relevant_event
            continue
        event_type = event_type_value.strip()
        if event_type not in ("task_started", "task_complete", "turn_aborted", "thread_goal_updated", "user_message"):
            continue
        if event_type in ("task_started", "task_complete", "turn_aborted"):
            raw_event_turn = obj_value(payload, "turn_id", "turn-id", "turnId", default="")
            event_turn = raw_event_turn.strip() if isinstance(raw_event_turn, str) else ""
            if not event_turn:
                invalid_lifecycle = True
                continue
            last_lifecycle = event_type
            last_lifecycle_turn_id = event_turn
            if event_type == "task_started":
                current_turn = event_turn
            if event_type in ("task_complete", "turn_aborted"):
                latest_terminal_event_type = event_type
                raw_terminal_message = (
                    str(payload.get("last_agent_message", ""))
                    if event_type == "task_complete"
                    else ""
                )
                latest_terminal_message = (
                    sanitize(raw_terminal_message, 4000, preserve_lines=True)
                    if include_message
                    else ""
                )
                latest_terminal_end_offset = line_offset
                latest_terminal_timestamp = str(envelope.get("timestamp", ""))
            if event_turn == candidate_turn and event_type in ("task_complete", "turn_aborted"):
                candidate_completed = True
                candidate_final_message = bool(str(payload.get("last_agent_message", "")).strip()) or event_type == "turn_aborted"
                candidate_completion_end_offset = line_offset
                record["completion_event_type"] = event_type
            if event_type in ("task_complete", "turn_aborted") and current_turn == event_turn:
                current_turn = ""
        elif event_type == "user_message":
            if current_turn and current_turn == candidate_turn:
                candidate_user_message = True
        elif event_type == "thread_goal_updated":
            goal = payload.get("goal")
            raw_goal_state = goal.get("status") if isinstance(goal, dict) else None
            goal_state = raw_goal_state.strip().lower() if isinstance(raw_goal_state, str) else ""
            if goal_state not in ("active", "paused", "blocked", "usage_limited", "budget_limited", "complete"):
                invalid_lifecycle = True
                continue
            rollout_goal_status = goal_state

    status = "busy" if last_lifecycle == "task_started" else "idle" if last_lifecycle in ("task_complete", "turn_aborted") else "unknown"
    if candidate_completion_end_offset > 0:
        record["rollout_identity"] = str(rollout)
        record["completion_end_offset"] = candidate_completion_end_offset
    probe = {
        "status": status,
        "reason": "",
        "rollout_path": str(rollout),
        "offset": observed_size,
        "last_lifecycle": last_lifecycle,
        "last_lifecycle_turn_id": last_lifecycle_turn_id,
        "candidate_completed": candidate_completed,
        "candidate_user_message": candidate_user_message,
        "candidate_final_message": candidate_final_message,
        "candidate_completion_end_offset": candidate_completion_end_offset,
        "latest_terminal_message": latest_terminal_message,
        "latest_terminal_event_type": latest_terminal_event_type,
        "latest_terminal_end_offset": latest_terminal_end_offset,
        "latest_terminal_timestamp": latest_terminal_timestamp,
        "goal_status": rollout_goal_status,
        "mtime_unix_ms": observed_mtime_ms,
        "snapshot_changed": snapshot_changed,
        "incomplete_tail": incomplete_tail,
        "invalid_lifecycle": invalid_lifecycle,
    }
    record["idle_probe"] = probe
    return probe


def unknown_probe_schedule(
    record: dict[str, Any], config: dict[str, Any], now_ms: int, reason: str
) -> tuple[bool, int]:
    """Return (expired, next probe) for evidence that cannot be verified yet."""
    created_ms = int(record.get("created_unix_ms", now_ms) or now_ms)
    deadline = created_ms + int(max(0, float(config.get("idle_probe_grace_seconds", 30))) * 1000)
    if str(config.get("idle_detection_mode", "strict")).lower() == "strict" and now_ms >= deadline:
        return True, now_ms
    previous_reason = str(record.get("unknown_probe_reason", ""))
    count = int(record.get("unknown_probe_count", 0) or 0) if previous_reason == reason else 0
    base = max(0.1, float(config.get("goal_poll_seconds", 1)))
    maximum = max(base, float(config.get("unknown_retry_max_seconds", 60)))
    delay = min(maximum, base * (2 ** min(10, count)))
    record["unknown_probe_reason"] = reason
    record["unknown_probe_count"] = count + 1
    due = now_ms + int(delay * 1000)
    return False, min(due, deadline) if deadline > now_ms else due


def idle_gate(
    runtime: Runtime,
    record: dict[str, Any],
    config: dict[str, Any],
    now_ms: int,
) -> tuple[bool, int, str]:
    mode = str(config.get("idle_detection_mode", "strict")).lower()
    if mode not in ("strict", "balanced", "off"):
        mode = "strict"
    if mode == "off":
        return True, now_ms, "disabled"

    probe = update_idle_probe(runtime, record, bool(config.get("include_message", False)))
    goal_available, database_goal = goal_status(
        Path(str(record.get("session_sqlite_home") or record.get("session_codex_home") or runtime.sqlite_home)),
        str(record.get("thread_id", "")),
    )
    rollout_goal = str(probe.get("goal_status", ""))
    if goal_available:
        effective_goal = database_goal
        if not effective_goal and rollout_goal in ("paused", "blocked", "usage_limited", "budget_limited", "complete"):
            effective_goal = rollout_goal
    else:
        effective_goal = rollout_goal
    record["goal_status"] = effective_goal
    poll_due = now_ms + int(max(0.25, float(config["goal_poll_seconds"])) * 1000)
    if config.get("goal_aware", True) and effective_goal == "active":
        return False, poll_due, "goal-active"

    descendants_busy, descendant_count, descendant_invalid = active_descendants(runtime, record, config, now_ms)
    record["active_descendants"] = descendant_count
    if descendant_invalid:
        invalid_deadline = int(record.get("created_unix_ms", now_ms) or now_ms) + int(
            max(0, float(config.get("idle_probe_grace_seconds", 30))) * 1000
        )
        if now_ms >= invalid_deadline:
            return False, now_ms, "unverifiable"
        expired, unknown_due = unknown_probe_schedule(record, config, now_ms, "descendant-probe-failed")
        if expired:
            return False, now_ms, "unverifiable"
        return False, unknown_due, "descendant-probe-failed"
    if descendants_busy:
        return False, poll_due, "subagents-active"

    if bool(probe.get("snapshot_changed", False)):
        return False, poll_due, "rollout-changing"
    if bool(probe.get("incomplete_tail", False)):
        return False, poll_due, "probe-incomplete"
    if bool(probe.get("invalid_lifecycle", False)):
        invalid_deadline = int(record.get("created_unix_ms", now_ms) or now_ms) + int(
            max(0, float(config.get("idle_probe_grace_seconds", 30))) * 1000
        )
        if now_ms >= invalid_deadline:
            return False, now_ms, "unverifiable"
        expired, unknown_due = unknown_probe_schedule(record, config, now_ms, "rollout-probe-failed")
        if expired:
            return False, now_ms, "unverifiable"
        return False, unknown_due, "rollout-probe-failed"

    probe_status = str(probe.get("status", "unknown"))
    candidate_completed = bool(probe.get("candidate_completed", False))
    candidate_is_latest = str(probe.get("last_lifecycle_turn_id", "")) == str(record.get("turn_id", ""))
    if probe_status == "busy":
        if candidate_completed and not candidate_is_latest:
            # A later turn is open. Its own terminal event is the only useful
            # notification candidate; holding this predecessor creates stale
            # notifications after the conversation has moved on.
            return False, now_ms, "superseded"
        return False, poll_due, "turn-active"

    if probe_status == "idle" and candidate_completed and candidate_is_latest:
        quiet_due = int(probe.get("mtime_unix_ms", 0) or 0) + int(max(0, float(config["idle_grace_seconds"])) * 1000)
        if quiet_due > now_ms:
            return False, quiet_due, "settling"
        return True, now_ms, "idle"

    if probe_status == "idle" and candidate_completed and not candidate_is_latest:
        if enqueue_latest_probe_candidate(runtime, record, probe, config):
            return False, now_ms, "superseded"
        return False, poll_due, "newer-completion-awaiting-candidate"

    fallback_due = int(record.get("created_unix_ms", now_ms)) + int(
        max(0, float(config["idle_probe_grace_seconds"])) * 1000
    )
    if mode == "balanced" and fallback_due <= now_ms:
        return True, now_ms, "balanced-fallback"
    expired, unknown_due = unknown_probe_schedule(record, config, now_ms, "probe-incomplete")
    if expired:
        return False, now_ms, "unverifiable"
    return False, min(unknown_due, fallback_due) if mode == "balanced" else unknown_due, "probe-incomplete"


def technical_suppression_reason(record: dict[str, Any], config: dict[str, Any]) -> str:
    if str(config.get("idle_detection_mode", "strict")).lower() == "off":
        return ""
    if not config.get("suppress_technical_turns", True):
        return ""
    if str(record.get("source_event", "")) == "Stop":
        return ""
    if str(record.get("goal_status", "")) in ("paused", "blocked", "usage_limited", "budget_limited", "complete"):
        return ""
    probe = record.get("idle_probe") if isinstance(record.get("idle_probe"), dict) else {}
    if not bool(probe.get("candidate_user_message", False)):
        return "technical-turn"
    if not bool(probe.get("candidate_final_message", False)):
        return "technical-turn"
    return ""


def new_record(
    event: dict[str, Any],
    origin: str,
    session_home: str,
    session_sqlite_home: str,
    classification: str,
    include_message: bool,
) -> dict[str, Any]:
    thread_id = str(obj_value(event, "thread-id", "thread_id", default=""))
    turn_id = str(obj_value(event, "turn-id", "turn_id", default=""))
    weak = not thread_id or not turn_id
    identity = f"codex-ntfy/v1|{thread_id}|{turn_id}" if not weak else f"codex-ntfy/v1|weak|{uuid.uuid4().hex}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    now = utc_now()
    now_ms = unix_ms()
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
        "provider": "codex",
        "weak_identity": weak,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "origin": sanitize(origin, 100),
        "session_codex_home": session_home,
        "session_sqlite_home": session_sqlite_home,
        "session_classification": classification,
        "include_message": include_message,
        "source_event": str(obj_value(event, "hook-event-name", "hook_event_name", default="legacy-notify")),
        "completion_event_type": str(
            obj_value(event, "completion-event-type", "completion_event_type", default="")
        ),
        "goal_status": str(obj_value(event, "goal-status", "goal_status", default="")),
        "created_at": now.isoformat(),
        "created_unix_ms": now_ms,
        "next_probe_unix_ms": now_ms,
        "next_attempt_unix_ms": now_ms,
        "attempts": 0,
        "last_error": None,
        "event": stored_event,
    }


def _enqueue_unlocked(runtime: Runtime, record: dict[str, Any]) -> str:
    runtime.ensure()
    sent_path = runtime.sent / f"{record['key']}.json"
    suppressed_path = runtime.suppressed / f"{record['key']}.json"
    dead_path = runtime.dead / f"{record['key']}.json"
    pending_path = runtime.pending / f"{record['key']}.json"
    outbox_path = runtime.outbox / f"{record['key']}.json"
    if sent_path.exists():
        runtime.log(f"deduplicated sent event key={record['key'][:12]}")
        return "sent"
    if suppressed_path.exists():
        receipt = read_json(suppressed_path)
        if str(record.get("source_event", "")) == "Stop" and receipt.get("reason") in ("technical-turn", "unverifiable"):
            suppressed_path.unlink(missing_ok=True)
        else:
            runtime.log(f"deduplicated suppressed event key={record['key'][:12]}")
            return "suppressed"
    if dead_path.exists():
        runtime.log(f"deduplicated dead event key={record['key'][:12]}")
        return "dead"
    if pending_path.exists():
        runtime.log(f"deduplicated pending event key={record['key'][:12]}")
        return "pending"
    if outbox_path.exists():
        runtime.log(f"deduplicated queued event key={record['key'][:12]}")
        return "queued"
    try:
        atomic_write_json(outbox_path, record, no_overwrite=True)
    except FileExistsError:
        return "queued"
    runtime.log(f"queued event key={record['key'][:12]} origin={record['origin']}")
    return "queued"


def enqueue(runtime: Runtime, record: dict[str, Any]) -> str:
    with record_mutation_lock(runtime, str(record.get("key", ""))):
        return _enqueue_unlocked(runtime, record)


def upgrade_pending_from_stop(path: Path, record: dict[str, Any]) -> bool:
    if str(record.get("source_event", "")) != "Stop" or not path.exists():
        return False
    try:
        existing = validate_record(read_json(path), record["key"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError, FileNotFoundError):
        return False
    existing["source_event"] = "Stop"
    incoming_classification = str(record.get("session_classification", "unknown"))
    existing["session_classification"] = (
        incoming_classification if incoming_classification in ("root", "subagent") else "unknown"
    )
    existing["candidate_kind"] = "hook_stop"
    existing["origin"] = record["origin"]
    existing["session_codex_home"] = record["session_codex_home"]
    existing["session_sqlite_home"] = record["session_sqlite_home"]
    existing["include_message"] = bool(record.get("include_message", False))
    existing["event"] = record["event"]
    now = unix_ms()
    existing["next_probe_unix_ms"] = min(int(existing.get("next_probe_unix_ms", now) or now), now)
    atomic_write_json(path, existing)
    return True


def _enqueue_pending_unlocked(runtime: Runtime, record: dict[str, Any]) -> str:
    runtime.ensure()
    paths = {
        "sent": runtime.sent / f"{record['key']}.json",
        "suppressed": runtime.suppressed / f"{record['key']}.json",
        "dead": runtime.dead / f"{record['key']}.json",
        "queued": runtime.outbox / f"{record['key']}.json",
        "pending": runtime.pending / f"{record['key']}.json",
    }
    is_stop = str(record.get("source_event", "")) == "Stop"
    for status, path in paths.items():
        if path.exists():
            if is_stop and status == "suppressed":
                try:
                    receipt = read_json(path)
                except (OSError, json.JSONDecodeError):
                    receipt = {}
                if isinstance(receipt, dict) and receipt.get("reason") in ("technical-turn", "unverifiable"):
                    path.unlink(missing_ok=True)
                    continue
            if is_stop and status == "pending":
                if upgrade_pending_from_stop(path, record):
                    runtime.log(f"upgraded pending evidence to Stop key={record['key'][:12]}")
                    return "pending"
            runtime.log(f"deduplicated {status} event key={record['key'][:12]}")
            return status
    try:
        atomic_write_json(paths["pending"], record, no_overwrite=True)
    except FileExistsError:
        if is_stop and upgrade_pending_from_stop(paths["pending"], record):
            runtime.log(f"upgraded concurrently enqueued evidence to Stop key={record['key'][:12]}")
        return "pending"
    runtime.log(f"pending idle confirmation key={record['key'][:12]} origin={record['origin']}")
    return "pending"


def enqueue_pending(runtime: Runtime, record: dict[str, Any]) -> str:
    with record_mutation_lock(runtime, str(record.get("key", ""))):
        return _enqueue_pending_unlocked(runtime, record)


def enqueue_latest_probe_candidate(
    runtime: Runtime,
    stale_record: dict[str, Any],
    probe: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    """Recover a newer terminal turn even when its live hook was lost."""
    latest_turn = str(probe.get("last_lifecycle_turn_id", ""))
    if not latest_turn or latest_turn == str(stale_record.get("turn_id", "")):
        return False
    event_type = str(probe.get("latest_terminal_event_type", ""))
    if event_type not in ("task_complete", "turn_aborted"):
        return False
    stale_event = stale_record.get("event") if isinstance(stale_record.get("event"), dict) else {}
    event = {
        "type": "agent-turn-complete",
        "thread-id": str(stale_record.get("thread_id", "")),
        "turn-id": latest_turn,
        "cwd": str(stale_event.get("cwd", "")),
        "last-assistant-message": str(probe.get("latest_terminal_message", "")),
        "hook-event-name": "rollout-probe",
        "completion-event-type": event_type,
    }
    recovered = new_record(
        event,
        str(stale_record.get("origin", "Codex")),
        str(stale_record.get("session_codex_home", "")),
        str(stale_record.get("session_sqlite_home", "")),
        str(stale_record.get("session_classification", "root")),
        bool(config.get("include_message", False)),
    )
    recovered["rollout_identity"] = str(probe.get("rollout_path", ""))
    recovered["completion_end_offset"] = int(probe.get("latest_terminal_end_offset", 0) or 0)
    recovered["completion_timestamp"] = str(probe.get("latest_terminal_timestamp", ""))
    status = enqueue_pending(runtime, recovered)
    runtime.log(
        f"recovered newer completion key={recovered['key'][:12]} from={stale_record['key'][:12]} status={status}"
    )
    return status in ("pending", "queued", "sent", "suppressed", "dead")


def _write_suppressed_receipt_unlocked(
    runtime: Runtime, record: dict[str, Any], reason: str = "subagent"
) -> None:
    # A concurrent modern Stop can upgrade the same pending record while a
    # worker still holds an older legacy snapshot. Stop is authoritative for
    # every suppression reason, so the stale worker must never remove it.
    for directory in (runtime.pending, runtime.outbox):
        current_path = directory / f"{record['key']}.json"
        if not current_path.exists():
            continue
        with contextlib.suppress(OSError, json.JSONDecodeError):
            current = read_json(current_path)
            if (
                isinstance(current, dict)
                and str(current.get("source_event", "")) == "Stop"
                and str(record.get("source_event", "")) != "Stop"
            ):
                runtime.log(f"kept Stop evidence during suppression key={record['key'][:12]}")
                return
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
                "reason": reason,
            },
            no_overwrite=True,
        )
    except FileExistsError:
        pass
    (runtime.pending / f"{record['key']}.json").unlink(missing_ok=True)
    (runtime.outbox / f"{record['key']}.json").unlink(missing_ok=True)


def write_suppressed_receipt(runtime: Runtime, record: dict[str, Any], reason: str = "subagent") -> None:
    with record_mutation_lock(runtime, str(record.get("key", ""))):
        _write_suppressed_receipt_unlocked(runtime, record, reason)


def persist_pending_record(runtime: Runtime, path: Path, record: dict[str, Any]) -> bool:
    with record_mutation_lock(runtime, str(record.get("key", ""))):
        if not path.exists():
            return False
        current = validate_record(read_json(path), record["key"])
        if str(current.get("source_event", "")) == "Stop" and str(record.get("source_event", "")) != "Stop":
            return False
        atomic_write_json(path, record)
        return True


def promote_pending_record(runtime: Runtime, path: Path, record: dict[str, Any]) -> bool:
    with record_mutation_lock(runtime, str(record.get("key", ""))):
        if not path.exists():
            return False
        current = validate_record(read_json(path), record["key"])
        if (
            str(current.get("source_event", "")) == "Stop"
            and str(record.get("source_event", "")) != "Stop"
        ):
            # The stale snapshot was gated before authoritative Stop evidence
            # arrived. Leave the canonical record pending so it is classified
            # and gated afresh; Stop can belong to a descendant session.
            return False
        promoted = record
        outbox_path = runtime.outbox / path.name
        try:
            atomic_write_json(outbox_path, promoted, no_overwrite=True)
        except FileExistsError:
            validate_record(read_json(outbox_path), record["key"])
        path.unlink(missing_ok=True)
        return True


def reconcile_suppressed_pending(runtime: Runtime, path: Path, record: dict[str, Any]) -> bool:
    """Return true when the pending record was discarded by a receipt."""
    with record_mutation_lock(runtime, str(record.get("key", ""))):
        receipt_path = runtime.suppressed / path.name
        if not receipt_path.exists() or not path.exists():
            return False
        receipt = read_json(receipt_path)
        current = validate_record(read_json(path), record["key"])
        if str(current.get("source_event", "")) == "Stop" and receipt.get("reason") == "technical-turn":
            receipt_path.unlink(missing_ok=True)
            return False
        path.unlink(missing_ok=True)
        return True


def rollout_position(record: dict[str, Any]) -> tuple[str, int]:
    identity = str(record.get("rollout_identity", ""))
    offset = int(record.get("completion_end_offset", 0) or 0)
    probe = record.get("idle_probe") if isinstance(record.get("idle_probe"), dict) else {}
    if not identity:
        identity = str(probe.get("rollout_path", ""))
    if offset <= 0:
        offset = int(probe.get("candidate_completion_end_offset", 0) or 0)
    return identity, offset


def uuid7_timestamp(turn_id: str) -> int | None:
    compact = turn_id.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", compact) or compact[12] != "7":
        return None
    return int(compact[:12], 16)


def compare_record_order(left: dict[str, Any], right: dict[str, Any]) -> int | None:
    """Compare logical rollout order; never guess from watcher arrival time."""
    left_rollout, left_offset = rollout_position(left)
    right_rollout, right_offset = rollout_position(right)
    if left_rollout and left_rollout == right_rollout and left_offset > 0 and right_offset > 0:
        return (left_offset > right_offset) - (left_offset < right_offset)
    left_uuid = uuid7_timestamp(str(left.get("turn_id", "")))
    right_uuid = uuid7_timestamp(str(right.get("turn_id", "")))
    if left_uuid is not None and right_uuid is not None and left_uuid != right_uuid:
        return (left_uuid > right_uuid) - (left_uuid < right_uuid)
    return None


def coalesce_thread_records(runtime: Runtime, config: dict[str, Any]) -> None:
    if str(config.get("idle_detection_mode", "strict")).lower() == "off":
        return
    newest: dict[str, tuple[dict[str, Any], Path]] = {}
    candidates: list[tuple[dict[str, Any], Path]] = []
    for path in runtime.pending.glob("*.json"):
        with contextlib.suppress(OSError, json.JSONDecodeError, TypeError, ValueError):
            record = validate_record(read_json(path), path.stem)
            thread_id = str(record.get("thread_id", ""))
            if not thread_id or bool(record.get("weak_identity", False)):
                continue
            candidates.append((record, path))
            current = newest.get(thread_id)
            if current is None or compare_record_order(record, current[0]) == 1:
                newest[thread_id] = (record, path)
    for record, path in candidates:
        latest = newest.get(str(record.get("thread_id", "")))
        if latest is None or latest[1] == path:
            continue
        if compare_record_order(latest[0], record) == 1:
            write_suppressed_receipt(runtime, record, "superseded")
            runtime.log(
                f"suppressed superseded event key={record['key'][:12]} thread={str(record.get('thread_id', ''))[:8]}"
            )


def has_newer_thread_record(runtime: Runtime, record: dict[str, Any]) -> bool:
    thread_id = str(record.get("thread_id", ""))
    if not thread_id or bool(record.get("weak_identity", False)):
        return False
    for path in runtime.pending.glob("*.json"):
        if path.stem == record.get("key"):
            continue
        with contextlib.suppress(OSError, json.JSONDecodeError, TypeError, ValueError):
            other = validate_record(read_json(path), path.stem)
            if str(other.get("thread_id", "")) != thread_id:
                continue
            if compare_record_order(other, record) == 1:
                return True
    return False


def process_pending(runtime: Runtime, config: dict[str, Any], now_ms: int) -> int | None:
    next_due: int | None = None
    mode = str(config.get("idle_detection_mode", "strict")).lower()
    # New completions must not wait behind hours-old unverifiable records.
    for path in sorted(runtime.pending.glob("*.json"), key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True):
        try:
            record = validate_record(read_json(path), path.stem)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            dead = {
                "schema": 1,
                "key": path.stem,
                "last_error": sanitize(exc, 300),
                "failed_at": utc_now().isoformat(),
            }
            atomic_write_json(runtime.dead / path.name, dead)
            path.unlink(missing_ok=True)
            runtime.log(f"dead-letter invalid pending event key={path.stem[:12]}")
            continue

        if (runtime.sent / path.name).exists():
            path.unlink(missing_ok=True)
            continue
        suppressed_path = runtime.suppressed / path.name
        if suppressed_path.exists() and reconcile_suppressed_pending(runtime, path, record):
            continue
        previous_reason = str(record.get("idle_reason", ""))
        if mode == "strict" and previous_reason in (
            "classification-unknown",
            "probe-incomplete",
            "newer-completion-awaiting-candidate",
        ):
            expired, _ = unknown_probe_schedule(record, config, now_ms, previous_reason)
            if expired:
                write_suppressed_receipt(runtime, record, "unverifiable")
                runtime.log(f"suppressed unverifiable pending key={record['key'][:12]} reason={previous_reason}")
                continue
        due = int(record.get("next_probe_unix_ms", 0) or 0)
        if due > now_ms:
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
                    str(record.get("session_sqlite_home", "")),
                )
                record["session_classification"] = classification
            if classification == "subagent":
                write_suppressed_receipt(runtime, record, "subagent")
                runtime.log(f"suppressed subagent pending key={record['key'][:12]}")
                continue
            if classification == "unknown" and mode == "strict":
                record["idle_reason"] = "classification-unknown"
                expired, next_probe = unknown_probe_schedule(record, config, now_ms, "classification-unknown")
                if expired:
                    write_suppressed_receipt(runtime, record, "unverifiable")
                    runtime.log(f"suppressed unverifiable pending key={record['key'][:12]} reason=classification-unknown")
                    continue
                record["next_probe_unix_ms"] = next_probe
                persist_pending_record(runtime, path, record)
                next_due = (
                    record["next_probe_unix_ms"]
                    if next_due is None
                    else min(next_due, record["next_probe_unix_ms"])
                )
                continue

        ready, probe_due, reason = idle_gate(runtime, record, config, now_ms)
        record["idle_reason"] = reason
        if not ready:
            if reason in ("superseded", "unverifiable"):
                write_suppressed_receipt(runtime, record, reason)
                continue
            record["next_probe_unix_ms"] = probe_due
            persist_pending_record(runtime, path, record)
            next_due = probe_due if next_due is None else min(next_due, probe_due)
            continue

        suppression_reason = technical_suppression_reason(record, config)
        if suppression_reason:
            write_suppressed_receipt(runtime, record, suppression_reason)
            runtime.log(f"suppressed {suppression_reason} key={record['key'][:12]}")
            continue

        # Close the idle epoch with a second snapshot while the record is still
        # pending. Once moved to outbox it is a durable delivery and must never
        # be coalesced with a later, separate user request.
        final_now_ms = unix_ms()
        ready, probe_due, reason = idle_gate(runtime, record, config, final_now_ms)
        record["idle_reason"] = reason
        if not ready:
            if reason in ("superseded", "unverifiable"):
                write_suppressed_receipt(runtime, record, reason)
                continue
            record["next_probe_unix_ms"] = probe_due
            persist_pending_record(runtime, path, record)
            next_due = probe_due if next_due is None else min(next_due, probe_due)
            continue
        suppression_reason = technical_suppression_reason(record, config)
        if suppression_reason:
            write_suppressed_receipt(runtime, record, suppression_reason)
            runtime.log(f"suppressed {suppression_reason} key={record['key'][:12]}")
            continue

        record["next_attempt_unix_ms"] = final_now_ms
        if promote_pending_record(runtime, path, record):
            runtime.log(f"idle confirmed key={record['key'][:12]} reason={reason}")
    return next_due


def recent_rollouts(runtime: Runtime, config: dict[str, Any], now_ms: int) -> list[Path]:
    runtime.cold_discovery_ran_this_scan = False
    root = runtime.codex_home / "sessions"
    if not root.is_dir():
        root = runtime.codex_home / "sessions"
    found: dict[str, Path] = {}
    discovery_ms = int(max(5, float(config.get("watch_discovery_seconds", 60))) * 1000)
    recent_ms = max(
        discovery_ms * 2,
        int(max(0, float(config["watch_initial_replay_seconds"])) * 1000),
    )
    cutoff_ms = now_ms - recent_ms
    today = dt.datetime.now().astimezone().date()
    for days_back in (0, 1):
        day = today - dt.timedelta(days=days_back)
        directory = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if directory.is_dir():
            for path in directory.glob("*.jsonl"):
                with contextlib.suppress(OSError):
                    if path.stat().st_mtime_ns // 1_000_000 >= cutoff_ms:
                        found[str(path)] = path
    for path in root.glob("*.jsonl"):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime_ns // 1_000_000 >= cutoff_ms:
                found[str(path)] = path

    # Once a file has a cursor, keep following it regardless of the date in
    # its directory. This supports long-lived tasks that cross day boundaries.
    refresh_cursors = now_ms - runtime.last_watch_cursor_refresh_ms >= discovery_ms
    if refresh_cursors:
        runtime.cold_discovery_ran_this_scan = True
        refreshed: dict[str, Path] = {}
        for state_path in runtime.watch.glob("*.json"):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                state = read_json(state_path)
                watched = Path(str(state.get("rollout_path", ""))) if isinstance(state, dict) else None
                if watched is not None and watched.is_file():
                    refreshed[str(watched)] = watched
        runtime.watch_cursor_cache = refreshed
        runtime.last_watch_cursor_refresh_ms = unix_ms()
    # The cached paths are already validated on the cold cadence; reusing them
    # keeps long-lived local sessions observable without rereading every cursor.
    found.update(runtime.watch_cursor_cache)

    if now_ms - runtime.last_watch_discovery_ms >= discovery_ms:
        runtime.cold_discovery_ran_this_scan = True
        discovered: dict[str, Path] = {}
        for root_name in ("sessions", "archived_sessions"):
            discovery_root = runtime.codex_home / root_name
            if not discovery_root.is_dir():
                continue
            with contextlib.suppress(OSError):
                for path in discovery_root.rglob("*.jsonl"):
                    with contextlib.suppress(OSError):
                        if path.stat().st_mtime_ns // 1_000_000 >= cutoff_ms:
                            discovered[str(path)] = path
        if runtime.last_watch_discovery_ms > 0:
            for key in set(discovered) - set(runtime.watch_discovery_cache):
                runtime.watch_force_replay_paths.add(key)
        else:
            replay_ms = int(max(0, float(config["watch_initial_replay_seconds"])) * 1000)
            for key, path in discovered.items():
                with contextlib.suppress(OSError):
                    if now_ms - path.stat().st_mtime_ns // 1_000_000 <= replay_ms:
                        runtime.watch_force_replay_paths.add(key)
        runtime.watch_discovery_cache = discovered
        # Measure cadence from completion, not from the start of a slow walk.
        runtime.last_watch_discovery_ms = unix_ms()
    for path in runtime.watch_discovery_cache.values():
        if path.is_file():
            found[str(path)] = path
    return list(found.values())


def rollout_metadata(path: Path) -> dict[str, Any]:
    try:
        first = read_first_json_line(path)
        payload = first.get("payload") if isinstance(first, dict) else None
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_rollout_watch_envelope(
    runtime: Runtime, path: Path, start: int, length: int, limit: int
) -> dict[str, Any] | None:
    if start < 0 or length <= 0 or length > limit:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if start + length > handle.tell():
                return None
            handle.seek(start)
            raw = handle.read(length)
        runtime.watch_bytes_read += len(raw)
        if len(raw) != length or not raw.endswith(b"\n"):
            return None
        value = strict_json_loads(raw.rstrip(b"\r\n").decode("utf-8", errors="strict"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def scan_rollout_file(
    runtime: Runtime,
    config: dict[str, Any],
    path: Path,
    now_ms: int,
    *,
    force_replay: bool = False,
    parent_pid: int = 0,
    parent_token: str = "",
) -> int:
    if not scan_parent_is_alive(runtime, parent_pid, parent_token):
        return 0
    try:
        metadata = read_first_json_line(path)
        payload = metadata.get("payload") if isinstance(metadata, dict) else None
        thread_id_from_metadata = str(payload.get("id", "")) if isinstance(payload, dict) else ""
    except (OSError, json.JSONDecodeError):
        return 0
    trusted_path = trusted_rollout_candidate(path, runtime.codex_home, thread_id_from_metadata)
    if trusted_path is None:
        return 0
    path = trusted_path
    state_id = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    state_path = runtime.watch / f"{state_id}.json"
    state: dict[str, Any] = {}
    if state_path.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = read_json(state_path)
            if isinstance(loaded, dict):
                state = loaded
    try:
        stat = path.stat()
    except OSError:
        return 0
    state_was_missing = not state
    limit = rollout_watch_max_read_bytes()
    discard_mode = ""
    corrupt_epoch = False
    incomplete_tail = False
    staged_type = ""
    staged_turn_id = ""
    staged_start = -1
    staged_length = 0
    thread_id = ""
    if state_was_missing:
        replay_ms = int(max(0, float(config["watch_initial_replay_seconds"])) * 1000)
        should_replay = force_replay or now_ms - stat.st_mtime_ns // 1_000_000 <= replay_ms
        # Reserve one byte for the record-boundary probe so total reads remain
        # within the configured cap while the retained tail still reaches EOF.
        offset = max(0, stat.st_size - max(1, limit - 1)) if should_replay else stat.st_size
        if should_replay and offset > 0:
            discard_mode = "initial"
            runtime.watch_truncated_replays += 1
    else:
        offset = int(state.get("offset", 0) or 0)
        discard_mode = str(state.get("discard_mode", ""))
        corrupt_epoch = bool(state.get("corrupt_epoch", False))
        incomplete_tail = bool(state.get("incomplete_tail", False))
        staged_type = str(state.get("staged_type", ""))
        staged_turn_id = str(state.get("staged_turn_id", ""))
        staged_start = int(state.get("staged_start", -1) or -1)
        staged_length = int(state.get("staged_length", 0) or 0)
        thread_id = str(state.get("thread_id", ""))
        if offset < 0 or offset > stat.st_size:
            offset = stat.st_size
            discard_mode = ""
            corrupt_epoch = True
            incomplete_tail = False
            staged_type = ""
            staged_turn_id = ""
            staged_start = -1
            staged_length = 0
        has_snapshot = "observed_size" in state and "observed_mtime_ns" in state
        snapshot_unchanged = (
            has_snapshot
            and int(state.get("observed_size", -1)) == stat.st_size
            and int(state.get("observed_mtime_ns", -1)) == stat.st_mtime_ns
        )
        # Retry staged EOF terminals, but never reread an unchanged partial
        # tail. Backlog offsets intentionally continue even with a same stat.
        if (
            snapshot_unchanged
            and staged_length <= 0
            and (offset == stat.st_size or incomplete_tail)
        ) or (not has_snapshot and offset == stat.st_size and staged_length <= 0):
            return 0
    try:
        with path.open("rb") as handle:
            snapshot = os.fstat(handle.fileno())
            snapshot_size = snapshot.st_size
            snapshot_mtime_ns = snapshot.st_mtime_ns
            offset = min(offset, snapshot_size)
            read_budget = limit
            boundary_bytes = 0
            if state_was_missing and discard_mode == "initial" and offset > 0:
                handle.seek(offset - 1)
                previous = handle.read(1)
                if previous:
                    boundary_bytes = 1
                    read_budget -= 1
                    if previous == b"\n":
                        discard_mode = ""
            handle.seek(offset)
            data = handle.read(min(max(0, snapshot_size - offset), read_budget))
        runtime.watch_bytes_read += boundary_bytes + len(data)
    except OSError:
        return 0

    parse_index = 0
    corruption_observed = False
    incomplete_tail = False
    if discard_mode and data:
        discard_newline = data.find(b"\n")
        if discard_newline < 0:
            parse_index = len(data)
        else:
            parse_index = discard_newline + 1
            discard_mode = ""
    while parse_index < len(data):
        newline = data.find(b"\n", parse_index)
        if newline < 0:
            break
        line_start_index = parse_index
        line_length = newline - line_start_index + 1
        if line_length > limit:
            corrupt_epoch = True
            corruption_observed = True
            staged_type = staged_turn_id = ""
            staged_start, staged_length = -1, 0
            parse_index = newline + 1
            continue
        raw_line = data[line_start_index:newline].rstrip(b"\r")
        try:
            line = raw_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            corrupt_epoch = True
            corruption_observed = True
            staged_type = staged_turn_id = ""
            staged_start, staged_length = -1, 0
            parse_index = newline + 1
            continue
        has_literal_lifecycle = any(
            marker in line for marker in ("task_started", "task_complete", "turn_aborted")
        )
        try:
            envelope = strict_json_loads(line)
        except json.JSONDecodeError:
            if has_literal_lifecycle:
                corrupt_epoch = True
                corruption_observed = True
                staged_type = staged_turn_id = ""
                staged_start, staged_length = -1, 0
            parse_index = newline + 1
            continue
        payload = (
            envelope.get("payload")
            if isinstance(envelope, dict) and envelope.get("type") == "event_msg"
            else None
        )
        if not isinstance(payload, dict):
            if has_literal_lifecycle:
                corrupt_epoch = True
                corruption_observed = True
                staged_type = staged_turn_id = ""
                staged_start, staged_length = -1, 0
            parse_index = newline + 1
            continue
        event_type_value = payload.get("type")
        event_type = event_type_value.strip() if isinstance(event_type_value, str) else ""
        if event_type == "task_started":
            corrupt_epoch = False
            staged_type = staged_turn_id = ""
            staged_start, staged_length = -1, 0
        elif event_type in ("task_complete", "turn_aborted"):
            turn_id = str(obj_value(payload, "turn_id", "turnId", default=""))
            if not turn_id:
                corrupt_epoch = True
                corruption_observed = True
                staged_type = staged_turn_id = ""
                staged_start, staged_length = -1, 0
            elif not corrupt_epoch:
                staged_type = event_type
                staged_turn_id = turn_id
                staged_start = offset + line_start_index
                staged_length = line_length
        parse_index = newline + 1

    new_offset = offset + parse_index
    if parse_index < len(data):
        trailing_length = len(data) - parse_index
        read_reached_snapshot = offset + len(data) >= snapshot_size
        if trailing_length >= limit:
            # Advance monotonically through an oversized unterminated record;
            # fail closed for this epoch until a later task_started record.
            corrupt_epoch = True
            corruption_observed = True
            staged_type = staged_turn_id = ""
            staged_start, staged_length = -1, 0
            discard_mode = "oversize"
            new_offset = offset + len(data)
        elif read_reached_snapshot:
            incomplete_tail = True
    if new_offset < snapshot_size and not incomplete_tail:
        runtime.watch_backlog_files += 1
    if corruption_observed:
        runtime.watch_corrupt_files += 1

    if not thread_id:
        metadata = rollout_metadata(path)
        thread_id = str(obj_value(metadata, "id", "thread_id", "threadId", default=""))
    cursor_state: dict[str, Any] = {
        "schema": 1,
        "rollout_path": str(path),
        "offset": new_offset,
        "seen_unix_ms": now_ms,
        "thread_id": thread_id,
        "observed_size": snapshot_size,
        "observed_mtime_ns": snapshot_mtime_ns,
        "incomplete_tail": incomplete_tail,
        "discard_mode": discard_mode,
        "corrupt_epoch": corrupt_epoch,
        "staged_type": staged_type,
        "staged_turn_id": staged_turn_id,
        "staged_start": staged_start,
        "staged_length": staged_length,
    }
    if not scan_parent_is_alive(runtime, parent_pid, parent_token):
        return 0
    atomic_write_json(state_path, cursor_state)

    try:
        post = path.stat()
    except OSError:
        return 0
    stable_eof = (
        new_offset == snapshot_size
        and not incomplete_tail
        and not discard_mode
        and post.st_size == snapshot_size
        and post.st_mtime_ns == snapshot_mtime_ns
    )
    if not stable_eof or corrupt_epoch or staged_length <= 0:
        return 0
    staged_envelope = read_rollout_watch_envelope(
        runtime, path, staged_start, staged_length, limit
    )
    staged_payload = staged_envelope.get("payload") if isinstance(staged_envelope, dict) else None
    verified_type = str(staged_payload.get("type", "")) if isinstance(staged_payload, dict) else ""
    verified_turn = (
        str(obj_value(staged_payload, "turn_id", "turnId", default=""))
        if isinstance(staged_payload, dict)
        else ""
    )
    if verified_type != staged_type or verified_turn != staged_turn_id:
        cursor_state.update(
            {
                "corrupt_epoch": True,
                "staged_type": "",
                "staged_turn_id": "",
                "staged_start": -1,
                "staged_length": 0,
            }
        )
        atomic_write_json(state_path, cursor_state)
        if not corruption_observed:
            runtime.watch_corrupt_files += 1
        return 0

    metadata = rollout_metadata(path)
    if not thread_id:
        thread_id = str(obj_value(metadata, "id", "thread_id", "threadId", default=""))
    if not thread_id:
        runtime.log(f"watcher retained terminal: session identity unavailable path_hash={state_id[:12]}")
        return 0
    cwd = str(obj_value(metadata, "cwd", default=""))
    source = metadata.get("source")
    originator = str(obj_value(metadata, "originator", default=""))
    event = {
        "type": "agent-turn-complete",
        "thread-id": thread_id,
        "turn-id": verified_turn,
        "cwd": cwd,
        "last-assistant-message": str(staged_payload.get("last_agent_message", ""))
        if verified_type == "task_complete"
        else "",
        "hook-event-name": "rollout-watch",
        "completion-event-type": verified_type,
        "source": source,
    }
    if isinstance(source, str) and source.strip():
        classification = "subagent" if source.strip().lower() == "subagent" else "root"
    elif isinstance(source, dict) and source.get("subagent") is not None:
        classification = "subagent"
    else:
        classification = event_classification(
            runtime,
            event,
            thread_id,
            str(runtime.codex_home),
            str(runtime.sqlite_home),
        )
    origin = originator or platform.node() or "Codex"
    record = new_record(
        event,
        origin,
        str(runtime.codex_home),
        str(runtime.sqlite_home),
        classification,
        config["include_message"],
    )
    record["rollout_identity"] = str(path)
    record["completion_end_offset"] = staged_start + staged_length
    record["completion_timestamp"] = str(staged_envelope.get("timestamp", ""))
    if not scan_parent_is_alive(runtime, parent_pid, parent_token):
        return 0
    if config["suppress_subagents"] and classification == "subagent":
        write_suppressed_receipt(runtime, record, "subagent")
    elif config["idle_detection_mode"] == "off":
        enqueue(runtime, record)
    else:
        enqueue_pending(runtime, record)
    cursor_state.update(
        {
            "thread_id": thread_id,
            "staged_type": "",
            "staged_turn_id": "",
            "staged_start": -1,
            "staged_length": 0,
        }
    )
    atomic_write_json(state_path, cursor_state)
    return 1


def scan_rollouts(
    runtime: Runtime,
    config: dict[str, Any],
    now_ms: int,
    *,
    parent_pid: int = 0,
    parent_token: str = "",
) -> int:
    runtime.watch_bytes_read = 0
    runtime.watch_backlog_files = 0
    runtime.watch_truncated_replays = 0
    runtime.watch_corrupt_files = 0
    queued = 0
    for path in recent_rollouts(runtime, config, now_ms):
        if not scan_parent_is_alive(runtime, parent_pid, parent_token):
            return queued
        key = str(path)
        queued += scan_rollout_file(
            runtime,
            config,
            path,
            now_ms,
            force_replay=key in runtime.watch_force_replay_paths,
            parent_pid=parent_pid,
            parent_token=parent_token,
        )
        cursor = runtime.watch / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"
        if cursor.exists():
            runtime.watch_force_replay_paths.discard(key)
    return queued


def completion_label(record: dict[str, Any]) -> str:
    goal = str(record.get("goal_status", "")).strip().lower()
    labels = {
        "blocked": "blocked",
        "paused": "paused",
        "usage_limited": "usage limit",
        "budget_limited": "budget limit",
    }
    if goal in labels:
        return labels[goal]
    if str(record.get("completion_event_type", "")).strip().lower() == "turn_aborted":
        return "stopped"
    return "done"


def codex_task_url(thread_id: Any) -> str:
    raw = str(thread_id or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError):
        return ""
    canonical = str(parsed)
    if raw.lower() != canonical:
        return ""
    return CHATGPT_TASK_URL_PREFIX + canonical


def notification_title(value: Any, fallback: str = "workspace") -> str:
    candidate = sanitize(value, 60)
    if not candidate:
        candidate = sanitize(fallback, 60) or "workspace"
    oversized = len(candidate.encode("utf-8")) > MAX_NTFY_TITLE_BYTES
    fitted = truncate_utf8(candidate, MAX_NTFY_TITLE_BYTES)
    if oversized and fitted == "…":
        safe_fallback = sanitize(fallback, 60)
        if safe_fallback and safe_fallback != candidate:
            return notification_title(safe_fallback, "workspace")
        return "workspace"
    return fitted or "workspace"


def terminal_failure(record: dict[str, Any]) -> bool:
    if str(record.get("completion_event_type", "")).strip().lower() == "turn_aborted":
        return True
    goal = str(record.get("goal_status", "")).strip().lower()
    return bool(goal) and goal not in ("complete", "achieved")


def ntfy_payload(runtime: Runtime, record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    event = record["event"]
    cwd = str(obj_value(event, "cwd", "working-directory", "working_directory", default=""))
    project = notification_title(project_name(cwd), "workspace")
    display_name = project
    has_distinct_thread_title = False
    if config["include_thread_title"]:
        raw_local_title = sanitize(
            thread_title(
                runtime,
                record.get("thread_id", ""),
                record.get("session_codex_home", ""),
                record.get("session_sqlite_home", ""),
            ),
            60,
        )
        local_title = notification_title(raw_local_title, project) if raw_local_title else ""
        if local_title:
            display_name = local_title
            has_distinct_thread_title = local_title.casefold() != project.casefold()

    metadata: list[str] = []
    if config["include_full_path"] and cwd:
        metadata.append(sanitize(cwd, 120))
    elif has_distinct_thread_title:
        metadata.append(project)
    origin = sanitize(record.get("origin", ""), 40)
    if origin:
        metadata.append(origin)
    thread_id = sanitize(str(record.get("thread_id", ""))[:8], 8)
    if thread_id:
        metadata.append(f"#{thread_id}")
    context = " · ".join(metadata)

    summary = ""
    if config["include_message"]:
        raw_message = obj_value(event, "last-assistant-message", "last_assistant_message", default="")
        if not config["markdown"]:
            raw_message = markdown_to_plain_text(raw_message)
        summary = sanitize(
            raw_message,
            config["max_message_chars"],
            preserve_lines=config["markdown"],
        )
    separator = "\n\n" if config["markdown"] and summary else " · "
    suffix = separator + context if summary and context else context
    if summary:
        summary_budget = MAX_NTFY_MESSAGE_BYTES - len(suffix.encode("utf-8"))
        summary = truncate_utf8(summary, max(0, summary_budget))
        body = summary + suffix
    else:
        body = context or completion_label(record).capitalize()
    body = truncate_utf8(body, MAX_NTFY_MESSAGE_BYTES)

    payload: dict[str, Any] = {
        "topic": config["topic"],
        "title": display_name,
        "message": body,
        "sequence_id": record["sequence_id"],
    }
    provider = record.get("provider", "codex") if "provider" in record else "codex"
    if config["include_task_link"] and provider == "codex":
        task_url = codex_task_url(record.get("thread_id", ""))
        if task_url:
            payload["click"] = task_url
            if config["include_task_link_action"]:
                payload["actions"] = [
                    {
                        "action": "view",
                        "label": "Open task",
                        "url": task_url,
                        "clear": True,
                    }
                ]
    success_tag = config["tags"][0] if config.get("tags") else "white_check_mark"
    payload["tags"] = ["warning" if terminal_failure(record) else success_tag]
    if config["priority"] != 3:
        payload["priority"] = config["priority"]
    if config["markdown"] and bool(summary):
        payload["markdown"] = True
    return payload


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def publish(runtime: Runtime, record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not config["topic"]:
        raise RuntimeError(f"ntfy topic is not configured in {runtime.config_path}")
    if not config["server"]:
        raise RuntimeError("ntfy server is empty")
    parsed_server = urllib.parse.urlsplit(config["server"])
    if parsed_server.scheme not in ("http", "https") or not parsed_server.hostname:
        raise RuntimeError("ntfy server must be an absolute HTTP or HTTPS URL")
    if parsed_server.username is not None or parsed_server.password is not None:
        raise RuntimeError("ntfy server URL must not contain credentials; use token/username/password fields")
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
    if any(
        not safe_unicode_scalar_text(str(value)) or "\r" in str(value) or "\n" in str(value)
        for value in headers.values()
    ):
        raise RuntimeError("refusing unsafe HTTP header content")
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
        decoded = strict_json_loads(body)
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
    for path in runtime.state_root.glob("mutation-*.lock"):
        with contextlib.suppress(OSError):
            path.unlink()


def clear_synthetic_test_state(runtime: Runtime) -> int:
    """Remove only records created by the explicit --test/-Test command."""
    runtime.ensure()
    removed = 0
    for directory in (runtime.pending, runtime.outbox, runtime.sent, runtime.suppressed, runtime.dead):
        for path in directory.glob("*.json"):
            try:
                record = read_json(path)
                if not isinstance(record, dict) or str(record.get("thread_id", "")) != SYNTHETIC_TEST_THREAD_ID:
                    continue
                path.unlink()
                removed += 1
            except (OSError, json.JSONDecodeError):
                # Never infer test state from a filename or free-form message.
                continue
    return removed


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


def _try_lock(handle: Any) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except (BlockingIOError, OSError):
        return False


def _unlock(handle: Any) -> None:
    with contextlib.suppress(OSError):
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def scan_parent_is_alive(runtime: Runtime, pid: int, token: str) -> bool:
    if pid <= 0 and not token:
        return True
    if pid <= 0 or not process_is_alive(pid):
        return False
    try:
        health = read_json(runtime.worker_health_path)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(health, dict) and int(health.get("pid", 0) or 0) == pid and str(health.get("token", "")) == token


def write_scan_health(
    runtime: Runtime,
    *,
    status: str,
    started_at: str,
    completed_at: str = "",
    duration_ms: int = 0,
    observed: int = 0,
    error: str = "",
) -> None:
    try:
        atomic_write_json(
            runtime.scan_health_path,
            {
                "schema": 1,
                "status": status,
                "pid": os.getpid(),
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "observed": observed,
                "bytes_read": runtime.watch_bytes_read,
                "backlog_files": runtime.watch_backlog_files,
                "truncated_replays": runtime.watch_truncated_replays,
                "corrupt_files": runtime.watch_corrupt_files,
                "error": error,
            },
        )
    except OSError as exc:
        runtime.log(f"rollout scan health error: {sanitize(exc, 240)}")


def scan_worker(runtime: Runtime, *, continuous: bool, parent_pid: int, parent_token: str) -> int:
    runtime.ensure()
    lock_handle = runtime.scan_lock_path.open("a+b")
    if not _try_lock(lock_handle):
        lock_handle.close()
        return 0
    try:
        while True:
            if not scan_parent_is_alive(runtime, parent_pid, parent_token):
                return 0
            started = utc_now()
            started_at = started.isoformat()
            runtime.watch_bytes_read = 0
            runtime.watch_backlog_files = 0
            runtime.watch_truncated_replays = 0
            runtime.watch_corrupt_files = 0
            write_scan_health(runtime, status="running", started_at=started_at)
            runtime.log(f"rollout scan started pid={os.getpid()}")
            try:
                try:
                    test_delay_ms = max(0, int(os.environ.get("CODEX_NTFY_TEST_SCAN_DELAY_MS", "0")))
                except ValueError:
                    test_delay_ms = 0
                if test_delay_ms:
                    time.sleep(test_delay_ms / 1000)
                config = load_config(runtime)
                observed = scan_rollouts(
                    runtime,
                    config,
                    unix_ms(),
                    parent_pid=parent_pid,
                    parent_token=parent_token,
                )
                completed = utc_now()
                if runtime.cold_discovery_ran_this_scan:
                    completed_ms = unix_ms()
                    runtime.last_watch_cursor_refresh_ms = completed_ms
                    runtime.last_watch_discovery_ms = completed_ms
                duration_ms = max(0, int((completed - started).total_seconds() * 1000))
                write_scan_health(
                    runtime,
                    status="completed",
                    started_at=started_at,
                    completed_at=completed.isoformat(),
                    duration_ms=duration_ms,
                    observed=observed,
                )
                runtime.log(
                    f"rollout scan completed observed={observed} duration_ms={duration_ms} "
                    f"bytes_read={runtime.watch_bytes_read} backlog_files={runtime.watch_backlog_files}"
                )
            except Exception as exc:
                completed = utc_now()
                duration_ms = max(0, int((completed - started).total_seconds() * 1000))
                error = sanitize(exc, 300)
                write_scan_health(
                    runtime,
                    status="failed",
                    started_at=started_at,
                    completed_at=completed.isoformat(),
                    duration_ms=duration_ms,
                    error=error,
                )
                runtime.log(f"rollout watcher error: {error}")
                return 1
            if not continuous:
                return 0
            sleep_until = time.monotonic() + max(0.1, float(config["watch_scan_seconds"]))
            while time.monotonic() < sleep_until:
                if not scan_parent_is_alive(runtime, parent_pid, parent_token):
                    return 0
                time.sleep(min(0.5, max(0.01, sleep_until - time.monotonic())))
    finally:
        _unlock(lock_handle)
        lock_handle.close()


def start_scan_worker(parent_token: str) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--scan-rollouts",
        "--scan-parent-pid",
        str(os.getpid()),
        "--scan-parent-token",
        parent_token,
    ]
    if os.environ.get("CODEX_NTFY_SCAN_ONCE") != "1":
        command.append("--continuous")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, **kwargs)


def start_delivery_worker(parent_token: str, poll_seconds: float) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--continuous",
        "--delivery-only",
        "--poll-seconds",
        str(poll_seconds),
    ]
    env = os.environ.copy()
    env["CODEX_NTFY_PARENT_PID"] = str(os.getpid())
    env["CODEX_NTFY_PARENT_TOKEN"] = parent_token
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, **kwargs)


def worker(
    runtime: Runtime,
    *,
    continuous: bool,
    delivery_only: bool,
    poll_seconds: float,
    retry_base_seconds: float,
) -> int:
    runtime.ensure()
    lock_handle = (runtime.delivery_lock_path if delivery_only else runtime.lock_path).open("a+b")
    lock_acquired = _try_lock(lock_handle)
    scan_process: subprocess.Popen[bytes] | None = None
    delivery_process: subprocess.Popen[bytes] | None = None
    if not lock_acquired:
        lock_handle.close()
        return 0
    worker_token = str(os.environ.get("CODEX_NTFY_PARENT_TOKEN", "")) if delivery_only else uuid.uuid4().hex
    try:
        if not delivery_only:
            atomic_write_json(
                runtime.worker_health_path,
                {"schema": 1, "pid": os.getpid(), "token": worker_token, "started_at": utc_now().isoformat()},
            )
        else:
            atomic_write_json(
                runtime.delivery_health_path,
                {
                    "schema": 1,
                    "pid": os.getpid(),
                    "parent_pid": int(os.environ.get("CODEX_NTFY_PARENT_PID", "0") or 0),
                    "token": worker_token,
                    "started_at": utc_now().isoformat(),
                },
            )
        if continuous and not delivery_only:
            delivery_process = start_delivery_worker(worker_token, poll_seconds)
        cleaned = False
        next_watch_scan_ms = 0
        runtime.log(f"worker started continuous={continuous} delivery_only={delivery_only}")
        while True:
            if delivery_only and not scan_parent_is_alive(
                runtime,
                int(os.environ.get("CODEX_NTFY_PARENT_PID", "0") or 0),
                worker_token,
            ):
                return 0
            config = load_config(runtime)
            if not cleaned and not delivery_only:
                clean_runtime_state(runtime, config["sent_retention_days"], config["dead_retention_days"])
                cleaned = True
            now = unix_ms()
            if not delivery_only and continuous and delivery_process is not None and delivery_process.poll() is not None:
                runtime.log(f"delivery worker exited code={delivery_process.returncode}")
                delivery_process = start_delivery_worker(worker_token, poll_seconds)
            if not delivery_only and continuous and delivery_process is None:
                delivery_process = start_delivery_worker(worker_token, poll_seconds)
            if not config.get("watch_rollouts", True) and scan_process is not None:
                if scan_process.poll() is None:
                    with contextlib.suppress(OSError):
                        scan_process.terminate()
                scan_process = None
            if scan_process is not None and scan_process.poll() is not None:
                if scan_process.returncode:
                    runtime.log(f"rollout scan process exited code={scan_process.returncode}")
                scan_process = None
                next_watch_scan_ms = now + int(max(0.5, float(config["watch_scan_seconds"])) * 1000)
            if not delivery_only and continuous and config.get("watch_rollouts", True) and scan_process is None and now >= next_watch_scan_ms:
                try:
                    scan_process = start_scan_worker(worker_token)
                except OSError as exc:
                    runtime.log(f"failed to start rollout scan: {sanitize(exc, 300)}")
                    next_watch_scan_ms = now + int(max(0.5, float(config["watch_scan_seconds"])) * 1000)
            if not delivery_only:
                coalesce_thread_records(runtime, config)
                next_due = process_pending(runtime, config, now)
            else:
                next_due = None
            outbox_paths = (
                sorted(runtime.outbox.glob("*.json"), key=lambda item: (item.stat().st_mtime_ns, item.name))
                if delivery_only or not continuous
                else []
            )
            for path in outbox_paths:
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
                    receipt_data = read_json(suppressed_receipt)
                    if str(record.get("source_event", "")) == "Stop" and receipt_data.get("reason") in ("technical-turn", "unverifiable"):
                        suppressed_receipt.unlink(missing_ok=True)
                    else:
                        path.unlink(missing_ok=True)
                        continue
                due = int(record.get("next_attempt_unix_ms") or 0)
                if due > now:
                    next_due = due if next_due is None else min(next_due, due)
                    continue
                attempts = int(record.get("attempts") or 0)
                if config["suppress_subagents"] and attempts == 0:
                    classification = str(record.get("session_classification", "unknown"))
                    if classification not in ("root", "subagent"):
                        classification = event_classification(
                            runtime,
                            record.get("event") if isinstance(record.get("event"), dict) else {},
                            str(record.get("thread_id", "")),
                            str(record.get("session_codex_home", "")),
                            str(record.get("session_sqlite_home", "")),
                        )
                    if classification == "subagent":
                        write_suppressed_receipt(runtime, record, "subagent")
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

            remaining = sum(1 for _ in runtime.pending.glob("*.json")) + sum(1 for _ in runtime.outbox.glob("*.json"))
            if not continuous and remaining == 0:
                return 0
            sleep_for = max(0.1, poll_seconds)
            if next_due is not None:
                sleep_for = min(sleep_for, max(0.1, (next_due - unix_ms()) / 1000))
            time.sleep(sleep_for)
    finally:
        if delivery_process is not None and delivery_process.poll() is None:
            with contextlib.suppress(OSError):
                delivery_process.terminate()
        if scan_process is not None and scan_process.poll() is None:
            with contextlib.suppress(OSError):
                scan_process.terminate()
        if lock_acquired:
            _unlock(lock_handle)
        lock_handle.close()
        if not delivery_only:
            with contextlib.suppress(OSError, json.JSONDecodeError):
                health = read_json(runtime.worker_health_path)
                if isinstance(health, dict) and str(health.get("token", "")) == worker_token:
                    runtime.worker_health_path.unlink(missing_ok=True)
        else:
            with contextlib.suppress(OSError, json.JSONDecodeError):
                health = read_json(runtime.delivery_health_path)
                if isinstance(health, dict) and str(health.get("token", "")) == worker_token:
                    runtime.delivery_health_path.unlink(missing_ok=True)
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
    outbox_files = list(runtime.outbox.glob("*.json"))
    pending_files = list(runtime.pending.glob("*.json"))
    oldest_queued_seconds = 0.0
    if outbox_files:
        oldest_queued_seconds = round(max(0.0, time.time() - min(path.stat().st_mtime for path in outbox_files)), 1)
    oldest_pending_seconds = 0.0
    pending_reasons: dict[str, int] = {}
    if pending_files:
        oldest_pending_seconds = round(max(0.0, time.time() - min(path.stat().st_ctime for path in pending_files)), 1)
        for path in pending_files:
            reason = "invalid"
            with contextlib.suppress(OSError, json.JSONDecodeError, TypeError):
                value = read_json(path)
                if isinstance(value, dict):
                    reason = str(value.get("idle_reason", "") or "new")
            pending_reasons[reason] = pending_reasons.get(reason, 0) + 1
    scan_health: dict[str, Any] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError, TypeError):
        value = read_json(runtime.scan_health_path)
        if isinstance(value, dict):
            scan_health = value
    print(
        json.dumps(
            {
                "version": VERSION,
                "codex_home": str(runtime.codex_home),
                "config_path": str(runtime.config_path),
                "config_exists": runtime.config_path.exists(),
                "server": safe_server_display(config["server"]),
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
                "pending_idle": len(pending_files),
                "pending": len(pending_files),
                "oldest_pending_seconds": oldest_pending_seconds,
                "pending_reasons": pending_reasons,
                "queued": len(outbox_files),
                "oldest_queued_seconds": oldest_queued_seconds,
                "watch_scan_status": str(scan_health.get("status", "unknown")),
                "watch_scan_started_at": str(scan_health.get("started_at", "")),
                "watch_scan_completed_at": str(scan_health.get("completed_at", "")),
                "watch_scan_duration_ms": int(scan_health.get("duration_ms", 0) or 0),
                "watch_scan_observed": int(scan_health.get("observed", 0) or 0),
                "sent_receipts": sum(1 for _ in runtime.sent.glob("*.json")),
                "suppressed": sum(1 for _ in runtime.suppressed.glob("*.json")),
                "dead_letters": sum(1 for _ in runtime.dead.glob("*.json")),
                "watched_rollouts": sum(1 for _ in runtime.watch.glob("*.json")),
                "dead_retention_days": config["dead_retention_days"],
                "idle_detection_mode": config["idle_detection_mode"],
                "idle_grace_seconds": config["idle_grace_seconds"],
                "idle_probe_grace_seconds": config["idle_probe_grace_seconds"],
                "unknown_retry_max_seconds": config["unknown_retry_max_seconds"],
                "goal_aware": config["goal_aware"],
                "watch_rollouts": config["watch_rollouts"],
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
    parser.add_argument("--hook-event", action="store_true", help="Read a modern Codex lifecycle hook from stdin")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--kick-worker", action="store_true", help="Start an on-demand worker only when the outbox is nonempty")
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--delivery-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scan-rollouts", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scan-parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--scan-parent-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--no-spawn", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--cleanup-test-state", action="store_true", help="Remove local receipts/queue records created by --test")
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
        if args.cleanup_test_state:
            print(clear_synthetic_test_state(runtime))
            return 0
        if args.scan_rollouts:
            return scan_worker(
                runtime,
                continuous=args.continuous,
                parent_pid=args.scan_parent_pid,
                parent_token=args.scan_parent_token,
            )
        if args.kick_worker:
            if any(runtime.pending.glob("*.json")) or any(runtime.outbox.glob("*.json")):
                start_worker(args, runtime)
            if not args.classify:
                return 0
        if args.worker or args.continuous:
            return worker(
                runtime,
                continuous=args.continuous,
                delivery_only=args.delivery_only,
                poll_seconds=args.poll_seconds,
                retry_base_seconds=args.retry_base_seconds,
            )
        if args.test:
            raw = compact_json(
                {
                    "type": "agent-turn-complete",
                    "thread-id": SYNTHETIC_TEST_THREAD_ID,
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
        hook_name = str(obj_value(event, "hook_event_name", "hook-event-name", default=""))
        if hook_name and hook_name != "Stop":
            if args.hook_event:
                print("{}")
            return 0
        event = normalize_event(event)
        if event.get("type", "agent-turn-complete") != "agent-turn-complete":
            if args.hook_event:
                print("{}")
            return 0
        thread_id = str(obj_value(event, "thread-id", "thread_id", default=""))
        detected_classification = (
            "root"
            if args.test
            else event_classification(
                runtime,
                event,
                thread_id,
                str(runtime.codex_home),
                str(runtime.sqlite_home),
            )
        )
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
        record = new_record(
            event,
            origin,
            str(runtime.codex_home),
            str(runtime.sqlite_home),
            classification,
            config["include_message"],
        )
        if config["suppress_subagents"] and classification == "subagent":
            write_suppressed_receipt(runtime, record, "subagent")
            runtime.log(f"suppressed subagent completion thread={thread_id[:8]}")
            if args.hook_event:
                print("{}")
            return 0
        if args.test or config["idle_detection_mode"] == "off":
            enqueue(runtime, record)
        else:
            enqueue_pending(runtime, record)
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
        if args.hook_event:
            print("{}")
        return 0
    except Exception as exc:  # hook boundary: log safely, return non-zero for bridge fallback
        runtime.log(f"hook error: {sanitize(exc, 500)}")
        if args.hook_event:
            print("{}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
