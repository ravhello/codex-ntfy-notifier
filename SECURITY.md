# Security policy

Durable Codex ntfy notifier processes private local metadata and may hold reusable ntfy credentials. Please report vulnerabilities privately and avoid exposing affected users while a fix is prepared.

This is an unofficial community project. Security problems in OpenAI Codex, ntfy, Python, PowerShell, Windows, systemd, OpenSSH, VS Code, or an operating system should normally be reported to the relevant upstream project.

## Supported versions

| Version | Security fixes |
| --- | --- |
| 2.3.x | Supported |
| Earlier private/pre-public builds | Not supported; upgrade and rotate any embedded credentials |

Only the latest patch release receives security updates. A report may lead to a new patch release rather than a backport.

## Report a vulnerability

Use GitHub's private vulnerability reporting form:

[Report a vulnerability privately](https://github.com/ravhello/codex-ntfy-notifier/security/advisories/new)

If GitHub does not show a private reporting form, do **not** put technical details, proof of concept, credentials, topics, hostnames, or affected files in a public issue. Open a minimal issue asking the maintainer to enable/provide a private security channel, without describing the vulnerability.

Include, after sanitizing all secrets and personal data:

- affected version, commit, platform, and deployment topology;
- impact and realistic attack prerequisites;
- reproduction steps or a minimal proof of concept using fake credentials and a local test server;
- whether the issue affects PowerShell, Python, WSL, remote installation, or more than one path;
- suggested remediation, if known;
- whether the issue or exploit is already public.

Do not send a live token, password, private topic, queue record, Codex session, or backup. If a real credential may have been exposed, revoke/rotate it immediately; do not wait for triage.

## What to report here

Examples in scope include:

- authorization being forwarded or disclosed to an unintended endpoint;
- a crafted hook payload or queue record causing command execution or path escape;
- installer quoting, staging, rollback, ACL, or file-mode behavior exposing credentials;
- secret values appearing in logs, doctor output, exceptions, tests, or repository artifacts;
- queue tampering bypassing validation in a way that crosses a security boundary;
- unsafe remote-host validation that copies credentials to an unintended target;
- default behavior sending materially more Codex content than documented;
- a service/task configuration that grants unintended privilege.

Usually out of scope:

- a compromised local user, administrator/root, or SSH server reading that account's files;
- a user deliberately enabling `include_message`, `include_full_path`, or `allow_insecure_auth` and receiving the documented behavior;
- topic discovery or retention caused solely by the chosen ntfy server's policy;
- notification content visible because a client is configured to display it on a lock screen;
- denial of service that requires the attacker already to have arbitrary write access to the user's private state directory;
- unsupported/private builds that differ from the published source;
- upstream Codex events that never launch the external hook.

The boundary is not absolute. If uncertain and the impact could expose credentials, content, or code execution, report privately.

## Response process

The maintainer aims to:

1. acknowledge a complete report within three business days;
2. confirm scope and severity, or request more information, within seven business days;
3. keep the reporter informed when remediation takes longer;
4. prepare tests, a fix, release notes, and credential-rotation guidance as appropriate;
5. coordinate disclosure after a patched release is available.

These are targets for a community-maintained project, not a service-level agreement. Duplicate, incomplete, or upstream-only reports may be closed or redirected.

Please allow a reasonable remediation period before public disclosure. The project will credit reporters who want attribution, unless legal or safety constraints prevent it.

## Handling a suspected credential leak

Operators should act immediately:

1. revoke or rotate the token/password and, if needed, the topic;
2. issue a new publish-only credential per affected host;
3. update every local/WSL/remote private config and restart workers;
4. remove obsolete credentials from backups, environment injection, service configuration, shell history, and secrets managers;
5. review ntfy server access/publish logs where available;
6. remove exposed public artifacts and request cache/history cleanup, understanding that deletion cannot guarantee recall.

Rewriting Git history does not invalidate a credential already copied by someone else. Rotation is mandatory.

## Hardening guidance

Deployment and privacy guidance is maintained in [docs/security-and-privacy.md](docs/security-and-privacy.md). In particular, keep `include_message: false`, use HTTPS, refuse redirects, scope a publish-only token per host, and protect state and backups with host-native permissions.
