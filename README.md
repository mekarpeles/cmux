<img src="assets/cmux.png" alt="cmux" width="180" />

## A Claude Code multiplexer with agents that can message each other.

Cmux lets you spawn and manage multiple persistent Claude Code agents that can send and receive messages to each other.

## Install

Requires [Claude Code](https://claude.ai/code) to already be installed and authenticated on your machine.

```bash
curl -fsSL https://raw.githubusercontent.com/mekarpeles/cmux/main/install.sh | bash
```

The install script also handles tmux and pipx on macOS (Homebrew) and Ubuntu/Debian.

## How it works

Cmux is built on three primitives:

- **[claudio](https://github.com/mekarpeles/claudio)** — a lightweight IO wrapper that gives each Claude Code session an inbox. Each agent has a private message queue. Anything can write to it — another agent, a script, a human. When Claude is idle, the next message is delivered into the session.
- **[tmux](https://github.com/tmux/tmux)** — manages the sessions. Each agent runs in a named tmux window, persistent and re-attachable.
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

## Tutorial

Let's create a `standup` team with Alice, Bob, and Carol. Bob will check if there are any new GitHub issues today for the Open Library project. Carol will check if there are any new unassigned PRs. Both will report to Alice, who gives us a summary of what's new.

**Step 1 — Start the coordinator**

First, spin up a new claudio agent named Alice and pass them an initial prompt. We use `-d` to start all of this work detached in the background. We use `-s` to add Alice to the tmux workspace session called `standup`.

```bash
# -s standup: add to shared workspace  -d: start detached  --: begins the initial prompt
cmux -s standup start alice -d -- "You are Alice, a coordinator. Bob and Carol are already running as separate agents and will report to you shortly — do NOT start them yourself. Bob is checking for new GitHub issues on internetarchive/openlibrary. Carol is checking for unassigned open PRs. Wait for messages from both, then write a short executive summary (a few bullet points each)."
```

**Step 2 — Start the researchers**

```bash
cmux -s standup start bob -d -- "You are Bob. Run: gh issue list --repo internetarchive/openlibrary --state open --json number,title,createdAt --limit 50. Filter to issues created today. Summarise count and titles, then report to Alice: cmux send alice '<your summary>' --from bob"

cmux -s standup start carol -d -- "You are Carol. Run: gh pr list --repo internetarchive/openlibrary --state open --json number,title,assignees --limit 50. Filter to PRs with no assignees. Summarise count and titles, then report to Alice: cmux send alice '<your summary>' --from carol"
```

**Step 3 — Check what is running**

```bash
cmux ls
```

```
AGENT                WORKSPACE        STARTED
------------------------------------------------------------
alice                standup          2026-06-15T10:00:00Z
bob                  standup          2026-06-15T10:00:03Z
carol                standup          2026-06-15T10:00:06Z
```

**Step 4 — Watch the summary arrive**

```bash
cmux attach alice   # Ctrl-b n / Ctrl-b p to switch windows
```

Bob and Carol each run their queries, send results to Alice's inbox, and Alice compiles the summary once both reports are in. Detach at any time with `Ctrl-b d`.

**Step 5 — Clean up**

```bash
cmux stop alice && cmux stop bob && cmux stop carol
```

---

More examples in [`examples/`](examples/).
