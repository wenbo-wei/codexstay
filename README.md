# Codex Stay

Keeps an active Codex turn running after terminal or SSH disconnects.

## Install

Requires Linux or macOS, Python 3.10+, the Codex CLI, and tmux 3.2+.

```bash
uv tool install git+https://github.com/wenbo-wei/codex-stay.git
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

A turn already running under Codex Stay survives a terminal or SSH client
disconnect.

Codex Stay does not guarantee task success. Authentication or network errors,
approval prompts, and requests for user input may pause or end a turn. A host
reboot or power loss ends it.

MIT licensed. Not affiliated with or endorsed by OpenAI.
