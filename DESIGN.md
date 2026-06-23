# cmux Design Notes

Architectural decisions, internals, and lessons learned. Intended for contributors and for AI agents working on this codebase.

---

## Two-layer state model

cmux separates **runtime state** from **persistent catalog**:

| File | Purpose | Lifetime |
|------|---------|----------|
| `~/.cmux/sessions.json` | Running agents — tmux targets, daemon PIDs, socket paths | Cleared on stop; pruned on `cmux ls` if session died |
| `~/.cmux/agents.db` | Registered agents — role, workspace, workflow, initial_prompt | Survives stop/restart/reboot; deleted only by `cmux agent rm` |

This mirrors Docker's lifecycle model:

| Docker | cmux |
|--------|------|
| `docker pull` / Dockerfile | `cmux up <name>` (first run) |
| `docker run` (existing) | `cmux up <name>` (upserts DB, restores from DB) |
| `docker stop` | `cmux down <name>` — preserves DB + home dir |
| `docker rm` | `cmux rm <name>` — de-registers from DB, preserves home dir |
| `docker ps` | `cmux ls` (running section) |
| `docker ps -a` | `cmux ls` (all agents — running + stopped) |

`cmux start` / `cmux stop` are backwards-compat aliases for `up` / `down`.

**Key distinctions from Docker:**
- Every `cmux up` upserts to `agents.db` and creates `~/.cmux/{name}/` — there is no separate "register first" step. All agents are persistent.
- `cmux rm` de-registers from `agents.db` but **preserves** `~/.cmux/{name}/` (cq history, notes, scripts have provenance value). Delete the dir manually to reclaim space.
- Task queue entries **survive** `cmux down` — pending tasks are waiting when the agent restarts.
- Claude conversation history is **not** preserved — each `cmux up` is a fresh Claude Code process. There is no `--resume` or snapshot capability today.
- `cmux -s ol-loop` with no subcommand restarts every agent registered to that workspace — the post-reboot restore pattern.

---

## SQLite (agents.db)

**Why SQLite over flat JSONL**: multiple agents writing to the same queue file simultaneously produces race conditions and corrupt JSON. SQLite WAL (Write-Ahead Logging) mode gives OS-level concurrent reads with serialized writes — no application-level locking needed.

```python
c.execute('PRAGMA journal_mode=WAL')
```

**Schema** (`cmux_lib/db.py`):

```sql
agents  -- persistent catalog; upsert on name (ON CONFLICT DO UPDATE)
tasks   -- legacy; kept in schema for existing DB compatibility but no longer used by CLI
```

Task tracking has moved to `cq` (per-agent issue tracker). The `tasks` table remains in the schema so existing `agents.db` files don't break on `init()`, but all task CLI commands (`cmux task add/list/done/update`) have been removed. Use `cq issue create/list` instead.

---

## Message delivery pipeline

```
cmux send alice "msg"
  → Unix socket → daemon (claudio.run loop)
    → waits for is_idle()
      → tmux send-keys (normal mode)
      → appends to inbox.jsonl (--no-inject mode)
```

**`is_idle()` — three checks** (in `daemon.py:make_is_idle`):
1. `❯` prompt visible in pane content (Claude is waiting, not generating)
2. `cursor_x <= 2` — cursor at the prompt position, not mid-typed input
3. No text after `❯` beyond the ghost-hint — handles `Ctrl-A` edge case where cursor moves to 0 but typed text remains in the buffer

Only the bottom 4 lines are tracked for stability, not the full scrollback. This prevents the spinner and token counter (which change constantly during generation) from triggering false "busy" signals.

**`--no-inject` mode**: used for coordinator sessions (like Lupin) where Mek is actively typing. `send-keys` injection into an active pane is racy and corrupts Mek's input buffer. In `--no-inject` mode, the daemon appends to `~/.cmux/{name}.inbox.jsonl`; the agent reads it with `cmux inbox <name>`.

---

## Message length limits and fragility

Two limits apply to `cmux send`:

| Limit | Value | Where enforced | Failure mode |
|-------|-------|----------------|--------------|
| Hard limit | 2000 chars | `cmd_send()` — rejects before socket | Error + `sys.exit(1)` |
| Paste-detection soft limit | ~400 chars | Claude Code TUI behavior | Shows `[paste N lines]` instead of content |

The paste-detection threshold is empirical, not a hard tmux limit. Claude Code's TUI detects large text blocks as paste events. The threshold can shift across Claude Code versions.

For messages over ~400 chars, use the **file-based pattern**: write content to a state file, send a short notification pointer via `cmux send`.

**Known fragility in the delivery pipeline:**

- `sanitize()` collapses all whitespace (including `\n`, `\t`) to a single space — multi-line messages arrive flat. This is intentional (`\n` in `send-keys` = Enter keystroke), but callers must know their formatting is stripped.
- No retry logic anywhere — `cmd_send()` makes one socket connection attempt; failure is immediate exit.
- `is_idle()` polling interval is controlled by claudio, not configurable from cmux.
- If Claude Code is updated and changes its prompt character or ghost-hint format, `is_idle()` breaks silently — the three checks (`❯` visible, `cursor_x ≤ 2`, no text after `❯`) are all heuristic.
- A human scrolling or typing in an attached pane can delay or prevent message delivery (cursor position check fires false).
- Daemon process death leaves a stale PID file — `_check_singleton()` in `daemon.py` handles this on next start, but messages queued during the gap are lost.

---

## `CMUX_SESSION_NAME` env var

Set when starting Claude: `CMUX_SESSION_NAME={name} claude`. This means any agent running inside cmux can call `cmux send <other-agent> "msg"` without passing `--from` — the sender name is auto-detected from the environment. `cmd_send()` reads it as the default sender.

---

## Workspace vs standalone topology

| Mode | tmux session | Stop behavior |
|------|-------------|---------------|
| Workspace (`-s ol-loop`) | Shared session `ol-loop`, one window per agent | `cmux stop fran` kills only fran's window; other windows stay |
| Standalone (no `-s`) | Own session `cmux-{name}` | `cmux stop fran` kills the whole session |

`cmux -s ol-loop` with no further subcommand restarts all agents registered to `ol-loop` that aren't currently alive — useful after a reboot.

---

## Permission-prompt detection (`cmux check`)

Agents get silently stuck at Claude Code's Allow/Deny permission dialogs for hours. `cmux check` inspects all running agents non-destructively:

```bash
tmux capture-pane -t <target> -p -S -15   # last 15 lines of scrollback
```

Patterns searched (case-insensitive) in `_PERM_PATTERNS` (defined in `daemon.py`, imported by `cli.py`):
- `yes, proceed`
- `always allow`
- `no, and tell claude`
- `needs permission`
- `[y/n]`

Output is `[STUCK]` or `[OK]` per agent; prints the tmux target for quick `tmux attach`. Read-only — never modifies any session.

**Intended use**: Lupin uses `ScheduleWakeup(delaySeconds=300)` every 5 minutes to run `cmux check`, and surfaces any `[STUCK]` agents in the consolidated status view. Mek decides Allow/Deny.

---

## Session continuity (`--continue`)

Every `cmux up` passes `--continue` to Claude Code unconditionally. If a prior session exists for that CWD, it is resumed. If not (first start, or session was never saved), Claude Code starts fresh — `--continue` is a no-op in that case, not an error.

**Scaffolding** — files written to `~/.cmux/{name}/` when missing (no "first start" detection needed):
- `identity.md` — written from `initial_prompt` if the file doesn't exist yet. The agent can edit this file; cmux will never overwrite it.
- `MIGRATE.md` — brain migration checklist, written once.

On every `cmux up`, after the socket is ready, cmux injects two messages:
1. Orientation (name, home dir, cq, messaging protocol) — same message every time.
2. Contents of `identity.md` — so role context is always at the top of the conversation, even after heavy session compaction.

**Why inject identity.md on every start, not just first start:** Session compaction can evict the agent's role definition from the active context window. Injecting it on every startup is cheap and ensures the agent always knows who they are, regardless of history depth.

**Shared workspace caveat**: `--continue` resumes by CWD. Agents that share a workspace (`~/Projects/pm`) all have sessions in the same `~/.claude/projects/<hash>/` directory. Claude Code picks the most recent session, which may be a different agent's. In practice this is acceptable (each agent's session is their own process), but the clean fix is `--resume <session-id>` with a per-agent stored ID — a future improvement.

---

## `--unblock` mode

For fully autonomous agents that should never wait for human permission approval, `--unblock` starts a background watcher thread alongside the daemon:

```python
# daemon.py: _unblock_watcher(name, target, interval=1.5)
while True:
    time.sleep(interval)
    pane_text = tmux capture-pane (last 15 lines, lowercased)
    if any(_PERM_PATTERN in pane_text):
        tmux send-keys Escape
        time.sleep(1.0)
        tmux send-keys "[claudio@noreply]: ..." Enter
```

The 1-second pause between Escape and the notification gives Claude Code time to dismiss the dialog before the next input arrives. The sender `[claudio@noreply]` is a reserved internal sender — agents should treat any message from this address as a system notification, not a conversational turn from another agent.

**Storage**: `unblock INTEGER DEFAULT 0` in `agents.db`. Migration is applied automatically (ALTER TABLE ADD COLUMN on init) for existing DBs that predate the column.

**Safety note**: `--unblock` bypasses every security confirmation Claude Code would normally show. It is intentionally opt-in per agent, not global. Do not use it for agents with write access to sensitive systems unless you understand the implications.

---

## Installation (pipx editable install)

cmux is installed via `pipx install -e .`. On macOS, system Python rejects `pip install -e .` with PEP 668. The pipx editable install means **source file changes in `cmux_lib/` take effect immediately** — no reinstall needed. Verify with `cmux --version` or `which cmux`.

---

## `KNOWN_AGENTS` dict (cli.py)

Hardcoded metadata (role, workspace, workflow path) for the 14 PAM agents. Used by `cmux agent import-sessions` to bootstrap `agents.db` from `sessions.json`. Any agent in `KNOWN_AGENTS` but absent from `sessions.json` is still registered with role/workspace metadata, enabling `cmux start <name>` to work post-reboot.

Keep this dict in sync with `~/Projects/pm/AGENTS.md` when the PAM team roster changes.

---

## sanitize() in daemon.py

`tmux send-keys` interprets `\n` as Enter, which splits a multi-line message into multiple submissions — corrupting the delivery. `sanitize()` collapses all whitespace (including `\n`, `\r`, `\t`) to a single space and strips remaining ASCII control characters before injection.

---

## claudio dependency

cmux delegates the inbox/socket loop to `claudio` (`github.com/mekarpeles/claudio`). cmux adds: tmux session management, workspace grouping, the `is_idle()` detection, persistent agent registry, task queue, and health checking. The underlying message queue primitive is claudio's.

When cmux's `STATE_DIR` differs from claudio's default, the daemon passes `state_dir=STATE_DIR` explicitly to `claudio.run()`.
