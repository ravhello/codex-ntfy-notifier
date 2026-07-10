#!/usr/bin/env python3
"""Finish a codex-ntfy installation inside a Linux Remote SSH host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


def atomic_write(path: Path, text: str, mode: int) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.chmod(mode)
    os.replace(temp, path)


def ensure_notify(config_path: Path, notifier: Path, origin: str, python: Path) -> None:
    escaped_python = str(python).replace("\\", "\\\\").replace('"', '\\"')
    escaped_path = str(notifier).replace("\\", "\\\\").replace('"', '\\"')
    escaped_origin = origin.replace("\\", "\\\\").replace('"', '\\"')
    line = f'notify = ["{escaped_python}", "{escaped_path}", "--origin", "{escaped_origin}"]'
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    table = re.search(r"(?m)^[ \t]*\[", text)
    root_text = text[: table.start()] if table else text
    match = re.search(r"(?m)^[ \t]*notify[ \t]*=.*$", root_text)
    if match:
        if "notify-ntfy.py" not in match.group(0):
            raise RuntimeError(f"unrelated notify command already exists in {config_path}")
        if match.group(0) == line:
            return
        text = text[: match.start()] + line + text[match.end() :]
        atomic_write(config_path, text, 0o600)
        return
    if table:
        text = text[: table.start()] + line + "\n\n" + text[table.start() :]
    else:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + line + "\n"
    atomic_write(config_path, text, 0o600)


def handler_is_managed(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    pattern = re.compile(r"(?:^|[\\/])notify-ntfy(?:\.ps1|\.py|-wsl\.sh)(?=$|[\s'\"])", re.IGNORECASE)
    return any(
        isinstance(handler.get(field), str) and pattern.search(handler[field]) is not None
        for field in ("command", "commandWindows", "command_windows")
    )


def ensure_stop_hook(hooks_path: Path, notifier: Path, origin: str, python: Path) -> None:
    if hooks_path.exists():
        text = hooks_path.read_text(encoding="utf-8-sig")
        if text.strip():
            try:
                document = json.loads(text)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid JSON in {hooks_path}: {error}") from error
        else:
            document = {}
    else:
        text = ""
        document = {}

    if not isinstance(document, dict):
        raise RuntimeError(f"{hooks_path} must contain a JSON object")
    hooks = document.get("hooks")
    if hooks is None:
        hooks = {}
        document["hooks"] = hooks
    elif not isinstance(hooks, dict):
        raise RuntimeError(f"hooks in {hooks_path} must contain a JSON object")

    # Remove every older notifier handler, including obsolete UserPromptSubmit or
    # SubagentStop registrations, while retaining unrelated groups and handlers.
    for event_name, groups in list(hooks.items()):
        if not isinstance(groups, list):
            if event_name == "Stop":
                raise RuntimeError(f"hooks.Stop in {hooks_path} must contain a JSON array")
            continue
        filtered_groups: list[object] = []
        removed_from_event = False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                filtered_groups.append(group)
                continue
            handlers = group["hooks"]
            filtered_handlers = [handler for handler in handlers if not handler_is_managed(handler)]
            if len(filtered_handlers) == len(handlers):
                filtered_groups.append(group)
                continue
            removed_from_event = True
            if filtered_handlers:
                filtered_group = dict(group)
                filtered_group["hooks"] = filtered_handlers
                filtered_groups.append(filtered_group)
        if filtered_groups or not removed_from_event:
            hooks[event_name] = filtered_groups
        else:
            del hooks[event_name]

    stop_groups = hooks.setdefault("Stop", [])
    if not isinstance(stop_groups, list):
        raise RuntimeError(f"hooks.Stop in {hooks_path} must contain a JSON array")
    command = " ".join(
        (
            shlex.quote(str(python)),
            shlex.quote(str(notifier)),
            "--hook-event",
            "--origin",
            shlex.quote(origin),
        )
    )
    stop_groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 30,
                }
            ]
        }
    )

    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if rendered != text:
        atomic_write(hooks_path, rendered, 0o600)
    else:
        hooks_path.chmod(0o600)


def systemd_quote(value: str | Path) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def migrate_private_config(path: Path) -> None:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise RuntimeError("private config must contain a JSON object")
    changed = False
    for key, value in {
        "include_message": True,
        "include_thread_title": True,
        "allow_insecure_auth": False,
        "idle_detection_mode": "strict",
        "idle_grace_seconds": 1.5,
        "idle_probe_grace_seconds": 30,
        "goal_aware": True,
        "goal_poll_seconds": 1,
        "subagent_orphan_seconds": 1800,
        "suppress_technical_turns": True,
        "watch_rollouts": True,
        "watch_scan_seconds": 2,
        "watch_discovery_seconds": 60,
        "watch_initial_replay_seconds": 15,
        "watch_roots": [],
        "dead_retention_days": 30,
    }.items():
        if key not in config:
            config[key] = value
            changed = True
    # Watch roots are host-local topology. Never carry a source machine's WSL
    # or custom paths onto an SSH target.
    if config.get("watch_roots") != []:
        config["watch_roots"] = []
        changed = True
    if changed or path.read_bytes().startswith(b"\xef\xbb\xbf"):
        atomic_write(path, json.dumps(config, indent=2) + "\n", 0o600)


def backup_current(codex_home: Path, unit: Path, keep: int = 10) -> Path:
    root = codex_home / "ntfy-backups"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    backup = root / stamp
    backup.mkdir(mode=0o700)
    for name in ("config.toml", "hooks.json", "ntfy-config.json", "notify-ntfy.py", "install-remote-linux-target.py"):
        source = codex_home / name
        if source.exists():
            target = backup / name
            shutil.copy2(source, target)
            target.chmod(0o600 if name.endswith((".json", ".toml")) else 0o700)
    if unit.is_file():
        target = backup / "codex-ntfy.service"
        shutil.copy2(unit, target)
        target.chmod(0o600)
    for old in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True)[max(1, keep) :]:
        shutil.rmtree(old, ignore_errors=True)
    return backup


def restore_backup(
    codex_home: Path,
    backup: Path,
    previously_present: set[str],
    unit: Path,
    unit_previously_present: bool,
) -> None:
    for name in ("config.toml", "hooks.json", "ntfy-config.json", "notify-ntfy.py", "install-remote-linux-target.py"):
        destination = codex_home / name
        saved = backup / name
        if saved.exists():
            temp = destination.with_name(f".{destination.name}.rollback.{os.getpid()}")
            shutil.copy2(saved, temp)
            os.replace(temp, destination)
        elif name not in previously_present:
            destination.unlink(missing_ok=True)
    saved_unit = backup / "codex-ntfy.service"
    if saved_unit.is_file():
        unit.parent.mkdir(parents=True, exist_ok=True)
        temp = unit.with_name(f".{unit.name}.rollback.{os.getpid()}")
        shutil.copy2(saved_unit, temp)
        temp.chmod(0o600)
        os.replace(temp, unit)
    elif not unit_previously_present:
        unit.unlink(missing_ok=True)


def install_systemd(home: Path, notifier: Path, python: Path, codex_home: Path, sqlite_home: Path) -> str:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return "on-demand"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "codex-ntfy.service"
    probe = subprocess.run(
        [systemctl, "--user", "show-environment"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if probe.returncode != 0:
        return "on-demand"
    if unit.is_file() and "notify-ntfy.py" not in unit.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError(f"unrelated systemd unit already exists: {unit}")
    content = f"""[Unit]
Description=Durable ntfy worker for Codex completion notifications

[Service]
Type=simple
Environment={systemd_quote(f"CODEX_HOME={codex_home}")}
Environment={systemd_quote(f"CODEX_SQLITE_HOME={sqlite_home}")}
ExecStart={systemd_quote(python)} {systemd_quote(notifier)} --worker --continuous
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""
    atomic_write(unit, content, 0o600)
    subprocess.run([systemctl, "--user", "daemon-reload"], check=True, capture_output=True, text=True, timeout=20)
    subprocess.run([systemctl, "--user", "enable", "--now", unit.name], check=True, capture_output=True, text=True, timeout=20)
    subprocess.run([systemctl, "--user", "restart", unit.name], check=True, capture_output=True, text=True, timeout=20)
    state = subprocess.run(
        [systemctl, "--user", "is-active", unit.name],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--skip-systemd", action="store_true")
    parser.add_argument("--stage-dir")
    parser.add_argument("--expected-sha256", action="append", default=[])
    args = parser.parse_args()
    host = os.uname().nodename
    if args.origin.startswith("SSH:"):
        alias = args.origin[4:]
        effective_origin = f"SSH:{host}" if not alias or alias.lower() == host.lower() else f"SSH:{host} ({alias})"
    else:
        effective_origin = args.origin
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser().resolve()
    sqlite_home = Path(os.environ.get("CODEX_SQLITE_HOME") or codex_home).expanduser().resolve()
    notifier = codex_home / "notify-ntfy.py"
    python = Path(sys.executable).resolve()
    private_config = codex_home / "ntfy-config.json"
    config = codex_home / "config.toml"
    hooks = codex_home / "hooks.json"
    unit = home / ".config" / "systemd" / "user" / "codex-ntfy.service"
    systemctl = shutil.which("systemctl")
    if not args.skip_systemd and unit.is_file() and "notify-ntfy.py" not in unit.read_text(
        encoding="utf-8", errors="replace"
    ):
        raise RuntimeError(f"unrelated systemd unit already exists: {unit}")
    systemd_available = False
    if not args.skip_systemd and systemctl:
        probe = subprocess.run(
            [systemctl, "--user", "show-environment"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        systemd_available = probe.returncode == 0
    unit_was_enabled = bool(
        systemd_available
        and subprocess.run(
            [systemctl, "--user", "is-enabled", "--quiet", unit.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        ).returncode
        == 0
    )
    unit_was_active = bool(
        systemd_available
        and subprocess.run(
            [systemctl, "--user", "is-active", "--quiet", unit.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        ).returncode
        == 0
    )
    managed_names = ("notify-ntfy.py", "install-remote-linux-target.py", "ntfy-config.json")
    expected_hashes: dict[str, str] = {}
    for item in args.expected_sha256:
        name, separator, digest = item.partition("=")
        if not separator or name not in managed_names or name in expected_hashes or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RuntimeError(f"invalid expected SHA-256 specification: {item}")
        expected_hashes[name] = digest.lower()
    if expected_hashes and set(expected_hashes) != set(managed_names):
        raise RuntimeError("expected SHA-256 values must cover every staged file")
    previously_present = {name for name in (*managed_names, "config.toml", "hooks.json") if (codex_home / name).exists()}
    unit_previously_present = unit.is_file()
    backup = backup_current(codex_home, unit)
    stage: Path | None = None
    systemd_attempted = False
    try:
        if args.stage_dir:
            stage = Path(args.stage_dir).expanduser().resolve()
            if stage.parent != codex_home or not stage.is_dir():
                raise RuntimeError("invalid staging directory")
            for name in managed_names:
                staged = stage / name
                if not staged.is_file():
                    raise RuntimeError(f"staged file is missing: {name}")
                if expected_hashes:
                    actual = hashlib.sha256(staged.read_bytes()).hexdigest()
                    if actual != expected_hashes[name]:
                        raise RuntimeError(f"staged file hash mismatch: {name}")
            (stage / "notify-ntfy.py").chmod(0o700)
            (stage / "install-remote-linux-target.py").chmod(0o700)
            (stage / "ntfy-config.json").chmod(0o600)
            for name in managed_names:
                os.replace(stage / name, codex_home / name)
        if not notifier.exists() or not private_config.exists():
            raise RuntimeError("remote notifier files are incomplete")
        notifier.chmod(0o700)
        private_config.chmod(0o600)
        migrate_private_config(private_config)
        ensure_notify(config, notifier, effective_origin, python)
        ensure_stop_hook(hooks, notifier, effective_origin, python)
        state = codex_home / "ntfy-state"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        doctor = subprocess.run(
            [str(python), str(notifier), "--doctor"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        details = json.loads(doctor.stdout)
        if args.skip_systemd:
            worker = "on-demand"
        else:
            systemd_attempted = systemd_available
            worker = install_systemd(home, notifier, python, codex_home, sqlite_home)
    except Exception:
        if systemd_attempted and systemctl:
            subprocess.run(
                [systemctl, "--user", "disable", "--now", unit.name],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        restore_backup(codex_home, backup, previously_present, unit, unit_previously_present)
        if systemd_attempted and systemctl:
            subprocess.run(
                [systemctl, "--user", "daemon-reload"],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if unit_previously_present and unit_was_enabled:
                subprocess.run(
                    [systemctl, "--user", "enable", unit.name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            if unit_previously_present and unit_was_active:
                subprocess.run(
                    [systemctl, "--user", "start", unit.name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
        raise
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    print(
        "WARNING: the Codex Stop hook must be reviewed and trusted with /hooks before it can run.",
        file=sys.stderr,
    )
    print(
        json.dumps(
            {
                "host": host,
                "topic_configured": details["topic_configured"],
                "worker": worker,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
