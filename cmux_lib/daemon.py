"""
Queue daemon for a single cmux session.

Listens on a Unix socket for incoming messages, then injects them into
the tmux pane when Claude is idle (bare ❯ prompt, nothing typed after it).

Usage (internal — called by `cmux start`):
    python3 -m cmux_lib.daemon <session-name>
"""

import json
import os
import socket
import subprocess
import sys
import time
import threading
from collections import deque

STATE_DIR = os.path.expanduser('~/.cmux')
POLL_INTERVAL = 0.5   # seconds between idle checks
HUMAN_IDLE_SECS = 3.0  # wait this long after last inject before injecting again


def tmux_session(name):
    return f'cmux-{name}'


def pane_content(target):
    """Return the visible lines of the tmux pane at target (session:window)."""
    result = subprocess.run(
        ['tmux', 'capture-pane', '-t', target, '-p'],
        capture_output=True, text=True
    )
    return result.stdout


def is_idle(target):
    """
    True when Claude is at the input prompt (not processing).

    Scans the visible pane for a line starting with '❯'. Claude Code's TUI
    places the prompt above a separator and a status bar, so we can't just
    check the last line. We scan all visible lines and return True if any
    starts with '❯' (ASCII or after lstrip).
    """
    for line in pane_content(target).split('\n'):
        if line.lstrip().startswith('❯'):
            return True
    return False


def inject(target, msg):
    """Send a formatted message into the tmux pane via two send-keys calls."""
    sender = msg.get('from', 'unknown')
    body = msg['body']
    label = f'[{sender}@cmux]' if sender != 'cmux' else '[cmux]'
    text = f'{label}: {body}'
    subprocess.run(['tmux', 'send-keys', '-t', target, text])
    subprocess.run(['tmux', 'send-keys', '-t', target, '', 'Enter'])


def socket_path(name):
    return os.path.join(STATE_DIR, f'{name}.sock')


def run(name, tmux_target=None):
    if tmux_target is None:
        tmux_target = f'cmux-{name}:{name}'
    os.makedirs(STATE_DIR, exist_ok=True)
    sock = socket_path(name)
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass

    queue = deque()
    lock = threading.Lock()

    # --- Socket server thread ---
    def serve():
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock)
        srv.listen(20)
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                continue
            try:
                data = conn.recv(65536)
                if data:
                    with lock:
                        queue.append(json.loads(data))
                    conn.sendall(json.dumps({'ok': True}).encode())
                conn.close()
            except Exception as e:
                try:
                    conn.sendall(json.dumps({'ok': False, 'error': str(e)}).encode())
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=serve, daemon=True).start()

    # --- Inject loop (main thread) ---
    while True:
        time.sleep(POLL_INTERVAL)
        with lock:
            if not queue:
                continue
        if not is_idle(tmux_target):
            continue
        with lock:
            msg = queue.popleft()
        inject(tmux_target, msg)
        # Brief pause so Claude registers the injected message before we check again
        time.sleep(1.0)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: python3 -m cmux_lib.daemon <name> [tmux-target]', file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
