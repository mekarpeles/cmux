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
]

# Folder-trust dialog ("Quick safety check: Is this a project you created or
# one you trust?"). Distinct from _PERM_PATTERNS because its default option
# is "1. Yes, I trust this folder" — Enter accepts it. Sending Escape here
# (the _PERM_PATTERNS response) hits "Esc to cancel", which quits Claude and
# kills the whole tmux window instead of unblocking it.
_TRUST_DIALOG_PATTERNS = [
    'quick safety check',
    'yes, i trust this folder',
    # Older/alternate phrasings, kept for forward/backward compatibility.
    'do you trust',
    'trust the files in this folder',
    'workspace trust',
]

# Three-option prompts where option 2 is "Yes, allow for session".
# Send '2' to approve for the whole session.
# Also includes the --permission-mode bypassPermissions confirmation screen.
_ALLOW_SESSION_PATTERNS = [
    'allow reading',
    'allow all edits',
    'allow all',
    'bypass permissions mode',  # --permission-mode bypassPermissions startup confirmation
]

# Two-option prompts (1. Yes / 2. No) — bash commands, simple confirmations.
# Send '1' to approve once. Sending '2' here would DENY the operation.
_APPROVE_ONCE_PATTERNS = [
    'do you want to proceed',
    'do you want to create',
    'do you want to edit',
    'do you want to write',
    'do you want to delete',
    'do you want to run',
    'do you want to execute',
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


# Messages longer than this are redirected to an @file pointer. Historically
# this dodged Claude Code's paste-detection heuristic; with bracketed-paste
# injection (below) the heuristic no longer matters, but the redirect is kept
# so huge bodies don't bloat the input line and the msg-*.md files remain an
# on-disk record of what was delivered.
_PASTE_THRESHOLD = 300

# How many times to press Enter (with growing waits) before declaring a
# delivery stuck. Submission is VERIFIED, not assumed — see _submitted().
_SUBMIT_RETRIES = 5


def _inject_text(target: str, text: str) -> None:
    """Put text into the Claude input via tmux bracketed paste.

    Why not plain send-keys: send-keys delivers the string as one
    instantaneous key burst. Claude Code's paste-detection heuristic sees the
    burst, enters paste-capture, and an Enter sent immediately afterwards is
    often captured INTO the paste as a newline instead of submitting — the
    message is left sitting unsubmitted in the input box (the long-standing
    "stuck message" bug). A literal '@path' typed as keystrokes also pops the
    file-mention autocomplete, where Enter selects the completion instead of
    submitting.

    Bracketed paste (paste-buffer -p) fixes both: the text arrives inside an
    explicit ESC[200~ … ESC[201~ envelope, so the TUI knows exactly where the
    paste ends — a subsequent Enter is unambiguously a keypress — and pasted
    '@' does not trigger the autocomplete popup.
    """
    loaded = subprocess.run(
        ['tmux', 'load-buffer', '-b', 'cmux-deliver', '-'],
        input=text.encode(),
    )
    if loaded.returncode == 0:
        subprocess.run(
            ['tmux', 'paste-buffer', '-p', '-d', '-b', 'cmux-deliver', '-t', target],
        )
    else:
        # Fallback: legacy typed injection (-l = literal, never key-name lookup).
        subprocess.run(['tmux', 'send-keys', '-t', target, '-l', text])


def _submitted(target: str, text: str) -> bool:
    """True once the injected text is no longer sitting in the input line.

    Checks the pane from the LAST ❯ prompt downward (the input box, including
    wrapped continuation rows) for a probe prefix of the injected text. The
    conversation echo of a successfully submitted message sits ABOVE the
    prompt, so it never false-positives; the empty-input ghost hint doesn't
    contain the probe either. If no prompt is visible at all (redraw, screen
    switch), report submitted rather than retry-looping blind.
    """
    content = pane_content(target)
    lines = content.split('\n')
    prompt_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith('❯'):
            prompt_idx = i
    if prompt_idx is None:
        return True
    probe = text[:24]
    input_region = '\n'.join(lines[prompt_idx:])
    return probe not in input_region


def _submit(target: str, text: str) -> bool:
    """Press Enter until the injected text actually leaves the input line.

    A single Enter can be swallowed (paste-capture window, autocomplete
    popup, mid-redraw). Verify-and-retry converts "usually submits" into
    "submits or tells you it couldn't": each attempt presses Enter, waits
    (growing backoff — also lets a popup/paste window close), and re-checks
    the pane. Returns True on verified submission.
    """
    for attempt in range(_SUBMIT_RETRIES):
        subprocess.run(['tmux', 'send-keys', '-t', target, 'Enter'])
        time.sleep(0.4 + 0.3 * attempt)
        if _submitted(target, text):
            return True
    return False


def make_deliver(name: str, target: str):
    home = os.path.join(STATE_DIR, name)

    def deliver(msg: dict) -> None:
        sender = msg.get('from')
        label = f'[{sender}@cmux]: ' if sender and sender != 'cmux' else '[cmux]: '
        body = msg['body']

        if len(label) + len(body) > _PASTE_THRESHOLD:
            # Write full message to a file; send a short @file pointer that
            # Claude Code reads on its own (and that doubles as a record).
            os.makedirs(home, exist_ok=True)
            ts = int(time.time() * 1000)
            msg_path = os.path.join(home, f'msg-{ts}.md')
            with open(msg_path, 'w') as f:
                f.write(f'{label}{body}')
            text = sanitize(f'[cmux]: @{msg_path}')
        else:
            text = sanitize(f'{label}{body}')

        _inject_text(target, text)
        time.sleep(0.2)  # let the paste render before the first Enter
        if not _submit(target, text):
            # Loud, greppable failure — the message text is preserved in the
            # input box (and, for long bodies, in the msg file), never lost.
            print(
                f'cmux: DELIVERY STUCK for {name!r} — injected text did not '
                f'submit after {_SUBMIT_RETRIES} Enter presses: {text[:80]!r}',
                file=sys.stderr, flush=True,
            )

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

    Four prompt types, four responses:
    - 'allow' in options (3-option): send '2' — "Yes, allow for session"
    - 'do you want to X' without 'allow' (2-option: 1.Yes/2.No): send '1' — approve once
    - Folder-trust dialog (2-option: 1.Yes trust/2.No exit): send '1' — trust it,
      then verify the dialog actually cleared before notifying. Escape would
      hit "Esc to cancel" and quit Claude, killing the window.
    - Tool-use prompts: send Escape to dismiss
    """
    notify_msg = (
        '[claudio@noreply]: A permission prompt was detected and automatically '
        'dismissed. Continuing with normal operation.'
    )

    def _send_and_notify(key):
        subprocess.run(['tmux', 'send-keys', '-t', target, key, 'Enter'], capture_output=True)
        time.sleep(1.0)
        _inject_text(target, notify_msg)
        _submit(target, notify_msg)

    def _trust_dialog_and_verify():
        """Accept the folder-trust dialog, then re-check the pane before
        notifying. Only one shot at this dialog is safe — sending an
        unexpected key into an unrecognized variant of it could pick "No,
        exit" instead of "Yes" — so confirm the dialog actually cleared
        before claiming success. If it's still showing (or the window is
        gone), stay quiet and let the next poll re-evaluate from scratch.
        """
        subprocess.run(['tmux', 'send-keys', '-t', target, '1', 'Enter'], capture_output=True)
        time.sleep(1.0)
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p', '-S', '-15'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return  # window is gone — nothing left to notify
        if any(pat in result.stdout.lower() for pat in _TRUST_DIALOG_PATTERNS):
            return  # dialog still showing — leave it for the next poll
        _inject_text(target, notify_msg)
        _submit(target, notify_msg)

    while True:
        time.sleep(interval)
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p', '-S', '-15'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue
        pane_text = result.stdout.lower()
        if any(pat in pane_text for pat in _ALLOW_SESSION_PATTERNS):
            _send_and_notify('2')  # "Yes, allow for session"
        elif any(pat in pane_text for pat in _APPROVE_ONCE_PATTERNS):
            _send_and_notify('1')  # "Yes" on a 2-option prompt
        elif any(pat in pane_text for pat in _TRUST_DIALOG_PATTERNS):
            _trust_dialog_and_verify()  # "Yes, I trust this folder" — verified
        elif any(pat in pane_text for pat in _PERM_PATTERNS):
            subprocess.run(['tmux', 'send-keys', '-t', target, 'Escape'], capture_output=True)
            time.sleep(1.0)
            _inject_text(target, notify_msg)
            _submit(target, notify_msg)


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
    if not no_inject:
        # Always run the unblock watcher — catches bypass permissions confirmation
        # and other permission prompts that appear during normal operation.
        # no_inject sessions don't use send-keys so the watcher would be a no-op.
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
