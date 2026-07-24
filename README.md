# CodexStay

Keep Codex turns running after terminal or SSH disconnects.

CodexStay runs Codex in an isolated tmux session. When the active turn
finishes, it asks Codex to exit cleanly and removes the session.

## Features

- Keeps an active Codex turn running after terminal or SSH disconnects
- Starts and resumes Codex threads in protected tmux sessions
- Reads resume history through the Codex app-server, not private databases
- Cleans up completed sessions automatically

## Install

Requires Linux or macOS, Python 3.10+, the Codex CLI, and tmux 3.2+.

```bash
uv tool install git+https://github.com/wenbo-wei/codexstay.git
codexstay setup
```

On first launch, run `/hooks` in Codex and trust the generated `SessionStart`
hook.

## Usage

```bash
codexstay
codexstay resume
```

## Guarantees and limits

A turn already running under CodexStay survives a terminal or SSH client
disconnect. Completion sends `/exit` first; exact session termination is used
only as a fallback.

CodexStay cannot guarantee that an external task succeeds. Authentication or
network errors, approval prompts, and requests for user input may still pause
or end a turn. It does not survive a host reboot or power loss.

MIT licensed. Not affiliated with or endorsed by OpenAI.
