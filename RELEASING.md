# Release process

This checklist is for maintainers publishing Durable Codex ntfy notifier. Public releases use Semantic Versioning and must be reproducible from a clean Git tag.

## Version policy

- Patch: backward-compatible bug, reliability, documentation, or security fixes.
- Minor: backward-compatible features, platforms, configuration keys, or installer capabilities.
- Major: intentional breaking changes to config, persisted queue/receipt schema, supported platforms, hook semantics, or upgrade behavior.

The persisted outbox is user data. A schema change requires backward-compatible reading or a tested migration; a version bump alone is not sufficient.

## 1. Prepare the release change

Create a release branch from current `main`. Update:

- `VERSION`;
- `$ScriptVersion` in `src/notify-ntfy.ps1`;
- `VERSION` in `src/notify-ntfy.py`;
- the `[Unreleased]` section and dated version section in `CHANGELOG.md`;
- README support/version text and examples when behavior changed;
- `README.it.md` for user-visible setup, privacy, or diagnostic changes;
- security and uninstall guidance for new state, credentials, services, or managed files.

Keep `include_message: false` as the fresh-install default unless a major release explicitly documents a privacy-model change.

Verify version parity:

```sh
version=$(tr -d '\r\n' < VERSION)
grep -F "VERSION = \"$version\"" src/notify-ntfy.py
grep -F "\$ScriptVersion = '$version'" src/notify-ntfy.ps1
```

## 2. Review public safety

Inspect the complete staged diff and commit metadata, not only the latest source files:

```sh
git status --short
git diff --check
git diff --cached --check
git diff --cached
git log --format=fuller --decorate -n 10
```

Search tracked content and reachable history for real topics, tokens, passwords, email addresses, personal host aliases, user profiles, private paths, and private ntfy server names. Automated secret scanners are useful but do not replace manual review.

Confirm that:

- only `ntfy-config.example.json` is tracked, with fake values;
- no state, backup, environment, test-runtime, log, or cache file is tracked;
- examples use generic hostnames and usernames;
- commit author email is intentionally public or uses a GitHub no-reply address;
- release notes call the project unofficial and do not claim affiliation or absolute uniqueness;
- alternative projects are characterized factually and linked to primary sources;
- GitHub private vulnerability reporting is enabled.

If a real secret ever entered a commit, rotate it before rewriting history. History rewriting alone is not remediation.

## 3. Validate locally

Run the platform-independent suite:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile src/notify-ntfy.py src/install-remote-linux-target.py tests/test_notifiers.py
git diff --check
```

On a POSIX host:

```sh
sh -n install-linux.sh install-remote-linux.sh src/notify-ntfy-wsl.sh
shellcheck install-linux.sh install-remote-linux.sh src/notify-ntfy-wsl.sh
```

On Windows PowerShell 5.1, parse all PowerShell files and run the suite:

```powershell
$AllErrors = @()
Get-ChildItem -Path . -Filter *.ps1 -Recurse | ForEach-Object {
  $Tokens = $null
  $Errors = $null
  [void][Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$Tokens, [ref]$Errors)
  $AllErrors += $Errors
}
if ($AllErrors.Count) { $AllErrors | Format-List; throw 'PowerShell parse validation failed.' }
python -m unittest discover -s tests -v
```

Run isolated installer smoke tests from [CONTRIBUTING.md](CONTRIBUTING.md). For changes to WSL or Remote SSH, also test the affected topology using disposable accounts/VMs and fake publish-only credentials. Verify:

- fresh install and upgrade;
- doctor output without secrets;
- concurrent hook enqueue;
- offline queue, recovery, and retry;
- generic `Stop` root/subagent classification, subagent suppression, and strict fail-closed handling of unknown evidence;
- rollback after an induced installer failure;
- uninstall without modifying unrelated Codex settings;
- config, state, staging, and backup permissions.

Never use a production topic for release tests.

## 4. Pass review and CI

Open a pull request with:

- release version and date;
- concise user-visible changes;
- compatibility or migration notes;
- privacy/security changes;
- tests and platforms exercised;
- known upstream limitations.

Require the repository CI workflow to pass on every supported test platform. Resolve review comments and verify the final merge commit, because a green earlier revision is insufficient.

## 5. Tag the exact commit

After the release pull request is merged:

```sh
git switch main
git pull --ff-only origin main
test -z "$(git status --porcelain)"
version=$(tr -d '\r\n' < VERSION)
git tag -a "v$version" -m "codex-ntfy-notifier v$version"
git show --stat "v$version"
git push origin "v$version"
```

Use a signed tag when maintainer signing is configured. Never move or reuse a published version tag. If the wrong commit was tagged, publish a corrected patch version and clearly document the error.

## 6. Create the GitHub release

Create release notes from the matching changelog section. Include upgrade behavior, privacy defaults, security fixes, known limitations, and a link to uninstall/rollback guidance.

With GitHub CLI:

```sh
version=$(tr -d '\r\n' < VERSION)
gh release create "v$version" \
  --verify-tag \
  --title "v$version" \
  --notes-file release-notes.md
```

`release-notes.md` is a temporary, reviewed artifact and must not contain credentials, private test output, internal hostnames, or unsupported claims.

This repository currently ships source-only releases. If binary/archive assets are added later, generate checksums in a clean environment and document the toolchain used.

## 7. Verify after publication

From a clean directory or disposable VM:

```sh
git clone --branch "v$(tr -d '\r\n' < VERSION)" --depth 1 \
  https://github.com/ravhello/codex-ntfy-notifier.git clean-release-check
```

Verify:

- repository and release are public;
- tag, release, and source constants match;
- CI is green for the tagged commit;
- README links and badges resolve;
- license, security policy, changelog, and examples render correctly;
- source archives contain no ignored runtime/private files;
- a clean isolated install succeeds on the changed platforms;
- GitHub topics, description, default branch, branch protection, vulnerability alerts, and private reporting remain configured.

Monitor public issues and security reports after release. Do not dismiss a report solely because unit tests pass; installer and topology failures often depend on host context.

## Security release exception

For an embargoed vulnerability, work in a GitHub private security advisory or another approved private channel. Add a regression test that contains no live exploit secret, coordinate credit and disclosure, rotate any maintainer/test credentials, and publish a patch release as soon as affected users have actionable upgrade guidance.

Do not reveal the vulnerable detail in a public `Unreleased` changelog entry before the coordinated release.
