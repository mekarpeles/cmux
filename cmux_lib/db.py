"""cmux SQLite agent registry.

One active table:
  agents — persistent catalog of registered agents (survives stop/restart)

DB lives at ~/.cmux/agents.db alongside sessions.json.
WAL mode allows concurrent multi-agent reads + serialized writes at OS level.

Note: the `tasks` table still exists in the schema for backwards compatibility
with existing DB files, but the task queue CLI has been removed. Use cq instead.
"""

import os
import sqlite3
import time
import uuid

STATE_DIR = os.environ.get('CMUX_STATE_DIR', os.path.expanduser('~/.cmux'))
DB_PATH = os.path.join(STATE_DIR, 'agents.db')


def _conn():
    os.makedirs(STATE_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c


def init():
    """Create tables if they don't exist (idempotent)."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                name            TEXT PRIMARY KEY,
                role            TEXT,
                workspace       TEXT,
                workflow_path   TEXT,
                initial_prompt  TEXT,
                no_inject       INTEGER DEFAULT 0,
                unblock         INTEGER DEFAULT 0,
                registered_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                agent       TEXT NOT NULL,
                task        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                note        TEXT,
                created_by  TEXT DEFAULT 'mek',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
        """)
        # Migration: add unblock column to existing DBs that predate it.
        try:
            c.execute('ALTER TABLE agents ADD COLUMN unblock INTEGER DEFAULT 0')
        except Exception:
            pass  # column already exists


def register_agent(name, role=None, workspace=None, workflow_path=None,
                   initial_prompt=None, no_inject=False, unblock=False):
    """Register or update an agent (upsert on name)."""
    init()
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with _conn() as c:
        c.execute("""
            INSERT INTO agents
                (name, role, workspace, workflow_path, initial_prompt, no_inject, unblock, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                role=excluded.role,
                workspace=excluded.workspace,
                workflow_path=excluded.workflow_path,
                initial_prompt=excluded.initial_prompt,
                no_inject=excluded.no_inject,
                unblock=excluded.unblock
        """, (name, role, workspace, workflow_path, initial_prompt, int(no_inject), int(unblock), ts))


def get_agent(name):
    """Return agent registration dict or None."""
    init()
    with _conn() as c:
        row = c.execute('SELECT * FROM agents WHERE name = ?', (name,)).fetchone()
        return dict(row) if row else None


def list_agents():
    """Return all registered agents sorted by workspace (nulls last) then name."""
    init()
    with _conn() as c:
        rows = c.execute(
            'SELECT * FROM agents ORDER BY workspace IS NULL, workspace, name'
        ).fetchall()
        return [dict(r) for r in rows]


def agents_in_workspace(workspace):
    """Return all agents registered to a specific workspace."""
    init()
    with _conn() as c:
        rows = c.execute(
            'SELECT * FROM agents WHERE workspace = ? ORDER BY name', (workspace,)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_agent(name):
    """Remove agent from catalog (does not stop a running agent)."""
    init()
    with _conn() as c:
        c.execute('DELETE FROM agents WHERE name = ?', (name,))
