<img src="assets/cmux.png" alt="cmux" width="180" />

## A Claude Code multiplexer with session-to-session messaging.

Cmux lets you spawn and manage multiple persistent Claude Code agents that can send and receive messages to each other.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/mekarpeles/cmux/main/install.sh | bash
```

Requires tmux and pipx. The install script handles both on macOS (Homebrew) and Ubuntu/Debian.

## How it works

Cmux is built on three primitives:

- **claudio** — a lightweight IO wrapper that gives each Claude Code session an inbox. Each agent has a private message queue. Anything can write to it — another agent, a script, a human. When Claude is idle, the next message is delivered into the session.
- **tmux** — manages the sessions. Each agent runs in a named tmux window, persistent and re-attachable.
- **cmux** — the CLI that ties it together. Use it to start and manage claudio-wrapped Claude Code agents, and to send messages between them.

Running `cmux start <name>` launches a claudio-wrapped Claude Code session inside tmux, registers it by name, and starts listening for messages.

## Why cmux exists

Claude Code has two primary modes:
1. An interactive TUI for humans.
2. A JSON streaming mode for programmatic use.

Many long-lived CLI tools (tmux, emacs) solve this by running as servers that clients connect to. Cmux approximates this for Claude Code: a human can attach to a session via the TUI while other agents submit messages to its inbox. The result is a lightweight chat-room primitive — multiple agents and humans participating in the same session, however you'd like to wire them up.

Claude Code's experimental Agent Teams feature offers a related capability, but it is opinionated about orchestration and treats the multi-agent structure as a framework rather than a primitive. Cmux makes no assumptions about how agents relate to each other — it only gives each session an inbox and a name. You decide the topology. The goal is primitives and control.

## Usage

### Agents

**Start an agent** (starts and attaches):
```bash
cmux alice
```

**Start detached, with an initial prompt:**
```bash
cmux start alice -d -- "You are Alice, a project coordinator."
```

**List agents:**
```bash
cmux ls
```

**Attach / detach:**
```bash
cmux attach alice   # attach terminal
# Ctrl-b d          # detach (standard tmux keybinding)
```

**Stop an agent:**
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

Group agents into one tmux session as windows/tabs:

```bash
cmux -s myproject start alice -d -- "You are Alice."
cmux -s myproject start bob -d   -- "You are Bob."

cmux attach alice        # opens myproject on Alice's window
# Ctrl-b n / Ctrl-b p   # move between agent windows

cmux stop bob            # kills Bob's window; workspace and Alice stay alive
```

`cmux ls` shows agents grouped by workspace:

```
AGENT                WORKSPACE        STARTED
------------------------------------------------------------
alice                myproject        2026-06-15T10:00:00Z
bob                  myproject        2026-06-15T10:00:01Z
carol                -                2026-06-15T10:00:02Z
```

## Try it

Run a two-agent debate on any question:

```bash
bash examples/debate.sh "Is it better to be a generalist or a specialist?"
```

Alice argues in favor, Bob argues against. They exchange short paragraphs for three rounds. Attach to watch either agent live:

```bash
cmux attach alice   # Ctrl-b n to switch to bob
```

When done:

```bash
cmux stop alice && cmux stop bob
```

More examples in [`examples/`](examples/).
