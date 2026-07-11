# Contributing

Thank you for helping improve Codex ntfy Notifier. This is an unofficial community project and is not affiliated with OpenAI or ntfy.

Contributions are welcome for idle detection, delivery reliability, platform compatibility, privacy, security, tests, and documentation. Keep changes focused on notifying when a root Codex task has no more work rather than turning the repository into a general notification framework.

## Contribute in 60 seconds

1. Pick a focused issue, or open a short feature request before a broad change.
2. Fork the repository and create one branch for that change.
3. Run `python3 -m unittest discover -s tests -v` plus the platform checks below.
4. Open a pull request that explains the user impact, affected environments, and validation.

Small compatibility fixtures, documentation corrections, installer UX improvements, and isolated regression tests are especially useful first contributions.

## Before opening an issue

Search existing issues and review [Troubleshooting](docs/troubleshooting.md). For a possible vulnerability, credential exposure, permission bypass, or secret leak, do not open a public issue; follow [SECURITY.md](SECURITY.md).

A useful public bug report includes:

- notifier version and operating system/runtime versions;
- whether Codex runs in the app, VS Code, CLI, WSL, or through Remote SSH;
- the expected and observed behavior;
- a minimal reproduction that uses a fake topic and fake credentials;
- sanitized doctor output and relevant log lines;
- whether the hook/watcher, pending idle gate, outbox, worker, and HTTP stages were reached.

Never attach `ntfy-config.json`, `hooks.json`, Codex session/rollout/database data, pending/outbox records, dead letters, backups, environment dumps, or raw logs. Replace topics, URLs, tokens, usernames, hostnames, paths, thread/turn IDs, goal state, and message content before posting.

## Development requirements

Runtime code has no third-party package dependencies.

- Python 3.10 or later;
- Windows PowerShell 5.1 for the Windows implementation and installer;
- a POSIX shell for `*.sh` validation;
- Git;
- ShellCheck when changing shell scripts;
- optional WSL, systemd user services, and SSH test hosts for integration work.

PowerShell changes must remain compatible with Windows PowerShell 5.1. Do not introduce syntax or cmdlets that require PowerShell 7 unless a compatible fallback is included and tested.

## Set up a working copy

Fork the repository, then clone your fork and create a focused branch:

```sh
git clone https://github.com/YOUR-USER/codex-ntfy-notifier.git
cd codex-ntfy-notifier
git switch -c fix/short-description
```

Do not use a real private topic in repository fixtures, commands committed to documentation, screenshots, or test output.

## Run the tests

The unit suite uses an in-process HTTP server and must not contact a real ntfy service:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile src/notify-ntfy.py src/install-remote-linux-target.py tests/test_notifiers.py
```

Validate shell syntax and lint where available:

```sh
sh -n install-linux.sh install-remote-linux.sh src/notify-ntfy-wsl.sh
shellcheck install-linux.sh install-remote-linux.sh src/notify-ntfy-wsl.sh
```

On Windows PowerShell, parse every PowerShell file with the 5.1 parser:

```powershell
$AllErrors = @()
Get-ChildItem -Path . -Filter *.ps1 -Recurse | ForEach-Object {
  $Tokens = $null
  $Errors = $null
  [void][Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$Tokens, [ref]$Errors)
  $AllErrors += $Errors
}
if ($AllErrors.Count) {
  $AllErrors | Format-List
  throw 'PowerShell parse validation failed.'
}
```

Run the full suite on Windows when changing `notify-ntfy.ps1`, `install.ps1`, remote Windows deployment, process control, quoting, or ACL behavior:

```powershell
python -m unittest discover -s tests -v
```

### Isolated installer smoke tests

Use a disposable Codex home and fake topic. Doctor validation does not publish a notification. Installer tests may inspect the generated `hooks.json` but must never modify a real Codex hook trust store.

Windows:

```powershell
$TemporaryHome = Join-Path $env:TEMP ('codex-ntfy-' + [Guid]::NewGuid().ToString('N'))
$env:CODEX_NTFY_TOPIC = 'test-topic-not-a-secret'
try {
  .\install.ps1 -CodexHome $TemporaryHome -NoWsl -SkipScheduledTask
  & "$TemporaryHome\notify-ntfy.ps1" -Doctor
  $Hooks = Get-Content "$TemporaryHome\hooks.json" -Raw | ConvertFrom-Json
  if (@($Hooks.hooks.Stop).Count -ne 1) { throw 'Expected one managed Stop group.' }
} finally {
  Remove-Item Env:CODEX_NTFY_TOPIC -ErrorAction SilentlyContinue
  if ($TemporaryHome -like "$env:TEMP\codex-ntfy-*") {
    Remove-Item -LiteralPath $TemporaryHome -Recurse -Force -ErrorAction SilentlyContinue
  }
}
```

Linux:

```sh
temporary_home=$(mktemp -d)
trap 'rm -rf -- "$temporary_home"' EXIT HUP INT TERM
CODEX_HOME="$temporary_home/.codex" \
CODEX_NTFY_TOPIC='test-topic-not-a-secret' \
CODEX_NTFY_SKIP_SYSTEMD=1 \
  ./install-linux.sh
python3 "$temporary_home/.codex/notify-ntfy.py" --doctor
CODEX_HOME="$temporary_home/.codex" python3 -c 'import json, os, pathlib; p=pathlib.Path(os.environ["CODEX_HOME"])/"hooks.json"; assert len(json.loads(p.read_text())["hooks"]["Stop"]) == 1'
```

Do not run remote installer tests against a shared or production account. A remote test must use an isolated user/VM and a publish-only fake credential.

## Cross-platform invariants

The PowerShell and Python implementations intentionally mirror one another. A behavior change normally needs equivalent code and tests for both implementations.

Preserve these invariants:

- a modern `Stop` hook classifies its session, creates an accepted root candidate, and returns promptly; it never publishes directly;
- explicitly named `SubagentStop` and locally classified descendant stops are ignored, while the legacy root-level `notify` command remains a compatibility source;
- the continuous watcher tracks each rollout independently, advances only across complete JSONL lines, and limits first-sight replay to the configured recent window;
- with idle detection enabled, accepted candidates enter `pending/` before they can enter the network-ready outbox;
- `strict` has no timed fail-open for unknown root classification or incomplete matching rollout evidence;
- `balanced` is the only timed incomplete-evidence fallback, and `off` is the explicit per-turn compatibility mode;
- a later open turn, an `active` root goal, or any recent active descendant keeps the root pending;
- goal integration uses and persists only status; it never extracts the objective into notifier state;
- coalescing uses the root thread identity, suppresses older candidates as `superseded`, and never applies one global cooldown across unrelated tasks;
- the idle gate takes a final fresh snapshot before atomic promotion; after promotion, outbox records are immutable delivery epochs and are never coalesced with later pending work;
- rollout recovery, hook delivery, and simultaneous app/VS Code/CLI sessions converge through stable event identity;
- atomic, no-overwrite enqueue for a stable event key;
- one worker lock per state directory;
- deterministic identity only when both thread and turn IDs are present;
- the same sequence ID on every retry;
- a receipt before outbox removal;
- `include_message: false`, `include_thread_title: false`, `include_full_path: false`, and `idle_detection_mode: "strict"` for fresh installs;
- no storage of Codex `input-messages`;
- redirects refused and authenticated non-loopback HTTP refused by default;
- invalid records isolated without stopping the rest of the queue;
- active-child orphan handling is explicit and bounded by `subagent_orphan_seconds`;
- installers preserve unrelated `hooks.json` groups/handlers/metadata, register only managed `Stop`, and never edit the Codex trust store;
- secrets absent from doctor output, logs, exceptions, tests, and repository history;
- private permissions for config, hooks, state, staging, and backups.

If parity is intentionally impossible because of platform behavior, document the difference and add a platform-specific regression test.

## Code and documentation style

- Prefer the Python standard library and built-in Windows facilities; discuss any runtime dependency before adding it.
- Keep modern and legacy hook paths fast and deterministic. Network access belongs in the worker.
- Treat pending, watcher, outbox, and receipt formats as persisted public interfaces. Add migration or backward-compatible parsing before changing them.
- Parse rollout files incrementally and tolerate concurrent appenders; never consume an incomplete trailing JSONL record.
- Keep completion evidence scoped to the candidate’s own root thread and turn. Do not select a globally newest rollout or add a process-global debounce.
- Keep SQLite access read-only/query-only and select the minimum fields required for lifecycle decisions.
- Preserve unrelated hook handlers and require explicit `/hooks` review; tests must not write product trust state.
- Avoid logging payload bodies, credentials, topics, or full identifiers.
- Use atomic replacement for managed files and retain rollback behavior in installers.
- Use absolute paths in persistent hooks and service/task definitions while preserving the originating WSL/remote `CODEX_HOME` and `CODEX_SQLITE_HOME`.
- Quote all paths and treat host aliases, origins, payloads, and configuration as untrusted input.
- Keep English as the canonical technical documentation; update `README.it.md` when user-facing behavior changes.
- Link primary upstream documentation or issues for claims about Codex and ntfy behavior.

## Pull request checklist

Before requesting review:

- rebase or merge the latest `main` as appropriate;
- run all relevant tests and syntax checks;
- add a regression test for a bug fix;
- for lifecycle changes, test modern `Stop`, legacy notification, lost-hook rollout recovery, automatic continuation coalescing, active goal, and active descendant behavior as applicable;
- for installer changes, test idempotence and prove unrelated `hooks.json` handlers/metadata survive;
- update `README.md`, `README.it.md`, or `docs/` for user-visible behavior;
- add an entry under `Unreleased` in [CHANGELOG.md](CHANGELOG.md);
- keep `VERSION`, the PowerShell `$ScriptVersion`, and Python `VERSION` in sync when preparing a release;
- inspect `git diff --check` and the staged diff;
- scan the full commit history for accidental secrets or personal data;
- confirm that no real server, topic, token, hostname, username, or private path was added.

Explain the failure mode, safety impact, and validation performed in the pull request. Screenshots are rarely needed; if used, redact notification content and all identifying metadata.

## Licensing

By submitting a contribution, you agree that it may be distributed under the repository's [MIT License](LICENSE). Only submit material you have the right to license.
