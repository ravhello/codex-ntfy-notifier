# Alternatives and adjacent projects

There are useful notification projects for Codex and other coding agents. Durable Codex ntfy notifier is not claimed to be the first or only solution. The best choice depends on whether the priority is true root-task idle detection, durable ntfy delivery, native desktop interaction, broad provider support, tmux integration, or a lightweight explicit workflow.

This comparison was reviewed for version 2.4.3 on 2026-07-13, including the compact notification patterns described below. Projects change; verify their current documentation, code, supported platforms, maintenance status, security model, and license before adoption. The descriptions below are orientation, not security reviews or feature guarantees.

## Project map

| Project | Reported focus | Consider it when |
| --- | --- | --- |
| [JerrySkywalker/codex-ntfy-notifier](https://github.com/JerrySkywalker/codex-ntfy-notifier) | Codex-to-ntfy notification on Windows/PowerShell, including protected local configuration. | A smaller Windows-focused Codex/ntfy setup is enough. |
| [loccen/codex-ntfy-final-notifier](https://github.com/loccen/codex-ntfy-final-notifier) | A Codex skill for sending a final result or summary through ntfy. | You prefer an explicit model-invoked skill over an always-installed lifecycle worker. |
| [Ariandel35/codex-ping](https://github.com/Ariandel35/codex-ping) | A cross-platform Codex hook with ntfy and other push providers. | You want a lightweight multi-provider path. |
| [mikolysz/cdntfy](https://github.com/mikolysz/cdntfy) | A small Go-based Codex-to-ntfy hook. | A compact compiled client for a straightforward ntfy workflow is preferable. |
| [DevinoSolutions/ai-agent-notifier](https://github.com/DevinoSolutions/ai-agent-notifier) | Notifications for multiple AI coding agents, with desktop and ntfy-oriented delivery features. | One notifier across several agent products or richer local notification behavior matters most. |
| [qinsz01/ai-ding](https://github.com/qinsz01/ai-ding) | Multi-agent notification routing with ntfy and other channels, including SSH-oriented workflows. | Broad provider/agent support is the priority. |
| [lumpinif/agents-router](https://github.com/lumpinif/agents-router) | A broader local routing service for coding-agent events and many notification providers. | You want a central multi-provider service rather than a focused Codex/ntfy worker. |
| [MarioZZJ/cc-notify-hooks](https://github.com/MarioZZJ/cc-notify-hooks) | Shell-based multi-channel agent hooks with filtering and delayed notification behavior. | A configurable shell plugin across many channels fits the workflow. |
| [wmzspace/AgentNotification](https://github.com/wmzspace/AgentNotification) | Installer-driven notifications for several agents and providers, including ntfy. | Multi-agent coverage and direct publishing are the priority. |
| [mylee04/code-notify](https://github.com/mylee04/code-notify) | Multi-agent notifications with desktop, sound, and webhook integrations. | Native desktop experience, sound, or broad agent coverage matters more than an ntfy-specific queue. |
| [paultendo/agent-notify](https://github.com/paultendo/agent-notify) | Multi-agent completion notifications and remote webhook targets, including ntfy-oriented workflows. | You need a general remote-notification tool rather than a Codex-specific completion engine. |
| [flavio87/tap-to-tmux](https://github.com/flavio87/tap-to-tmux) | A phone/ntfy, SSH, and tmux-centered remote workflow. | The main interaction is returning from a phone notification to a tmux session. |
| [mhmdibrahimm/codex-goal-hooks](https://github.com/mhmdibrahimm/codex-goal-hooks) | Hooks around Codex goal status and lifecycle. | Goal-state changes themselves are the primary signal. |

## What the adjacent-project review changed

The lifecycle review behind 2.4 reinforced several engineering patterns:

- tail append-only lifecycle files incrementally and never advance past an incomplete JSONL line;
- persist watcher cursors so a restart does not replay all history;
- use atomic local state transitions instead of doing network work inside a hook;
- treat goal status as one signal rather than the only definition of completion;
- deduplicate and delay at a stable task/thread identity, not with one global cooldown;
- keep an explicit hook signal plus a persisted-state recovery path.

The presentation review also found a useful compact pattern across several adjacent tools: expose a short result excerpt, commonly in roughly the 120–200 character range, and use one semantic icon/tag instead of repeating decorative markers through the title and body. That review informed this repository's 180-character default excerpt, single `white_check_mark` tag, title containing only the task/project name, and label-free one-line context. It did not change 2.4's idle-only detection semantics.

The implementation in this repository combines those general lessons with its existing durable outbox and was written for this project. Feature ideas are not a claim of source-code reuse, compatibility, endorsement, or equivalent licensing.

## Where this repository is opinionated

Durable Codex ntfy notifier is a strong fit when these properties are needed together:

- ntfy is the primary delivery channel;
- “done” means the **root task is locally verifiable as idle**, not merely that one turn emitted a final response;
- modern `Stop`, legacy `notify`, and local rollout state should converge on the same candidate;
- an active goal or active descendant must delay the root notification;
- automatic continuations must supersede earlier candidates;
- concurrent app, VS Code, and CLI tasks must be isolated per root thread rather than merged by a global rate limit;
- a completion must survive in per-host state before network delivery;
- offline failures retry with backoff;
- Windows, WSL, Linux, and Remote SSH form one supported topology;
- final assistant content should be excluded by default.

Its deliberate tradeoffs:

- it is Codex-specific rather than universal;
- it depends on local Codex rollout/database formats for its strongest idle proof;
- `strict` can withhold a real notification when local evidence is missing;
- exact-task navigation is an opt-in HTTPS `click` target; native sounds, a notification center, and a central dashboard remain outside the project;
- installation manages both hook configuration and a background worker;
- queues are independent per host;
- delivery is at-least-once, not exactly-once;
- pure cloud tasks without mirrored local lifecycle state are outside its guarantee.

## Why common simpler strategies differ

### Publish every turn-complete event

This is compact and can be appropriate when every turn matters. It cannot distinguish an intermediate completion immediately followed by another `task_started`. Version 2.4 and later instead stage every signal in `pending/` and wait for logical idle.

### Add a global delay or rate limit

A delay can absorb a fast continuation, but a single global timer can suppress or merge unrelated tasks in concurrent VS Code windows. This project coalesces only records sharing the same root thread ID and also checks goal/descendant state.

### Ask the model to invoke a notification skill

An explicit skill can produce a useful summary and requires little background machinery. Its execution is prompt/model dependent. This project makes notification a lifecycle/worker decision and keeps message content disabled by default.

### Watch only the newest rollout

Selecting the globally newest file creates a race when several sessions are active. This project tracks each rollout path and byte offset independently, then keys candidates by their own thread/turn identity.

### Use goal status alone

An active goal is valuable negative evidence, but a task without a goal can still continue and a terminal goal can coexist briefly with pending child/turn state. This project combines goal status with root rollout lifecycle, descendant activity, and a quiet window.

## Lightweight alternative

For one machine with reliable networking, one task at a time, and no requirement to distinguish intermediate turns, a short custom hook that publishes directly to ntfy may be sufficient. It has less code and state, but no durable offline queue and no root-idle proof.

Use the smallest system matching the failure modes that matter. If another maintained project already fits the topology and privacy requirements, adopting it can be better than migrating for feature count alone.

## Evaluation checklist

Before choosing or combining tools:

1. Does “completion” mean a turn ended, a model produced a message, a goal became terminal, or the entire root task became idle?
2. Can an automatic continuation start after the signal being published?
3. Can active descendants delay their parent without producing their own alerts?
4. Are simultaneous sessions isolated by stable identity or merged by a global timer/lock?
5. What happens when the network is unavailable?
6. Can a missed hook be reconstructed from persisted local state?
7. What prompts, replies, paths, titles, IDs, goal fields, and credentials are read, stored, or sent?
8. Does it follow redirects while carrying authorization?
9. How are hooks reviewed and trusted, and does installation preserve unrelated handlers?
10. Does it run where Codex actually runs—app, local VS Code, CLI, WSL, container, or SSH host?
11. Are pure cloud tasks in scope?
12. Is the project license and maintenance model acceptable?

Feature overlap is expected and healthy. Corrections are welcome through a documentation pull request linking the relevant project’s current primary documentation.
