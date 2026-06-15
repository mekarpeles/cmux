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
        for line in pane_content(target).split('\n'):
            if line.lstrip().startswith('❯'):
                return True
        return False
    return is_idle


def make_deliver(target: str):
    def deliver(msg: dict) -> None:
        sender = msg.get('from', 'unknown')
        label = f'[{sender}@cmux]' if sender != 'cmux' else '[cmux]'
        text = f'{label}: {msg["body"]}'
        subprocess.run(['tmux', 'send-keys', '-t', target, text])
        subprocess.run(['tmux', 'send-keys', '-t', target, '', 'Enter'])
    return deliver


def run(name: str, tmux_target: str = None) -> None:
    if tmux_target is None:
        tmux_target = f'cmux-{name}:{name}'
    claudio.run(
        name=name,
        deliver=make_deliver(tmux_target),
        is_idle=make_is_idle(tmux_target),
        state_dir=STATE_DIR,
    )


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: python3 -m cmux_lib.daemon <name> [tmux-target]', file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
