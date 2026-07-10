#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
codex_home=${CODEX_HOME:-"$HOME/.codex"}
private_config="$codex_home/ntfy-config.json"
origin=${CODEX_NTFY_ORIGIN:-"Linux:$(hostname 2>/dev/null || printf unknown-host)"}

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 >= 3.10 is required" >&2
  exit 2
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "python3 >= 3.10 is required" >&2
  exit 2
fi

mkdir -p "$codex_home"
chmod 700 "$codex_home"
stage="$codex_home/.ntfy-install-$$"
mkdir "$stage"
chmod 700 "$stage"
trap 'rm -rf "$stage"' EXIT HUP INT TERM

if [ -f "$private_config" ]; then
  cp "$private_config" "$stage/ntfy-config.json"
else
  if [ -z "${CODEX_NTFY_TOPIC:-}" ]; then
    echo "set CODEX_NTFY_TOPIC or create $private_config before installation" >&2
    exit 2
  fi
  CODEX_NTFY_CONFIG_TARGET="$stage/ntfy-config.json" python3 - <<'PY'
import json
import os

path = os.environ["CODEX_NTFY_CONFIG_TARGET"]
config = {
    "server": os.environ.get("CODEX_NTFY_SERVER", "https://ntfy.sh"),
    "topic": os.environ["CODEX_NTFY_TOPIC"],
    "token": os.environ.get("CODEX_NTFY_TOKEN", ""),
    "username": os.environ.get("CODEX_NTFY_USER", ""),
    "password": os.environ.get("CODEX_NTFY_PASSWORD", ""),
    "allow_insecure_auth": False,
    "priority": 3,
    "tags": ["computer", "white_check_mark"],
    "max_message_chars": 900,
    "include_message": False,
    "include_thread_title": False,
    "markdown": True,
    "include_full_path": False,
    "suppress_subagents": True,
    "subagent_classification_grace_seconds": 8,
    "timeout_seconds": 12,
    "max_attempts": 0,
    "retry_max_seconds": 900,
    "sent_retention_days": 14,
    "dead_retention_days": 30,
}
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
fd = os.open(path, flags, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
PY
fi

chmod 600 "$stage/ntfy-config.json"
cp "$script_dir/src/notify-ntfy.py" "$stage/notify-ntfy.py"
cp "$script_dir/src/install-remote-linux-target.py" "$stage/install-remote-linux-target.py"
chmod 700 "$stage/notify-ntfy.py" "$stage/install-remote-linux-target.py"

set -- python3 "$stage/install-remote-linux-target.py" --origin "$origin" --stage-dir "$stage"
if [ "${CODEX_NTFY_SKIP_SYSTEMD:-0}" = "1" ]; then
  set -- "$@" --skip-systemd
fi
CODEX_HOME="$codex_home" "$@"
trap - EXIT HUP INT TERM
