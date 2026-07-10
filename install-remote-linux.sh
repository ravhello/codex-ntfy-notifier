#!/usr/bin/env sh
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 <ssh-host> [private-config]" >&2
  exit 2
fi

host=$1
case "$host" in
  *[!A-Za-z0-9_.@-]*)
    echo "unsafe SSH host alias: $host" >&2
    exit 2
    ;;
esac

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
private_config=${2:-"${USERPROFILE:-$HOME}/.codex/ntfy-config.json"}
notifier="$script_dir/src/notify-ntfy.py"
target_installer="$script_dir/src/install-remote-linux-target.py"
stage=".ntfy-install-$$-$(date +%s)"

if [ ! -f "$private_config" ]; then
  echo "private config not found: $private_config" >&2
  exit 2
fi
if ! command -v sha256sum >/dev/null 2>&1; then
  echo "sha256sum is required to verify the remote staging transfer" >&2
  exit 2
fi

notifier_hash=$(sha256sum "$notifier" | awk '{print $1}')
installer_hash=$(sha256sum "$target_installer" | awk '{print $1}')
config_hash=$(sha256sum "$private_config" | awk '{print $1}')

ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$host" \
  "mkdir -p \"\$HOME/.codex/$stage\" && chmod 700 \"\$HOME/.codex\" \"\$HOME/.codex/$stage\""
cleanup() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$host" "rm -rf \"\$HOME/.codex/$stage\"" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

scp -q -o BatchMode=yes -o ConnectTimeout=10 -- "$notifier" "${host}:~/.codex/$stage/notify-ntfy.py"
scp -q -o BatchMode=yes -o ConnectTimeout=10 -- "$target_installer" "${host}:~/.codex/$stage/install-remote-linux-target.py"
scp -q -o BatchMode=yes -o ConnectTimeout=10 -- "$private_config" "${host}:~/.codex/$stage/ntfy-config.json"
ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$host" \
  "chmod 700 \"\$HOME/.codex/$stage/notify-ntfy.py\" \"\$HOME/.codex/$stage/install-remote-linux-target.py\" && chmod 600 \"\$HOME/.codex/$stage/ntfy-config.json\" && CODEX_HOME=\"\$HOME/.codex\" python3 \"\$HOME/.codex/$stage/install-remote-linux-target.py\" --origin \"SSH:$host\" --stage-dir \"\$HOME/.codex/$stage\" --expected-sha256 \"notify-ntfy.py=$notifier_hash\" --expected-sha256 \"install-remote-linux-target.py=$installer_hash\" --expected-sha256 \"ntfy-config.json=$config_hash\""
trap - EXIT HUP INT TERM
