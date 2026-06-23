# AGENTS.md — cmux project team

This file describes the humans and agents who maintain the cmux/claudio/cq ecosystem. It is the canonical reference for who does what, how to reach them, and what they own.

---

## Humans

| Name | Role | Contact |
|------|------|---------|
| Mek (Michael E. Karpeles) | Executive Director — final decision authority on architecture, scope, and releases | mek@archive.org |

Mek reviews all significant design changes. Agents should not push to main, create public releases, or make breaking API changes without Mek's sign-off.

---

## Agents

| Name | Role | Workspace | Repos |
|------|------|-----------|-------|
| lupin | The Looper — coordinator, health monitor, synthesises status across the team | standalone | cmux, claudio, cq |

### Lupin

Lupin is the primary orchestrator. Responsibilities:
- Run `cmux check` every 5 minutes via `ScheduleWakeup` and surface any `[STUCK]` agents to Mek
- Maintain a consolidated status view across all PAM agents
- Coordinate work on cmux, claudio, and cq — opening cq issues, delegating tasks, reporting back
- Gate releases: confirm tests pass and DESIGN.md / README.md are current before tagging

Lupin does **not** approve its own PRs, merge to main, or make decisions that affect other agents' sessions without checking with Mek first.

---

## Repositories we maintain

| Repo | Purpose | Primary language |
|------|---------|-----------------|
| [cmux](https://github.com/mekarpeles/cmux) | Claude Code multiplexer — tmux session management, agent registry, messaging | Python |
| [claudio](https://github.com/mekarpeles/claudio) | Inbox/socket primitive — message queue and delivery loop | Python |
| [cq](https://github.com/mekarpeles/cq) | Per-agent issue tracker — SQLite-backed, `gh`-compatible CLI | Python |

### Dependency order

```
claudio   (no cmux/cq dependency)
   ↑
cmux      (depends on claudio; optionally integrates cq via env vars)
   ↑
cq        (no cmux/claudio import; reads CMUX_SESSION_NAME / CMUX_STATE_DIR env vars only)
```

Changes to claudio may require updates to cmux. Changes to cmux do not affect cq. cq can be developed and released independently.

---

## How we work

### Issues and tasks

Project-level issues live in `~/Projects/cmux/.cq/` (cq, not cmux's built-in task queue). Use `cq issue` to create, view, and close work items. The cmux built-in task queue (`cmux task`) is being deprecated in favour of cq — do not add new tasks to it.

```bash
export CQ_STATE_DIR=~/Projects/cmux/.cq
cq issue list
cq issue create -t "..." -l bug
```

Per-agent personal queues live in `~/.cmux/{agent}/.cq/` and are managed by each agent independently.

### Pull requests

- All changes go through a PR — no direct pushes to main
- PRs require: tests passing, DESIGN.md updated if architecture changed, README updated if user-facing behaviour changed
- Lupin opens PRs for cmux/claudio/cq work; Mek reviews and merges

### Testing

Every repo has isolated tests that never touch live agent state:
- cmux: `python3 -m pytest tests/test_cmux.py -v` — uses `CMUX_STATE_DIR` temp dirs
- cq: `python3 -m pytest tests/test_cq.py -v` — uses `CQ_STATE_DIR` / `path=` temp dirs

Tests must pass before a PR is opened.

### Backwards compatibility

cmux and cq are used by active agents. Breaking changes require:
1. A deprecation period (old behaviour kept as alias or fallback)
2. A migration issue in cq tracking each affected agent
3. Mek's explicit sign-off

The `cmux start` / `cmux stop` aliases exist for this reason — `up` / `down` are primary but `start` / `stop` are not removed until all agents have migrated.

---

## Agent migration status

Agents need to be registered under the new `cmux up/down` model (home dir at `~/.cmux/{name}/`, entry in `agents.db`). Track progress in cq issue #2.

| Agent | Registered | Home dir | Using cq |
|-------|-----------|----------|----------|
| lupin | ✓ | ✓ | ✓ |
| fran | ✓ | ✓ | pending |
| pierre | ✓ | ✓ | pending |
| odie | ✓ | ✓ | pending |
| slater | ✓ | ✓ | pending |
| richy | ✓ | ✓ | pending |
| fonzie | ✓ | ✓ | pending |
| locke | ✓ | ✓ | pending |
| reno | ✓ | ✓ | pending |
| impa | ✓ | ✓ | pending |
| saul | ✓ | ✓ | pending |
| ester | ✓ | ✓ | pending |
| revere | ✓ | ✓ | pending |
| valerie | ✓ | ✓ | pending |

Update this table as agents are migrated. Do not mark an agent's home dir as done until `~/.cmux/{name}/` exists. Do not mark "using cq" until the agent has opened at least one cq issue in their own queue.

`cmux rm` de-registers an agent from `agents.db` but deliberately preserves `~/.cmux/{name}/`. The home dir contains cq history, logs, and scripts that have provenance value. Delete it manually if you need to reclaim space.

---

## Vision (not yet implemented)

Each agent is intended to become a **standalone distributable unit**: a directory containing `AGENTS.md`, scripts, a `.cq/` queue, and workflow definitions — portable enough to hand to another person or publish to an agent registry. Claude's conversation history (JSON session files) is excluded from the bundle; it is runtime state, not definition.

This is the direction, not the current state. Do not implement agenthub-style packaging until the migration above is complete and stable.
