"""
cmux daemon — tmux backend for claudio.

Wraps a Claude Code session running in a tmux pane with a claudio inbox.
Idle detection reads the tmux pane for Claude's '❯' prompt.
Delivery injects messages via tmux send-keys.
"""

import os
import subprocess
import sys

import claudio

STATE_DIR = os.environ.get('CMUX_STATE_DIR', claudio.agent.DEFAULT_STATE_DIR.replace('.claudio', '.cmux'))


def pane_content(target: str) -> str:
    result = subprocess.run(
        ['tmux', 'capture-pane', '-t', target, '-p'],
        capture_output=True, text=True,
    )
    return result.stdout


def make_is_idle(target: str):
    def is_idle() -> bool:
        # Find the last ❯ line — it's the current prompt (Claude's TUI keeps it at the bottom).
        # Earlier ❯ lines may contain previously-injected text and should be ignored.
        last_prompt = None
        for line in pane_content(target).split('\n'):
            stripped = line.lstrip()
            if stripped.startswith('❯'):
                last_prompt = stripped
        if last_prompt is None:
            return False
        after = last_prompt[1:]
        # Idle if nothing follows ❯, or if the next char is NBSP (\xa0) — that's
        # Claude Code's ghost/placeholder hint, not user input.
        return after == '' or after[0:1] == '\xa0'
    return is_idle


def sanitize(text: str) -> str:
    """Remove or replace characters that confuse tmux send-keys.

    tmux interprets \\n as Enter (splits the message into multiple
    submissions) and may mishandle other ASCII control characters.
    Collapse all whitespace sequences to a single space and strip
    remaining control chars.
    """
    import re
    # Collapse any run of whitespace (including \n, \r, \t) to a single space
    text = re.sub(r'\s+', ' ', text)
    # Strip ASCII control characters (0x00-0x1f, 0x7f) except space (0x20)
    text = re.sub(r'[\x00-\x1f\x7f]', '', text)
    return text.strip()


def make_deliver(target: str):
    def deliver(msg: dict) -> None:
        sender = msg.get('from')
        label = f'[{sender}@cmux]: ' if sender and sender != 'cmux' else ''
        text = sanitize(f'{label}{msg["body"]}')
        subprocess.run(['tmux', 'send-keys', '-t', target, text])
        subprocess.run(['tmux', 'send-keys', '-t', target, '', 'Enter'])
    return deliver


def make_deliver_file(name: str):
    """File-only delivery — appends to inbox JSONL instead of injecting via send-keys.

    Used for coordinator sessions (e.g. lupus) where Mek is actively typing in the pane.
    Send-keys injection into such a pane is inherently racy and corrupts Mek's input buffer.
    The agent reads its inbox via `cmux inbox <name>`.
    """
    import json as _json
    inbox_path = os.path.join(STATE_DIR, f'{name}.inbox.jsonl')

    def deliver(msg: dict) -> None:
        with open(inbox_path, 'a') as f:
            f.write(_json.dumps(msg) + '\n')

    return deliver


def _check_singleton(name: str) -> None:
    """Exit if a daemon for this agent is already running."""
    pid_file = os.path.join(STATE_DIR, f'{name}.daemon.pid')
    try:
        existing_pid = int(open(pid_file).read().strip())
        # Check if process is actually alive
        os.kill(existing_pid, 0)
        print(
            f'cmux: daemon for {name!r} already running (pid {existing_pid}). '
            f'Stop it first: cmux stop {name}',
            file=sys.stderr,
        )
        sys.exit(1)
    except (FileNotFoundError, ValueError):
        pass  # no pid file — first start
    except ProcessLookupError:
        pass  # stale pid file — process is gone, safe to proceed

    # Write our own PID
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))


def run(name: str, tmux_target: str = None, no_inject: bool = False) -> None:
    _check_singleton(name)
    if tmux_target is None:
        tmux_target = f'cmux-{name}:{name}'
    if no_inject:
        deliver = make_deliver_file(name)
        is_idle = lambda: True  # always ready to queue; delivery is non-blocking
    else:
        deliver = make_deliver(tmux_target)
        is_idle = make_is_idle(tmux_target)
    claudio.run(
        name=name,
        deliver=deliver,
        is_idle=is_idle,
        state_dir=STATE_DIR,
    )


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: python3 -m cmux_lib.daemon <name> [tmux-target] [--no-inject]', file=sys.stderr)
        sys.exit(1)
    no_inject = '--no-inject' in sys.argv
    argv = [a for a in sys.argv[1:] if a != '--no-inject']
    run(argv[0], argv[1] if len(argv) > 1 else None, no_inject=no_inject)
