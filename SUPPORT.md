# Support

This is an unofficial community project. Support is provided on a best-effort
basis through GitHub; there is no service-level agreement.

## Get help

1. Upgrade to the [latest release](https://github.com/ravhello/codex-ntfy-notifier/releases/latest).
2. Run the notifier's doctor command and review the
   [troubleshooting guide](docs/troubleshooting.md).
3. Search [existing issues](https://github.com/ravhello/codex-ntfy-notifier/issues)
   for the same environment and symptom.
4. If the problem remains, open a
   [sanitized bug report](https://github.com/ravhello/codex-ntfy-notifier/issues/new?template=bug_report.yml).
   Use the
   [feature request form](https://github.com/ravhello/codex-ntfy-notifier/issues/new?template=feature_request.yml)
   for a proposed capability instead.

A useful report identifies the notifier version, operating system, Python or
PowerShell version, provider/surface (Codex app, Codex/Claude VS Code, Claude Desktop Code tab, CLI, WSL, or Remote SSH),
expected behavior, observed behavior, and minimal reproduction steps. Include
sanitized doctor output when relevant. Do not use a live ntfy endpoint or
credential in a reproduction.

## Protect private data

Public support requests must not contain real:

- ntfy topics, URLs containing private paths, tokens, passwords, or headers;
- usernames, hostnames, IP addresses, filesystem paths, or SSH aliases;
- Codex prompts, responses, task titles, rollout/session data, or databases;
- pending, outbox, receipt, dead-letter, hook, config, backup, or environment
  files;
- complete thread or turn identifiers, raw logs, or screenshots with visible
  notification content.

Replace sensitive values consistently with obvious placeholders such as
`<TOPIC>`, `<TOKEN>`, `<USER>`, `<HOST>`, `<PATH>`, and `<THREAD_ID>`. Review the
entire attachment after redaction; surrounding lines and filenames can still
identify a person or system.

Do not open a public issue for a vulnerability, credential exposure, permission
bypass, or secret leak. Follow the [security policy](SECURITY.md) and use the
[private vulnerability reporting form](https://github.com/ravhello/codex-ntfy-notifier/security/advisories/new).
Rotate a possibly exposed credential immediately.

## Supported versions

Only the latest patch release receives fixes. The current support line is:

| Version | Status |
| --- | --- |
| Latest 2.5.x patch | Supported |
| 2.4.x and earlier | Unsupported; upgrade to the latest release |
| Private or modified builds | Reproduce on the latest published release when possible |

Release availability and security support can change. Check the
[release list](https://github.com/ravhello/codex-ntfy-notifier/releases) and
[security policy](SECURITY.md) before reporting a problem.

Upstream problems in Codex, Claude Code, ntfy, VS Code, Python, PowerShell, SSH, WSL, systemd,
or an operating system may be redirected to the corresponding project.
