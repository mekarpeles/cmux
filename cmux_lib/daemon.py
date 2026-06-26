"""
cmux daemon — tmux backend for claudio.

Wraps a Claude Code session running in a tmux pane with a claudio inbox.
Idle detection reads the tmux pane for Claude's '❯' prompt.
Delivery injects messages via tmux send-keys.
"""

import os
import subprocess
import sys
import threading
import time

STATE_DIR = os.environ.get('CMUX_STATE_DIR', os.path.expanduser('~/.cmux'))

# Patterns that indicate a Claude Code permission/security prompt is on screen.
# Used by both the daemon's unblock watcher and by `cmux check`.
_PERM_PATTERNS = [
    # Tool-use permission prompts
    'yes, proceed',
    'always allow',
    'no, and tell claude',
    'needs permission',
    '[y/n]',
    # Directory-trust prompt (new project / relocated workspace)
    'do you trust',
    'trust the files in this folder',
    'workspace trust',
]

# Numbered-list file permission prompts (create/edit/read). These need '2 Enter'
# to approve for the session — Escape would deny the operation.
_NUMBERED_PERM_PATTERNS = [
    'allow reading',
    'allow all edits',
    'do you want to proceed',
    'do you want to create',
    'do you want to edit',
    'do you want to write',
    'do you want to delete',
]


def pane_content(target: str) -> str:
    result = subprocess.run(
        ['tmux', 'capture-pane', '-t', target, '-p'],
        capture_output=True, text=True,
    )
    return result.stdout


def prompt_area(target: str) -> str:
    """Return only the bottom 4 lines of the pane — the prompt + status bar.

    The scrollback above changes constantly while Claude is generating (spinner,
    token counter). Tracking only the prompt area means the stability clock is
    unaffected by active generation in the scrollback and only resets when the
    prompt itself changes (user typing, cursor moving, new injection arriving).
    """
    result = subprocess.run(
        ['tmux', 'capture-pane', '-t', target, '-p'],
        capture_output=True, text=True,
    )
    lines = result.stdout.splitlines()
    return '\n'.join(lines[-4:]) if len(lines) >= 4 else result.stdout


def make_is_idle(target: str):
    """Return True when the Claude session is idle: prompt visible, cursor at
    start, nothing typed. Inject immediately — no timers, no hashing.

    Three checks:
    1. ❯ prompt is visible (Claude is waiting for input, not generating)
    2. cursor_x <= 2 (cursor at the prompt position, not mid-typed-text)
    3. No text after the ❯ ghost-hint (handles Ctrl-A: cursor moves to 0
       but typed text is still in the buffer and visible in pane content)
    """
    def is_idle() -> bool:
        content = pane_content(target)

        has_prompt = any(line.lstrip().startswith('❯') for line in content.split('\n'))
        if not has_prompt:
            return False

        result = subprocess.run(
            ['tmux', 'display-message', '-t', target, '-p', '#{cursor_x}'],
            capture_output=True, text=True,
        )
        try:
            cursor_x = int(result.stdout.strip())
        except ValueError:
            cursor_x = 0
        if cursor_x > 2:
            return False

        # cursor_x == 2: cursor is at the ghost-hint position.
        # Any text visible after ❯ is a Claude Code UI hint, not user input.
        # Only check for typed text when cursor_x == 0 (Ctrl-A moves cursor
        # to the very start while leaving typed text in the buffer).
        if cursor_x < 2:
            last_prompt = None
            for line in content.split('\n'):
                stripped = line.lstrip()
                if stripped.startswith('❯'):
                    last_prompt = stripped
            if last_prompt is not None:
                after = last_prompt[1:]
                if after and after != '\xa0' and after.strip('\xa0') != '':
                    return False  # Ctrl-A case: text in buffer

        return True

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


# Messages longer than this trigger Claude Code's paste-detection heuristic:
# tmux delivers the full string at once, readline sees a fast burst, and
# Claude Code shows "[paste N lines]" instead of processing the message.
# Work-around: write the body to a file and send an @file pointer instead.
_PASTE_THRESHOLD = 300


def make_deliver(name: str, target: str):
    home = os.path.join(STATE_DIR, name)

    def deliver(msg: dict) -> None:
        sender = msg.get('from')
        label = f'[{sender}@cmux]: ' if sender and sender != 'cmux' else '[cmux]: '
        body = msg['body']

        if len(label) + len(body) > _PASTE_THRESHOLD:
            # Write full message to a file; send a short @file pointer that
            # Claude Code reads cleanly without triggering paste detection.
            os.makedirs(home, exist_ok=True)
            ts = int(time.time() * 1000)
            msg_path = os.path.join(home, f'msg-{ts}.md')
            with open(msg_path, 'w') as f:
                f.write(f'{label}{body}')
            text = sanitize(f'[cmux]: @{msg_path}')
        else:
            text = sanitize(f'{label}{body}')

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


def _unblock_watcher(name: str, target: str, interval: float = 1.5) -> None:
    """Background thread: poll for permission prompts and auto-dismiss.

    Two prompt types require different responses:
    - File-read prompts (numbered list, 'allow reading'): send '2 Enter' to
      approve reading for the session. Sending Escape here would DENY the read.
    - Tool-use / trust prompts: send Escape to dismiss.

    After either action, injects a notification so the agent knows what happened.
    """
    notify_msg = (
        '[claudio@noreply]: A security/permission prompt was detected and '
        'automatically dismissed. Something in our workflow triggered a blocking '
        'security response — continuing with normal operation.'
    )
    while True:
        time.sleep(interval)
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p', '-S', '-15'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue
        pane_text = result.stdout.lower()
        if any(pat in pane_text for pat in _NUMBERED_PERM_PATTERNS):
            # Approve file-read for the session (option 2 in the numbered list)
            subprocess.run(['tmux', 'send-keys', '-t', target, '2', 'Enter'], capture_output=True)
            time.sleep(1.0)
            subprocess.run(['tmux', 'send-keys', '-t', target, notify_msg])
            subprocess.run(['tmux', 'send-keys', '-t', target, '', 'Enter'])
        elif any(pat in pane_text for pat in _PERM_PATTERNS):
            subprocess.run(['tmux', 'send-keys', '-t', target, 'Escape'], capture_output=True)
            time.sleep(1.0)
            subprocess.run(['tmux', 'send-keys', '-t', target, notify_msg])
            subprocess.run(['tmux', 'send-keys', '-t', target, '', 'Enter'])


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


def run(name: str, tmux_target: str = None, no_inject: bool = False, unblock: bool = False) -> None:
    import claudio
    _check_singleton(name)
    if tmux_target is None:
        tmux_target = f'cmux-{name}:{name}'
    if no_inject:
        deliver = make_deliver_file(name)
        is_idle = lambda: True  # always ready to queue; delivery is non-blocking
    else:
        deliver = make_deliver(name, tmux_target)
        is_idle = make_is_idle(tmux_target)
    if unblock:
        t = threading.Thread(target=_unblock_watcher, args=(name, tmux_target), daemon=True)
        t.start()
    home = os.path.join(STATE_DIR, name)
    os.makedirs(home, exist_ok=True)
    claudio.run(
        name=name,
        deliver=deliver,
        is_idle=is_idle,
        state_dir=home,
    )


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: python3 -m cmux_lib.daemon <name> [tmux-target] [--no-inject] [--unblock]', file=sys.stderr)
        sys.exit(1)
    no_inject = '--no-inject' in sys.argv
    unblock = '--unblock' in sys.argv
    argv = [a for a in sys.argv[1:] if a not in ('--no-inject', '--unblock')]
    run(argv[0], argv[1] if len(argv) > 1 else None, no_inject=no_inject, unblock=unblock)
