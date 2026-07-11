---
layout: default
title: Codex ntfy Notifier
description: Idle-only, durable ntfy notifications for local OpenAI Codex tasks across the app, VS Code, CLI, WSL, Linux, and Remote SSH.
---

![Codex ntfy Notifier](assets/hero.svg)

# Know when Codex is actually idle

Codex ntfy Notifier sends an ntfy push after local lifecycle evidence
indicates that a root Codex task has no more work. Intermediate turn signals are
held behind an idle gate, while a durable outbox retries transient delivery
failures.

[View the repository](https://github.com/ravhello/codex-ntfy-notifier) ·
[Install the latest release](https://github.com/ravhello/codex-ntfy-notifier/releases/latest) ·
[Quick start](https://github.com/ravhello/codex-ntfy-notifier#quick-start)

## Built for real Codex setups

- Codex app, VS Code extension, and CLI
- Windows, WSL2, native Linux, and Remote SSH hosts
- concurrent tasks, automatic continuations, active goals, and delegated agents
- temporary network failures, with a persistent outbox and capped retry backoff
- compact notification titles and privacy-preserving defaults

Each installed environment needs access to its own local Codex lifecycle state.
Pure cloud tasks that never mirror that state locally are not guaranteed. The
delivery model is durable at-least-once after idle confirmation, not
transactional exactly-once.

## Quick start

Windows with an optional Ubuntu WSL installation:

```powershell
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
.\install.ps1 -WslDistro Ubuntu
```

Native Linux:

```sh
git clone https://github.com/ravhello/codex-ntfy-notifier.git
cd codex-ntfy-notifier
CODEX_NTFY_TOPIC=$(python3 -c 'import getpass; print(getpass.getpass("Private ntfy topic: "))')
export CODEX_NTFY_TOPIC
./install-linux.sh
unset CODEX_NTFY_TOPIC
```

Read the complete [installation and privacy guidance](https://github.com/ravhello/codex-ntfy-notifier#quick-start)
before using a real topic. Newly installed Codex hooks require explicit review
through `/hooks`.

## How it stays quiet and reliable

Completion signals become candidates rather than immediate notifications. The
idle gate checks the matching turn, later work, goal state, active descendants,
and a short quiet window. Eligible events then move atomically into a host-local
outbox, where one worker retries transient failures and deduplicates stable
thread-and-turn identities.

[Architecture](architecture.md) ·
[Privacy and security](security-and-privacy.md) ·
[Troubleshooting](troubleshooting.md) ·
[Contributing](https://github.com/ravhello/codex-ntfy-notifier/blob/main/CONTRIBUTING.md)

> This is an unofficial community project. It is not affiliated with or
> endorsed by OpenAI or ntfy.
