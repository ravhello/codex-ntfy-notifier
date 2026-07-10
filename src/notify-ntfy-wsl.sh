#!/usr/bin/env sh
set -u

windows_script_override=""
hook_event=0
payload=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --windows-script)
      [ "$#" -ge 2 ] || exit 2
      windows_script_override=$2
      shift 2
      ;;
    --hook-event)
      hook_event=1
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      payload=$1
      shift
      break
      ;;
  esac
done
if [ -z "$payload" ] && [ ! -t 0 ]; then
  payload="$(cat)"
fi

machine="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
[ -n "$machine" ] || machine="unknown-host"
origin="${machine}/WSL:${WSL_DISTRO_NAME:-Linux}"
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
native_notifier="$script_dir/notify-ntfy.py"
native_python="$(command -v python3 2>/dev/null || true)"
session_classification="unknown"
if [ -x "$native_notifier" ] && [ -n "$native_python" ]; then
  detected="$(printf '%s' "$payload" | "$native_python" "$native_notifier" --classify --kick-worker --read-stdin 2>/dev/null || true)"
  case "$detected" in
    subagent)
      session_classification="subagent"
      ;;
    root|unknown)
      session_classification="$detected"
      ;;
  esac
fi

windows_home=""
windows_session_home=""
windows_sqlite_home=""
if command -v cmd.exe >/dev/null 2>&1; then
  windows_home="$(cmd.exe /d /c "echo %USERPROFILE%" 2>/dev/null | tr -d '\r' | tail -n 1)"
fi
if command -v wslpath >/dev/null 2>&1; then
  windows_session_home="$(wslpath -w "$script_dir" 2>/dev/null || true)"
  windows_sqlite_home="$(wslpath -w "${CODEX_SQLITE_HOME:-$script_dir}" 2>/dev/null || true)"
fi

windows_powershell="$(command -v powershell.exe 2>/dev/null || true)"
windows_script=$windows_script_override
if [ -z "$windows_script" ] && [ -n "$windows_home" ]; then
  windows_script="${windows_home}\\.codex\\notify-ntfy.ps1"
fi
if [ -n "$windows_script" ] && [ -n "$windows_powershell" ]; then
  if [ -n "$windows_session_home" ]; then
    if [ "$hook_event" -eq 1 ]; then
      if printf '%s' "$payload" | "$windows_powershell" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$windows_script" -ReadStdin -HookEvent -BridgeFallback -Origin "$origin" -SessionCodexHome "$windows_session_home" -SessionSqliteHome "$windows_sqlite_home" -SessionClassification "$session_classification"; then
        exit 0
      fi
    elif printf '%s' "$payload" | "$windows_powershell" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$windows_script" -ReadStdin -Origin "$origin" -SessionCodexHome "$windows_session_home" -SessionSqliteHome "$windows_sqlite_home" -SessionClassification "$session_classification"; then
      exit 0
    fi
  elif [ "$hook_event" -eq 1 ]; then
    if printf '%s' "$payload" | "$windows_powershell" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$windows_script" -ReadStdin -HookEvent -BridgeFallback -Origin "$origin" -SessionClassification "$session_classification"; then
      exit 0
    fi
  elif printf '%s' "$payload" | "$windows_powershell" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$windows_script" -ReadStdin -Origin "$origin" -SessionClassification "$session_classification"; then
    exit 0
  fi
fi

# Native fallback keeps Remote WSL useful even if Windows interop is disabled.
if [ -x "$native_notifier" ] && [ -n "$native_python" ]; then
  if [ "$hook_event" -eq 1 ]; then
    printf '%s' "$payload" | "$native_python" "$native_notifier" --hook-event --read-stdin --origin "$origin" --session-classification "$session_classification"
  else
    printf '%s' "$payload" | "$native_python" "$native_notifier" --read-stdin --origin "$origin" --session-classification "$session_classification"
  fi
  exit $?
fi

exit 1
