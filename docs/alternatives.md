# Alternatives and adjacent projects

There are several useful notification projects for Codex and other coding agents. Durable Codex ntfy notifier is not claimed to be the first or only solution. The best choice depends on whether the priority is durable ntfy delivery, native desktop interaction, multiple agent products, tmux integration, or a lightweight final-summary workflow.

This comparison was last reviewed for the 2.3.0 public release on 2026-07-10. Projects change; verify their current documentation, supported platforms, maintenance status, security model, and license before adopting one.

## Project map

| Project | Reported focus | Consider it when |
| --- | --- | --- |
| [JerrySkywalker/codex-ntfy-notifier](https://github.com/JerrySkywalker/codex-ntfy-notifier) | Codex-to-ntfy notification on Windows/PowerShell, including protected local configuration. | You want a smaller Windows-focused Codex/ntfy setup. |
| [loccen/codex-ntfy-final-notifier](https://github.com/loccen/codex-ntfy-final-notifier) | A Codex skill for sending a final result/summary through ntfy. | You prefer an explicit skill workflow over an always-installed external completion hook. |
| [Ariandel35/codex-ping](https://github.com/Ariandel35/codex-ping) | A cross-platform Codex hook with ntfy and several other push providers. | You want a lightweight multi-provider script and do not need a durable disk outbox. |
| [mikolysz/cdntfy](https://github.com/mikolysz/cdntfy) | A small Go-based Codex-to-ntfy hook. | A compact compiled client for a simple ntfy.sh workflow is preferable. |
| [DevinoSolutions/ai-agent-notifier](https://github.com/DevinoSolutions/ai-agent-notifier) | Notifications for multiple AI coding agents, with desktop and ntfy-oriented delivery and deduplication features. | You want one notifier across several agents or richer local notification behavior. |
| [qinsz01/ai-ding](https://github.com/qinsz01/ai-ding) | Multi-agent notification routing with ntfy and other channels, including SSH-oriented workflows. | Broad provider/agent support matters more than persistent offline replay. |
| [lumpinif/agents-router](https://github.com/lumpinif/agents-router) | A broader local routing service for coding-agent events and many notification providers. | You want a central multi-provider service rather than a focused Codex/ntfy queue. |
| [MarioZZJ/cc-notify-hooks](https://github.com/MarioZZJ/cc-notify-hooks) | Shell-based multi-channel agent hooks with filtering and delayed notification behavior. | A configurable shell plugin across many channels fits the workflow. |
| [wmzspace/AgentNotification](https://github.com/wmzspace/AgentNotification) | Installer-driven notifications for several agents and providers, including ntfy. | Multi-agent coverage and straightforward direct publishing are the priority. |
| [mylee04/code-notify](https://github.com/mylee04/code-notify) | Multi-agent notifications with desktop, sound, and webhook-oriented integrations. | Native desktop experience, sound, or broad agent coverage matters more than an ntfy-specific outbox. |
| [paultendo/agent-notify](https://github.com/paultendo/agent-notify) | Multi-agent completion notifications and remote webhook targets, including ntfy-oriented workflows. | You need a general remote-notification tool rather than a Codex-specific delivery engine. |
| [flavio87/tap-to-tmux](https://github.com/flavio87/tap-to-tmux) | A phone/ntfy, SSH, and tmux-centered remote workflow. | The desired interaction is returning from a phone notification to a tmux session. |
| [mhmdibrahimm/codex-goal-hooks](https://github.com/mhmdibrahimm/codex-goal-hooks) | Hooks around Codex goal status and lifecycle. | Goal-state changes, rather than only the external turn-completion notification, are the main signal. |

The descriptions above are deliberately high-level. They are not security reviews, benchmarks, or promises about current feature parity.

## Where this repository is opinionated

Durable Codex ntfy notifier is a good fit when all of these are important:

- ntfy is the primary delivery channel;
- a completion must first survive in a per-host disk outbox;
- network and server failures should retry offline with backoff;
- concurrent VS Code/Codex sessions must share a safe host worker;
- Windows, WSL bridging, native Linux, and per-host Remote SSH deployment are part of one design;
- poison records must be isolated in a dead-letter directory;
- subagent completions should normally be filtered;
- final assistant content should be excluded by default.

Its deliberate tradeoffs are equally important:

- it is Codex-specific rather than a universal coding-agent notifier;
- it does not provide click-to-focus UI integration, sounds, or a notification center;
- queues are independent per host and have no central dashboard;
- installation manages a root-level Codex hook and a background worker;
- the delivery model is at-least-once, not exactly-once;
- approval/input events remain limited by the upstream Codex external hook.

## Lightweight alternative

For a single machine with reliable networking and no requirement to retain failed events, a short custom hook that publishes directly to ntfy may be sufficient. That approach has less code and operational state, but a slow or failed request occurs in the hook path and there is no durable retry queue unless you add one.

Use the smallest system that matches the failure modes you actually need to handle. If another maintained project already fits the topology and privacy requirements, using it is preferable to migrating solely for feature-count reasons.

## Evaluation checklist

Before choosing or combining notification tools, ask:

1. Does it run where Codex actually runs—local, WSL, container, or SSH host?
2. What happens when the network is unavailable at completion time?
3. Are simultaneous sessions serialized or deduplicated safely?
4. Which prompts, replies, paths, titles, identifiers, and credentials are stored or sent?
5. Does it follow redirects while carrying authorization?
6. How are credentials scoped, protected, rotated, and copied to remote hosts?
7. Can malformed queue data stop all future notifications?
8. How are background workers installed, upgraded, and removed?
9. Which events come from Codex itself, and which depend on prompting or an agent-specific skill?
10. Is the project license and maintenance model acceptable for the target environment?

Feature overlap is expected and healthy. Corrections to this page are welcome through a documentation pull request that links the relevant project's current primary documentation.
