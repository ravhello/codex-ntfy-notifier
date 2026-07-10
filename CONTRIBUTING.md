# Contributing

Thank you for helping improve Durable Codex ntfy notifier. This is an unofficial community project and is not affiliated with OpenAI or ntfy.

Contributions are welcome for reliability, platform compatibility, privacy, security, tests, and documentation. Keep changes focused on durable Codex completion delivery rather than turning the repository into a general notification framework.

## Before opening an issue

Search existing issues and review [Troubleshooting](docs/troubleshooting.md). For a possible vulnerability, credential exposure, permission bypass, or secret leak, do not open a public issue; follow [SECURITY.md](SECURITY.md).

A useful public bug report includes:

- notifier version and operating system/runtime versions;
- whether Codex runs locally, in WSL, or through Remote SSH;
- the expected and observed behavior;
- a minimal reproduction that uses a fake topic and fake credentials;
- sanitized doctor output and relevant log lines;
- whether the hook, queue, worker, and HTTP stages were reached.

Never attach `ntfy-config.json`, Codex session/rollout data, queue records, dead letters, backups, environment dumps, or raw logs. Replace topics, URLs, tokens, usernames, hostnames, paths, thread/turn IDs, and message content before posting.

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

Use a disposable Codex home and fake topic. Doctor validation does not publish a notification.

Windows:

```powershell
$TemporaryHome = Join-Path $env:TEMP ('codex-ntfy-' + [Guid]::NewGuid().ToString('N'))
$env:CODEX_NTFY_TOPIC = 'test-topic-not-a-secret'
try {
  .\install.ps1 -CodexHome $TemporaryHome -NoWsl -SkipScheduledTask
  & "$TemporaryHome\notify-ntfy.ps1" -Doctor
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
```

Do not run remote installer tests against a shared or production account. A remote test must use an isolated user/VM and a publish-only fake credential.

## Cross-platform invariants

The PowerShell and Python implementations intentionally mirror one another. A behavior change normally needs equivalent code and tests for both implementations.

Preserve these invariants:

- queue before network;
- atomic, no-overwrite enqueue for a stable event key;
- one worker lock per state directory;
- deterministic identity only when both thread and turn IDs are present;
- the same sequence ID on every retry;
- a receipt before outbox removal;
- `include_message: false`, `include_thread_title: false`, and `include_full_path: false` for fresh installs;
- no storage of Codex `input-messages`;
- redirects refused and authenticated non-loopback HTTP refused by default;
- invalid records isolated without stopping the rest of the queue;
- subagent suppression with a bounded grace period and fail-open root protection;
- secrets absent from doctor output, logs, exceptions, tests, and repository history;
- private permissions for config, state, staging, and backups.

If parity is intentionally impossible because of platform behavior, document the difference and add a platform-specific regression test.

## Code and documentation style

- Prefer the Python standard library and built-in Windows facilities; discuss any runtime dependency before adding it.
- Keep the hook fast and deterministic. Network access belongs in the worker.
- Treat queue formats as persisted public interfaces. Add schema migration or backward-compatible parsing before changing them.
- Avoid logging payload bodies, credentials, topics, or full identifiers.
- Use atomic replacement for managed files and retain rollback behavior in installers.
- Use absolute paths in persistent hooks and service/task definitions.
- Quote all paths and treat host aliases, origins, payloads, and configuration as untrusted input.
- Keep English as the canonical technical documentation; update `README.it.md` when user-facing behavior changes.
- Link primary upstream documentation or issues for claims about Codex and ntfy behavior.

## Pull request checklist

Before requesting review:

- rebase or merge the latest `main` as appropriate;
- run all relevant tests and syntax checks;
- add a regression test for a bug fix;
- update `README.md`, `README.it.md`, or `docs/` for user-visible behavior;
- add an entry under `Unreleased` in [CHANGELOG.md](CHANGELOG.md);
- keep `VERSION`, the PowerShell `$ScriptVersion`, and Python `VERSION` in sync when preparing a release;
- inspect `git diff --check` and the staged diff;
- scan the full commit history for accidental secrets or personal data;
- confirm that no real server, topic, token, hostname, username, or private path was added.

Explain the failure mode, safety impact, and validation performed in the pull request. Screenshots are rarely needed; if used, redact notification content and all identifying metadata.

## Licensing

By submitting a contribution, you agree that it may be distributed under the repository's [MIT License](LICENSE). Only submit material you have the right to license.
