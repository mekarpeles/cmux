"""
cmux daemon — tmux backend for claudio.

Wraps a Claude Code session running in a tmux pane with a claudio inbox.
Idle detection reads the tmux pane for Claude's '❯' prompt.
Delivery injects messages via tmux send-keys.
"""

import os
import re
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


# Dim placeholder text Claude Code paints into an EMPTY input box. Matched so
# a placeholder is never mistaken for a draft. Anything else after ❯ is treated
# as typed text: a delayed message is recoverable (it stays queued in claudio
# until the box clears), a draft that got pasted over and submitted is not.
_GHOST_HINT_PREFIXES = (
    'try "',
    '/ for commands',
    '? for shortcuts',
)

# A highlighted menu option ('❯ 1. Yes'), not a typed draft — permission and
# folder-trust dialogs reuse the same ❯ glyph as the input prompt.
_MENU_OPTION_RE = re.compile(r'^\d+\.\s')

# Present in the status bar only while Claude is mid-turn. The input box stays
# drawn during generation, so a visible prompt alone is not idleness.
_GENERATING_MARKER = 'esc to interrupt'


def _last_prompt_line(content: str) -> 'str | None':
    """The last line starting with ❯ — Claude Code's live input box.

    Scrollback above it holds echoes of previously submitted messages that also
    start with ❯; only the last occurrence is the current input box.
    """
    last = None
    for line in content.split('\n'):
        stripped = line.lstrip()
        if stripped.startswith('❯'):
            last = stripped
    return last


def _prompt_body(prompt_line: str) -> str:
    """Text after the ❯ glyph, with nbsp padding normalized away."""
    return prompt_line[1:].replace('\xa0', ' ').strip()


def _input_is_empty(prompt_line: str) -> bool:
    """True when the input box holds nothing the user would mind losing."""
    body = _prompt_body(prompt_line)
    if not body:
        return True
    return any(body.lower().startswith(p) for p in _GHOST_HINT_PREFIXES)


def _has_user_draft(content: str) -> bool:
    """True when the input box holds text the user typed.

    Deliberately weaker than `not _input_is_empty(...)`: a dialog's highlighted
    option ('❯ 1. Yes') is not a draft, and the unblock watcher must still be
    able to answer it.
    """
    prompt_line = _last_prompt_line(content)
    if prompt_line is None:
        return False
    if _input_is_empty(prompt_line):
        return False
    return not _MENU_OPTION_RE.match(_prompt_body(prompt_line))


def _has_numbered_options(content: str) -> bool:
    """True when the pane shows a numbered option list ('1. Yes' / '2. No').

    Every real Claude Code permission prompt renders one. Requiring it keeps
    the digit responses from firing when Claude merely narrates a phrase like
    "do you want to proceed" in its prose.
    """
    for line in content.split('\n'):
        stripped = line.lstrip().lstrip('❯').strip()
        if _MENU_OPTION_RE.match(stripped):
            return True
    return False


def make_is_idle(target: str):
    """Return True when the Claude session is idle: prompt visible, nothing
    generating, and no draft in the input box.

    Four checks:
    1. ❯ prompt is visible at all
    2. Claude isn't mid-turn (_GENERATING_MARKER absent)
    3. The input box is empty, or holds only Claude's ghost hint
    4. cursor_x <= 2 — cursor at the prompt, not out in typed text

    Check 3 is deliberately NOT gated on cursor_x. It used to be (`cursor_x <
    2`), on the theory that any text visible at cursor_x == 2 was the ghost
    hint rather than user input. But an empty box renders as '❯\\xa0' with the
    cursor already at column 2, so the check never ran — and the cursor sits at
    column 2 with a real draft present whenever it is at the start of that
    draft (Home/Ctrl-A, cursor navigation, a TUI repaint). Measured against
    live panes, 13 of 19 running agents held one of Mek's drafts and still
    reported idle, so claudio delivered and _submit() pressed Enter over his
    half-written message. Pane content, not cursor position, is the signal that
    survives; cursor_x is kept only as an additional way to say "not idle".
    """
    def is_idle() -> bool:
        content = pane_content(target)

        prompt_line = _last_prompt_line(content)
        if prompt_line is None:
            return False

        if _GENERATING_MARKER in content.lower():
            return False

        if not _input_is_empty(prompt_line):
            return False

        result = subprocess.run(
            ['tmux', 'display-message', '-t', target, '-p', '#{cursor_x}'],
            capture_output=True, text=True,
        )
        try:
            cursor_x = int(result.stdout.strip())
        except ValueError:
            cursor_x = 0
        return cursor_x <= 2

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


# Markers identifying Claude Code's status bar, which sits just below the input
# box and changes on its own (spinner, token counter, 'esc to interrupt').
# _input_box_text stops before it so that churn is never read as user typing.
_STATUS_BAR_MARKERS = ('⏵⏵', 'esc to interrupt', 'for agents', 'for shortcuts')

# Box-drawing characters Claude Code frames the input box with.
_BOX_BORDER_CHARS = set('─│╭╮╰╯')


def _input_box_text(content: str) -> str:
    """The text currently sitting in the input box, whitespace-collapsed.

    Starts at the last ❯ line and includes wrapped continuation rows, stopping
    at the box's closing border, a blank line, or the status bar. Excluding the
    status bar matters: it updates on its own while Claude works, and comparing
    against it would look exactly like the user typing.
    """
    lines = content.split('\n')
    prompt_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith('❯'):
            prompt_idx = i
    if prompt_idx is None:
        return ''

    parts = [_prompt_body(lines[prompt_idx].lstrip())]
    for line in lines[prompt_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            break
        if set(stripped) <= _BOX_BORDER_CHARS:
            break
        if any(m in stripped for m in _STATUS_BAR_MARKERS):
            break
        parts.append(stripped)
    return ' '.join(re.sub(r'\s+', ' ', p) for p in parts if p).strip()


def _submitted_in(content: str, text: str) -> bool:
    """True once the injected text is no longer sitting in the input line.

    Checks the pane from the LAST ❯ prompt downward (the input box, including
    wrapped continuation rows) for a probe prefix of the injected text. The
    conversation echo of a successfully submitted message sits ABOVE the
    prompt, so it never false-positives; the empty-input ghost hint doesn't
    contain the probe either. If no prompt is visible at all (redraw, screen
    switch), report submitted rather than retry-looping blind.
    """
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


def _submitted(target: str, text: str) -> bool:
    """Capture the pane and report whether `text` has left the input line."""
    return _submitted_in(pane_content(target), text)


def _input_grew_beyond(content: str, text: str) -> bool:
    """True when the input box holds MORE than the text we injected.

    That extra content is someone typing into the box while delivery is still
    retrying. Compared against the injected text rather than a captured
    baseline so the retry loop still needs only one pane capture per attempt.
    """
    box = _input_box_text(content)
    if not box:
        return False
    if text[:24] not in box:
        return False  # our text isn't in the box — submitted, or a redraw
    return len(box) > len(re.sub(r'\s+', ' ', text).strip())


def _submit(target: str, text: str) -> bool:
    """Press Enter until the injected text actually leaves the input line.

    A single Enter can be swallowed (paste-capture window, autocomplete
    popup, mid-redraw). Verify-and-retry converts "usually submits" into
    "submits or tells you it couldn't": each attempt presses Enter, waits
    (growing backoff — also lets a popup/paste window close), and re-checks
    the pane. Returns True on verified submission.

    Bails out early if the input box grows beyond what was injected: that is
    someone typing, and another Enter would submit their half-written text
    merged with this message. Giving up is cheap — the stranded message is
    picked up by _stuck_reconciler once the box settles — so a false bail costs
    a short delay while a missed one costs Mek his sentence.
    """
    for attempt in range(_SUBMIT_RETRIES):
        subprocess.run(['tmux', 'send-keys', '-t', target, 'Enter'])
        time.sleep(0.4 + 0.3 * attempt)
        content = pane_content(target)
        if _submitted_in(content, text):
            return True
        if _input_grew_beyond(content, text):
            print(
                f'cmux: delivery paused — input box changed while submitting, '
                f'someone is typing. Not pressing Enter again: {text[:80]!r}',
                file=sys.stderr, flush=True,
            )
            return False
    return False


def make_deliver(name: str, target: str, in_flight: 'threading.Event | None' = None):
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

        # in_flight fences the stuck-delivery reconciler out of this window, so
        # its Enter can never land on top of _submit()'s own Enter presses.
        if in_flight is not None:
            in_flight.set()
        try:
            _inject_text(target, text)
            time.sleep(0.2)  # let the paste render before the first Enter
            submitted = _submit(target, text)
        finally:
            if in_flight is not None:
                in_flight.clear()
        if not submitted:
            # Loud, greppable failure — the message text is preserved in the
            # input box (and, for long bodies, in the msg file), never lost.
            # _stuck_reconciler picks it up from here.
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


# Labels make_deliver() prefixes onto every injected message: '[cmux]: ' for
# cmux itself, '[<sender>@cmux]: ' for a peer agent. Used to recognize cmux's
# own text sitting in the input box — the one case where pressing Enter is
# recovering a delivery rather than submitting something a human wrote.
_CMUX_LABEL_RE = re.compile(r'^\[(cmux|[^\]]+@cmux)\]:')

# Consecutive identical polls required before the reconciler treats the input
# box as settled. Anything still changing is being actively written to.
_RECONCILE_STABLE_POLLS = 3

# Enter presses per distinct stuck message before giving up. A successful Enter
# clears the box, so this only bites on a genuinely wedged pane — where
# pressing Enter every poll forever would be noise, not recovery.
_RECONCILE_MAX_ATTEMPTS = 2


def _stuck_cmux_message(content: str) -> 'str | None':
    """The cmux-injected message stranded in the input box, if any.

    Returns None for an empty box, a ghost hint, a dialog option, or anything a
    human typed — only cmux's own label-prefixed text qualifies.
    """
    prompt_line = _last_prompt_line(content)
    if prompt_line is None:
        return None
    if _input_is_empty(prompt_line):
        return None
    body = _prompt_body(prompt_line)
    return body if _CMUX_LABEL_RE.match(body) else None


def _stuck_reconciler(name: str, target: str, interval: float = 5.0,
                      stop_event: 'threading.Event | None' = None,
                      in_flight: 'threading.Event | None' = None) -> None:
    """Background thread: submit a cmux message left sitting in the input box.

    _submit() presses Enter _SUBMIT_RETRIES times and then gives up, logging
    DELIVERY STUCK and leaving the text in the box (seen in production on
    'ada'). Before the draft guard existed, the next delivery pasted on top and
    its Enter cleared the box — corrupting the two messages together, but
    self-healing. The draft guard removes that path, so a stuck message would
    otherwise sit there forever, and because is_idle() now reports a non-empty
    box as busy, it would silently block every later message for that agent.

    Pressing Enter from a background thread is exactly the pattern that caused
    the cut-off-while-typing bug, so it is fenced in three ways:
    - the box must hold cmux's own label-prefixed text and nothing else, so a
      human draft can never qualify (_stuck_cmux_message)
    - that text must be byte-identical across _RECONCILE_STABLE_POLLS polls, so
      anything still being written to is left alone
    - no delivery may be in flight, so it cannot double-submit by racing
      _submit()'s own Enter presses
    """
    last_seen = None
    stable = 0
    attempts = {}

    while stop_event is None or not stop_event.is_set():
        time.sleep(interval)
        if in_flight is not None and in_flight.is_set():
            last_seen, stable = None, 0  # delivery running — restart the clock
            continue
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue

        stuck = _stuck_cmux_message(result.stdout)
        if stuck is None:
            last_seen, stable = None, 0
            continue

        if stuck == last_seen:
            stable += 1
        else:
            last_seen, stable = stuck, 1
        if stable < _RECONCILE_STABLE_POLLS:
            continue

        if attempts.get(stuck, 0) >= _RECONCILE_MAX_ATTEMPTS:
            continue
        attempts[stuck] = attempts.get(stuck, 0) + 1
        print(
            f'cmux: recovering stuck delivery for {name!r} — pressing Enter on '
            f'unsubmitted message: {stuck[:80]!r}',
            file=sys.stderr, flush=True,
        )
        subprocess.run(['tmux', 'send-keys', '-t', target, 'Enter'], capture_output=True)


def _unblock_watcher(name: str, target: str, interval: float = 1.5,
                      stop_event: 'threading.Event | None' = None) -> None:
    """Background thread: poll for permission prompts and auto-dismiss.

    Four prompt types, four responses:
    - 'allow' in options (3-option): send '2' — "Yes, allow for session"
    - 'do you want to X' without 'allow' (2-option: 1.Yes/2.No): send '1' — approve once
    - Folder-trust dialog (2-option: 1.Yes trust/2.No exit): send '1' — trust it,
      then verify the dialog actually cleared before notifying. Escape would
      hit "Esc to cancel" and quit Claude, killing the window.
    - Tool-use prompts: send Escape to dismiss

    stop_event is None for the real daemon (runs for the process lifetime, same
    as always). Tests pass a real Event so the thread they spawn actually exits
    at teardown instead of leaking as a zombie poller for the rest of the suite.
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

    while stop_event is None or not stop_event.is_set():
        time.sleep(interval)
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p', '-S', '-15'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue

        # Never send keys while there is a draft in the input box. Every
        # dismiss path below is followed by Enter, which would submit whatever
        # was being typed — the "I got cut off mid-sentence" report. A real
        # dialog replaces the input box, so a draft being present means there
        # is no dialog here to answer and nothing is lost by waiting.
        if _has_user_draft(result.stdout):
            continue

        pane_text = result.stdout.lower()
        # The digit responses below pick an option by number, so require that
        # numbered options are actually on screen. Without this, Claude merely
        # writing "do you want to proceed" in its prose sends '1' + Enter.
        has_options = _has_numbered_options(result.stdout)
        if has_options and any(pat in pane_text for pat in _ALLOW_SESSION_PATTERNS):
            _send_and_notify('2')  # "Yes, allow for session"
        elif has_options and any(pat in pane_text for pat in _APPROVE_ONCE_PATTERNS):
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
    in_flight = threading.Event()
    if no_inject:
        deliver = make_deliver_file(name)
        is_idle = lambda: True  # always ready to queue; delivery is non-blocking
    else:
        deliver = make_deliver(name, tmux_target, in_flight=in_flight)
        is_idle = make_is_idle(tmux_target)
    if not no_inject:
        # Recovers a message that _submit() left unsubmitted in the input box.
        # Without it, is_idle() correctly reports the non-empty box as busy and
        # every later message for this agent queues behind it forever.
        threading.Thread(
            target=_stuck_reconciler, args=(name, tmux_target),
            kwargs={'in_flight': in_flight}, daemon=True,
        ).start()
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
