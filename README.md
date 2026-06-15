# cmux

<img src="assets/cmux.png" alt="cmux" width="180" />

Claude Code multiplexing with session-to-session messaging.

Cmux wraps Claude Code sessions in a thin layer with a personal message queue, letting you run multiple persistent Claude Code sessions that can speak to each other.

## How it works

Claude Code has two main modes: an interactive TUI and a JSON streaming mode. Cmux takes the TUI and gives it a server-like quality — each session gets a Unix socket queue that any process can write to. When a message arrives, cmux waits until no one is actively using the session (detected by watching for Claude's idle prompt), then injects the message directly into the TUI, clearly labelled with the sender. The result: persistent, named Claude sessions that can receive messages from other agents, scripts, or humans without interrupting active work.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/mekarpeles/cmux/main/install.sh | bash
```

Requires tmux and pipx. The install script handles both on macOS (Homebrew) and Ubuntu/Debian.

## CLI

The cmux CLI is designed to feel like tmux and Claude Code combined. Sessions are named, persistent, and addressable.

### Sessions

**Start a session** (starts and attaches):
```bash
cmux alice
```

**Start detached, with an initial prompt:**
```bash
cmux start alice -d -- "You are Alice, a project coordinator."
```

**List sessions:**
```bash
cmux ls
```

**Attach / detach:**
```bash
cmux attach alice   # attach terminal
# Ctrl-b d          # detach (standard tmux keybinding)
```

**Stop a session:**
```bash
cmux stop alice
```

### Messaging

```bash
cmux send alice "what is the status of the build?"
```

Messages are delivered as `[sender@cmux]: <message>` once Claude is idle. The sender is auto-detected when sending from inside a cmux session; pass `--from <name>` to set it explicitly:

```bash
cmux send alice "the tests are passing" --from bob
```

### Workspaces

Group multiple sessions as windows inside a single tmux session:

```bash
cmux -s myproject start alice -d -- "You are Alice."
cmux -s myproject start bob -d   -- "You are Bob."

cmux attach alice        # opens myproject on Alice's window
# Ctrl-b n / Ctrl-b p   # move between windows

cmux stop bob            # kills Bob's window; workspace and Alice stay alive
```

`cmux ls` shows sessions grouped by workspace:

```
workspace: myproject
  alice              running  2026-06-15T10:00:00Z
  bob                running  2026-06-15T10:00:01Z
```

## Preview

*Coming soon.*
