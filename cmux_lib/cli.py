"""cmux — Claude multiplexer. Manage persistent Claude sessions."""

import json
import os
import signal
import socket
import subprocess
import sys
import time

from cmux_lib import db
from cmux_lib.daemon import _PERM_PATTERNS, _TRUST_DIALOG_PATTERNS, _stuck_cmux_message

STATE_DIR = os.environ.get('CMUX_STATE_DIR', os.path.expanduser('~/.cmux'))
REGISTRY = os.path.join(STATE_DIR, 'sessions.json')
MAX_MESSAGE_LEN = 2000

# Known agent metadata for `cmux agent import-sessions`.
# workspace=None means standalone (own tmux session), not ol-loop.
KNOWN_AGENTS = {
    'lupin':  {'role': 'The Looper — coordinator', 'workspace': None,      'workflow': '~/Projects/pm/workflows/the-loop.md'},
    'odie':   {'role': 'OPDS Performance Specialist', 'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/opds-system.md'},
    'slater': {'role': 'The Translator — i18n',     'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/i18n-translation-update.md'},
    'pierre': {'role': 'The PR Tidier',              'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/pr-review.md'},
    'richy':  {'role': 'The Issue & PR Enricher',   'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/issue-refinement.md'},
    'fonzie': {'role': 'The Responder — Needs:Response', 'workspace': 'ol-loop', 'workflow': None},
    'locke':  {'role': 'The Security Auditor',      'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/security.md'},
    'reno':   {'role': 'The Renovate Bundler',      'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/renovate-consolidation.md'},
    'impa':   {'role': 'The Import Pipeline Specialist', 'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/import_workflow.md'},
    'saul':   {'role': 'Solr Specialist',            'workspace': None,      'workflow': None},
    'ester':  {'role': 'The Auth Specialist — S3/xauthn', 'workspace': None, 'workflow': None},
    'fran':   {'role': 'The Frontend Tester — Playwright', 'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/frontend-testing.md'},
    'revere': {'role': 'The Code Reviewer',          'workspace': 'ol-loop', 'workflow': None},
    'valerie':{'role': 'The Volunteer Coordinator',  'workspace': 'ol-loop', 'workflow': '~/Projects/pm/workflows/mek-executive-assistant.md'},
}

# ------------------------------------------------------------------
# Registry helpers
# ------------------------------------------------------------------

def load_registry():
    if not os.path.exists(REGISTRY):
        return {}
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # The registry exists but is unreadable/corrupt. Do NOT silently treat
        # it as empty — every command that reads this then writes it back
        # would permanently erase every agent's session bookkeeping in one
        # shot (this is exactly how sessions.json got wiped to `{}` during the
        # 2026-07-23 incident). Back up the corrupt file so it can be
        # inspected/recovered, and refuse to proceed rather than risk that.
        import shutil as _shutil
        backup = f'{REGISTRY}.corrupt-{int(time.time())}'
        try:
            _shutil.copy2(REGISTRY, backup)
        except OSError:
            backup = None
        print(
            f"cmux: sessions.json is corrupt ({e}) — refusing to treat it as empty.\n"
            + (f"  Backed up to {backup}.\n" if backup else "")
            + "  Fix or remove it manually before continuing.",
            file=sys.stderr,
        )
        sys.exit(1)


def _parse_flags(args, kv=(), bools=()):
    """Parse --flag value pairs and boolean flags from an arg list.

    The special key 'note' captures all remaining args after --note.
    A bare '--' sets '__prompt__' to the joined remaining args.
    Returns (positional_list, flags_dict).
    """
    out = {k: None for k in kv}
    out.update({b: False for b in bools})
    pos = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == '--':
            out['__prompt__'] = ' '.join(args[i + 1:]) or None
            break
        key = tok[2:] if tok.startswith('--') else None
        if key in bools:
            out[key] = True; i += 1
        elif key in out:
            if key == 'note':
                out['note'] = ' '.join(args[i + 1:]); break
            if i + 1 < len(args):
                out[key] = args[i + 1]; i += 2
            else:
                i += 1
        elif not tok.startswith('-'):
            pos.append(tok); i += 1
        else:
            i += 1
    return pos, out


def save_registry(reg):
    os.makedirs(STATE_DIR, exist_ok=True)
    # Write atomically: a crash or a concurrent read mid-write must never leave
    # sessions.json truncated/partial, which load_registry() would otherwise
    # have to treat as corrupt (see above) and refuse to proceed past.
    tmp = f'{REGISTRY}.tmp-{os.getpid()}'
    with open(tmp, 'w') as f:
        json.dump(reg, f, indent=2)
    os.replace(tmp, REGISTRY)


def _tmux_session(name, workspace=None):
    """Tmux session name: workspace name if given, else cmux-{name}."""
    return workspace if workspace else f'cmux-{name}'


def _tmux_target(name, workspace=None):
    """Tmux target for send-keys / capture-pane: session:window."""
    return f'{_tmux_session(name, workspace)}:{name}'


def _cleanup_files(name):
    home = os.path.join(STATE_DIR, name)
    # socket lives inside the agent homedir (claudio state_dir=home)
    try:
        os.unlink(os.path.join(home, f'{name}.sock'))
    except FileNotFoundError:
        pass
    for suffix in ('.daemon.pid', '.daemon.log'):
        try:
            os.unlink(os.path.join(STATE_DIR, f'{name}{suffix}'))
        except FileNotFoundError:
            pass


def _window_exists(tmux_sess, window_name):
    r = subprocess.run(
        ['tmux', 'list-windows', '-t', tmux_sess, '-F', '#{window_name}'],
        capture_output=True, text=True
    )
    return window_name in r.stdout.split()


def session_alive(info):
    tmux_sess = info.get('tmux_session', f'cmux-{info["name"]}')
    window = info.get('tmux_window', info['name'])
    r = subprocess.run(['tmux', 'has-session', '-t', tmux_sess], capture_output=True)
    if r.returncode != 0:
        return False
    return _window_exists(tmux_sess, window)


def _ensure_alive(name):
    """Return the current registry, transparently recreating name's tmux
    session/window first if it's known but gone (e.g. a reboot wiped the tmux
    server while sessions.json/agents.db survived on disk).

    Recreation goes through cmd_start, which reads workspace/etc. from
    agents.db — so a workspaced agent rejoins its workspace session and a
    standalone agent gets its own session back, whichever applies.
    """
    reg = load_registry()
    if name in reg and session_alive(reg[name]):
        return reg
    if name not in reg and not db.get_agent(name):
        return reg
    print(f"cmux: tmux session for '{name}' not found — recreating...")
    cmd_start(name, detach=True)
    return load_registry()


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------

def _wait_for_socket(sock_path, timeout=10):
    """Block until the daemon's socket is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            s.close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f'cmux: daemon socket never became ready: {sock_path}')


def cmd_inbox(name):
    """Print and clear all queued messages for a --no-inject agent."""
    inbox_path = os.path.join(STATE_DIR, f'{name}.inbox.jsonl')
    try:
        lines = open(inbox_path).readlines()
    except FileNotFoundError:
        print(f'cmux: no inbox for {name!r} (not a --no-inject agent, or no messages yet)')
        return
    if not lines:
        print(f'cmux: inbox for {name!r} is empty')
        return
    for line in lines:
        try:
            msg = json.loads(line)
            sender = msg.get('from', '?')
            print(f'[{sender}@cmux]: {msg["body"]}')
        except Exception:
            print(line, end='')
    open(inbox_path, 'w').close()


# Overridable by tests via env vars or direct module assignment.
# Set CMUX_SESSION_DETECT_RETRIES=1 CMUX_SESSION_DETECT_INTERVAL=0 to skip
# waits in fake-claude environments (subprocess-spawned cmux reads these too).
_SESSION_DETECT_RETRIES = int(os.environ.get('CMUX_SESSION_DETECT_RETRIES', '4'))
_SESSION_DETECT_INTERVAL = int(os.environ.get('CMUX_SESSION_DETECT_INTERVAL', '3'))


def _cwd_to_claude_project_dir(cwd):
    """Map a filesystem path to the ~/.claude/projects subdirectory Claude uses for it."""
    # Claude replaces every / in the absolute path with - to form the subdirectory name.
    # e.g. /Users/mek/.cmux/alice -> ~/.claude/projects/-Users-mek-.cmux-alice
    slug = cwd.replace('/', '-')
    return os.path.join(os.path.expanduser('~/.claude/projects'), slug)


def _snapshot_claude_sessions(project_dir=None):
    """Return {filepath: mtime} for Claude session files.

    project_dir: if given, scan only that subdirectory (scoped to one agent's CWD).
    Otherwise scans all of ~/.claude/projects/ — use only when CWD is unknown.
    """
    projects_root = os.path.expanduser('~/.claude/projects')
    snapshot = {}
    if project_dir:
        dirs = [project_dir]
    else:
        if not os.path.isdir(projects_root):
            return snapshot
        dirs = [
            os.path.join(projects_root, d)
            for d in os.listdir(projects_root)
        ]
    for proj_path in dirs:
        if os.path.isdir(proj_path):
            for f in os.listdir(proj_path):
                if f.endswith('.jsonl'):
                    fp = os.path.join(proj_path, f)
                    try:
                        snapshot[fp] = os.path.getmtime(fp)
                    except OSError:
                        pass
    return snapshot


def _claude_session_exists(session_id):
    """Return True if the Claude JSONL for session_id exists anywhere under ~/.claude/projects/."""
    projects_root = os.path.expanduser('~/.claude/projects')
    if not os.path.isdir(projects_root):
        return False
    for d in os.listdir(projects_root):
        if os.path.exists(os.path.join(projects_root, d, f'{session_id}.jsonl')):
            return True
    return False


def _store_session_id(home, pre_snapshot, project_dir=None, retries=None, retry_interval=None):
    """Detect and store the Claude session UUID for this agent.

    Claude creates the JSONL lazily — often not until after the first message
    exchange. Retries with delay so we catch it after message injection.
    project_dir scopes the search to the agent's CWD to avoid picking up
    other active sessions as false positives.

    retries/retry_interval default to the module-level constants, which tests
    can override to 1/0 to skip waits in fake-claude environments.
    """
    if retries is None:
        retries = _SESSION_DETECT_RETRIES
    if retry_interval is None:
        retry_interval = _SESSION_DETECT_INTERVAL
    for attempt in range(retries):
        post = _snapshot_claude_sessions(project_dir=project_dir)
        candidates = [
            fp for fp, mtime in post.items()
            if fp not in pre_snapshot or mtime > pre_snapshot[fp]
        ]
        if candidates:
            newest = max(candidates, key=lambda fp: post[fp])
            session_uuid = os.path.splitext(os.path.basename(newest))[0]
            try:
                with open(os.path.join(home, 'last-session-id'), 'w') as f:
                    f.write(session_uuid)
            except OSError:
                pass
            return
        if attempt < retries - 1:
            time.sleep(retry_interval)


_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _inject_startup_context(name, home, is_first_start):
    """Inject exactly one startup message.

    Clone start (clone-source file present):
        name + home + source info + inline initial-prompt (if any) + @clone_readme.md
    First start (is_first_start True — no session was known for this agent
    before this cmd_start call):
        name + home + inline initial-prompt (if any) + @ONBOARDING + @IDENTITY_GUIDE
    Subsequent starts (resume):
        resume line + inline initial-prompt (if any)

    is_first_start — not identity.md's presence — is the gate: several real
    agents never get an identity.md seeded (no --identity was ever passed) but
    are well past their first start, and would otherwise get the full
    onboarding blast on every single resume.

    initial-prompt is inlined (not @file ref) so it's guaranteed delivered in the
    same injection — no file-reference parsing required for the user's prompt.
    initial-prompt.md is written by cmd_start before this call.
    clone-source is written by cmd_clone before calling cmd_start.
    """
    identity_path = os.path.join(home, 'identity.md')
    prompt_path = os.path.join(home, 'initial-prompt.md')
    clone_marker = os.path.join(home, 'clone-source')

    prompt_text = None
    if os.path.exists(prompt_path):
        try:
            prompt_text = open(prompt_path).read().strip() or None
        except OSError:
            pass

    if os.path.exists(clone_marker):
        source_name = open(clone_marker).read().strip()
        source_home = os.path.join(os.path.dirname(home), source_name)
        parts = [
            f'Session: "{name}" — home: {home}.',
            f'You are a clone of "{source_name}" (source home: {source_home}).',
        ]
        if prompt_text:
            parts.append(prompt_text)
        parts.append(f'@{os.path.join(_PKG_DIR, "clone_readme.md")}')
        cmd_send(name, ' '.join(parts), sender='cmux', quiet=True)
        os.unlink(clone_marker)  # delivered — subsequent wakeups are normal
        return

    has_identity = os.path.exists(identity_path)
    parts = []
    if is_first_start:
        parts.append(f'Session: "{name}" — home: {home}.')
    elif has_identity:
        parts.append(f'Resuming "{name}" (ref: {identity_path}).')
    else:
        parts.append(f'Resuming "{name}".')

    if prompt_text:
        parts.append(prompt_text)

    if is_first_start:
        parts.append(f'@{os.path.join(_PKG_DIR, "ONBOARDING.md")}')
        parts.append(f'@{os.path.join(_PKG_DIR, "IDENTITY_GUIDE.md")}')

    cmd_send(name, ' '.join(parts), sender='cmux', quiet=True)


def cmd_start(name, initial_prompt=None, detach=False, workspace=None, no_inject=False,
              unblock=False, allowed_tools=None, identity_path=None, from_session=None,
              force=False):
    """Start a new cmux session (window). Uses DB registration if available.

    from_session, when given, promotes an existing (externally-created) Claude
    session into this agent by resuming that session id instead of starting
    fresh — the rest of the startup procedure (tmux window, daemon, injected
    onboarding message) runs exactly as it does for a normal first start.

    force, when True, allows from_session to overwrite an agent's existing,
    still-valid tracked session with a different one. Without it, promoting
    into an agent that already has different valid history is refused — see
    the from_session block below for why.
    """
    # Fall back to DB registration for any unspecified args
    reg_info = db.get_agent(name)
    if reg_info:
        if workspace is None and reg_info.get('workspace'):
            workspace = reg_info['workspace']
        if initial_prompt is None and reg_info.get('initial_prompt'):
            initial_prompt = reg_info['initial_prompt']
        if not no_inject and reg_info.get('no_inject'):
            no_inject = bool(reg_info['no_inject'])
        if not unblock and reg_info.get('unblock'):
            unblock = bool(reg_info['unblock'])
        if allowed_tools is None and reg_info.get('allowed_tools'):
            allowed_tools = reg_info['allowed_tools']
        if identity_path is None and reg_info.get('identity_path'):
            identity_path = reg_info['identity_path']

    reg = load_registry()

    if name in reg and session_alive(reg[name]):
        print(f"cmux: agent '{name}' already running — attaching")
        if not detach:
            cmd_attach(name)
        return

    tmux_sess = _tmux_session(name, workspace)
    target = _tmux_target(name, workspace)

    os.makedirs(STATE_DIR, exist_ok=True)
    home = os.path.join(STATE_DIR, name)

    # Dangling session: home dir exists with a stale last-session-id but the agent
    # was removed from the DB (rm'd without archiving under old behaviour).
    # Only triggers when last-session-id is present — the real risk is silently
    # resuming a session the user thought they'd deleted.
    _session_id_path = os.path.join(home, 'last-session-id')
    if os.path.isdir(home) and reg_info is None and os.path.exists(_session_id_path):
        print(f"cmux: dangling agent '{name}' — removed from registry but home dir was not archived.")
        if sys.stdin.isatty():
            try:
                choice = input("  [A]rchive, [d]elete, [c]reate fresh: ").strip().lower() or 'a'
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(1)
        else:
            choice = 'a'  # non-interactive: archive by default
            print("  non-interactive — archiving by default")
        if choice == 'd':
            import shutil as _shutil
            _shutil.rmtree(home)
        elif choice == 'c':
            try:
                os.unlink(_session_id_path)
            except FileNotFoundError:
                pass
        else:  # 'a' or anything else
            import shutil as _shutil
            archive_dir = os.path.join(STATE_DIR, 'archive')
            os.makedirs(archive_dir, exist_ok=True)
            dest = os.path.join(archive_dir, f'{int(time.time())}_{name}')
            _shutil.move(home, dest)
            print(f"  archived → {dest}")

    os.makedirs(home, exist_ok=True)

    # Seed identity.md from --identity path on first start (never overwrites).
    if identity_path:
        identity_dst = os.path.join(home, 'identity.md')
        if not os.path.exists(identity_dst):
            import shutil as _shutil
            identity_path = os.path.expanduser(identity_path)
            if not os.path.exists(identity_path):
                print(f'cmux: identity file not found: {identity_path}', file=sys.stderr)
                sys.exit(1)
            _shutil.copy2(identity_path, identity_dst)
        # If identity.md already exists, the provided path is silently ignored —
        # the agent owns their identity after the first start.

    # Write initial_prompt to a file so it can be @-referenced regardless of length.
    if initial_prompt:
        with open(os.path.join(home, 'initial-prompt.md'), 'w') as _f:
            _f.write(initial_prompt)

    # Drop MIGRATE.md into home dir if not already present.
    _migrate_src = os.path.join(os.path.dirname(__file__), '..', 'MIGRATE.md')
    _migrate_dst = os.path.join(home, 'MIGRATE.md')
    if not os.path.exists(_migrate_dst) and os.path.exists(_migrate_src):
        import shutil as _shutil
        _shutil.copy2(_migrate_src, _migrate_dst)

    # Persist and reuse the agent's real working directory. Without this, every
    # start/resume launches wherever `cmux up` happens to be invoked from — if
    # that differs from where the agent's session actually lives, Claude can't
    # find the session there and silently starts an unrelated fresh conversation
    # in the wrong directory instead of resuming the real one. First start
    # records the invoker's cwd as the agent's home; every later start reuses it
    # regardless of where `cmux up` is run from.
    cwd_path = os.path.join(home, 'cwd')
    if os.path.exists(cwd_path):
        agent_cwd = open(cwd_path).read().strip() or os.getcwd()
        if not os.path.isdir(agent_cwd):
            print(
                f"cmux: warning — '{name}'s recorded directory no longer exists: {agent_cwd}\n"
                f"  falling back to the current directory: {os.getcwd()}",
                file=sys.stderr,
            )
            agent_cwd = os.getcwd()
    else:
        agent_cwd = os.getcwd()
        try:
            with open(cwd_path, 'w') as f:
                f.write(agent_cwd)
        except OSError:
            pass

    claude_bin = os.environ.get('CMUX_CLAUDE_CMD', 'claude')
    # Use --resume <id> if we tracked the agent's last session, plain claude
    # on first start (no flags = new session, which we then track for next time).
    session_id_path = os.path.join(home, 'last-session-id')
    stored_id = None
    if os.path.exists(session_id_path):
        stored_id = open(session_id_path).read().strip() or None
    # Validate: if the JSONL no longer exists on disk, the session was pruned.
    # Resuming a missing session causes claude to exit immediately, closing the
    # tmux window before we can attach.  Clear it and start fresh instead.
    if stored_id and not _claude_session_exists(stored_id):
        stored_id = None
        try:
            os.unlink(session_id_path)
        except FileNotFoundError:
            pass

    # -f/--from promotes an existing Claude session (e.g. one the user started
    # bare in a terminal) into this agent — an explicit override wins over
    # whatever, if anything, was already tracked for this name.
    if from_session:
        if not _claude_session_exists(from_session):
            print(
                f"cmux: no Claude session found with id '{from_session}' — check the id "
                f"and that you're in the directory it was started from",
                file=sys.stderr,
            )
            sys.exit(1)
        # Refuse to silently clobber a different, still-valid tracked session.
        # This exact mistake — testing -f against a real agent name and
        # overwriting its actual history with an unrelated session — is what
        # happened to the "ada" agent during the 2026-07-23 incident.
        if stored_id and stored_id != from_session and not force:
            print(
                f"cmux: '{name}' already has a valid session tracked ({stored_id}) — "
                f"refusing to overwrite it with '{from_session}' without --force.\n"
                f"  If you really mean to replace it, re-run with --force.",
                file=sys.stderr,
            )
            sys.exit(1)
        stored_id = from_session
        try:
            with open(session_id_path, 'w') as f:
                f.write(stored_id)
        except OSError:
            pass

    # True first start: no session was known for this agent before this call —
    # not from the tracked file, not from -f. Only in this case do we need to
    # auto-detect the session id Claude creates, by diffing the project
    # directory before/after spawn (see _store_session_id). A resume already
    # knows its session id, and diffing anyway is exactly how a resume that
    # launches into a busy shared directory can clobber last-session-id with
    # an unrelated session that merely has a newer mtime — this is precisely
    # what happened to several agents during the 2026-07-23 incident.
    _is_first_start = stored_id is None

    env_prefix = f'CMUX_SESSION_NAME={name} CLAUDIO_STATE_DIR={home}'
    allowed_tools_flag = f' --allowedTools {allowed_tools}' if allowed_tools else ''
    if stored_id:
        claude_cmd = f'{env_prefix} {claude_bin} --resume {stored_id}{allowed_tools_flag}'
    else:
        claude_cmd = f'{env_prefix} {claude_bin}{allowed_tools_flag}'

    # Scope session detection to the agent's OWN directory (not necessarily the
    # invoker's cwd) — avoids picking up other active Claude sessions in a
    # different, shared directory as false positives. Only computed on a true
    # first start — see _is_first_start above for why a resume skips this.
    _project_dir = _cwd_to_claude_project_dir(agent_cwd)
    _pre_sessions = _snapshot_claude_sessions(project_dir=_project_dir) if _is_first_start else {}

    sess_exists = subprocess.run(
        ['tmux', 'has-session', '-t', tmux_sess], capture_output=True
    ).returncode == 0

    # -c pins the window to the agent's own recorded directory. Without it,
    # tmux windows inherit the SESSION's start directory (or wherever `cmux up`
    # happened to be invoked from) — so an agent could start somewhere that
    # doesn't match where its session actually lives. agent_cwd also determines
    # which Claude Code project the agent belongs to (session detection +
    # --resume are per-directory).
    if not sess_exists:
        subprocess.run(
            ['tmux', 'new-session', '-d', '-s', tmux_sess, '-n', name,
             '-c', agent_cwd, claude_cmd],
            check=True
        )
    else:
        subprocess.run(
            # Trailing colon forces session resolution: a bare name can match a
            # WINDOW of the same name (e.g. the coordinator window named after
            # its workspace), making tmux try to create at that occupied index
            # ("create window failed: index 0 in use").
            ['tmux', 'new-window', '-t', f'{tmux_sess}:', '-n', name,
             '-c', agent_cwd, claude_cmd],
            check=True
        )

    # Give claude a moment to start, then verify the window is still alive.
    # If it exited immediately (e.g. bad flag, missing binary), the session may
    # already be gone — surface a useful error before we try to attach.
    time.sleep(0.5)
    if not _window_exists(tmux_sess, name):
        # Capture recent pane output to explain why claude exited.
        r = subprocess.run(
            ['tmux', 'capture-pane', '-t', f'{tmux_sess}:{name}', '-p'],
            capture_output=True, text=True,
        )
        output = r.stdout.strip()
        print(f"cmux: claude exited immediately in '{tmux_sess}:{name}'", file=sys.stderr)
        if output:
            print(f"  last output:\n{output}", file=sys.stderr)
        else:
            print(f"  (no output captured — window already closed)", file=sys.stderr)
        print(f"  cmd: {claude_cmd}", file=sys.stderr)
        sys.exit(1)

    daemon_log = os.path.join(STATE_DIR, f'{name}.daemon.log')
    daemon_pid_file = os.path.join(STATE_DIR, f'{name}.daemon.pid')
    # Stop any daemon still running for this agent before starting a new one.
    # Previously this just unlinked the pid file unconditionally — which erases
    # the bookkeeping but never signals the OLD daemon process. It kept running
    # orphaned forever, uncoordinated with whichever daemon started after it.
    # Orphaned daemons accumulate indefinitely this way (7+ per agent observed
    # for a single agent in the 2026-07-23 incident, one dating back days) and
    # race each other injecting into the same tmux pane — the direct cause of
    # duplicate permission-dismiss notices and similar corruption. Mirrors the
    # same kill-by-pid cmd_stop already does correctly.
    try:
        old_daemon_pid = int(open(daemon_pid_file).read().strip())
    except (FileNotFoundError, ValueError):
        old_daemon_pid = None
    if old_daemon_pid is not None:
        try:
            os.kill(old_daemon_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already dead — nothing to clean up
        else:
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    os.kill(old_daemon_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                print(
                    f"cmux: warning — previous daemon for '{name}' (pid {old_daemon_pid}) "
                    f"did not exit after SIGTERM within 3s; it may still be running",
                    file=sys.stderr,
                )
    try:
        os.unlink(daemon_pid_file)
    except FileNotFoundError:
        pass
    daemon_args = [sys.executable, '-m', 'cmux_lib.daemon', name, target]
    if no_inject:
        daemon_args.append('--no-inject')
    if unblock:
        daemon_args.append('--unblock')
    # The daemon must not inherit the caller's cwd as an import root. `python -m`
    # puts the cwd on sys.path, so any directory there whose name collides with a
    # daemon import shadows the real module — e.g. running `cmux up` from a dir
    # containing a bare `claudio/` folder makes `import claudio` resolve to that
    # empty namespace package, and the daemon dies with
    # `AttributeError: module 'claudio' has no attribute 'run'`.
    #   PYTHONSAFEPATH drops the cwd entry outright, but only on Python 3.11+.
    #   cwd=home covers 3.9/3.10, where it's silently ignored: the agent's state
    #   dir holds no importable packages. The daemon uses no relative paths.
    daemon_env = dict(os.environ, PYTHONSAFEPATH='1')
    daemon_proc = subprocess.Popen(
        daemon_args,
        stdout=open(daemon_log, 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=home,
        env=daemon_env,
    )

    reg[name] = {
        'name': name,
        'workspace': workspace,
        'tmux_session': tmux_sess,
        'tmux_window': name,
        'tmux_target': target,
        'socket': os.path.join(home, f'{name}.sock'),
        'daemon_pid': daemon_proc.pid,
        'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'initial_prompt': initial_prompt,
        'no_inject': no_inject,
        'unblock': unblock,
    }
    save_registry(reg)

    # Every cmux up upserts to DB — all agents are persistent by default.
    db.register_agent(
        name,
        role=reg_info.get('role') if reg_info else None,
        workspace=workspace,
        workflow_path=reg_info.get('workflow_path') if reg_info else None,
        initial_prompt=initial_prompt,
        no_inject=no_inject,
        unblock=unblock,
        allowed_tools=allowed_tools,
        identity_path=identity_path,
    )

    _wait_for_socket(reg[name]['socket'])

    _inject_startup_context(name, home, _is_first_start)

    # Detect and store Claude session ID after message injection — JSONL is created
    # lazily (often after the first exchange), so checking immediately after socket
    # ready misses it. Retries with delay until the file appears. Only needed on
    # a true first start — see _is_first_start above.
    if _is_first_start:
        _store_session_id(home, _pre_sessions, project_dir=_project_dir)

    # Inject workflow file if registered and readable.
    workflow_path = (reg_info.get('workflow_path') if reg_info else None)
    if workflow_path:
        workflow_path = os.path.expanduser(workflow_path)
        try:
            wf_content = open(workflow_path).read().strip()
            if wf_content:
                wf_msg = f'[cmux]: Your workflow from {workflow_path}:\n\n{wf_content}'
                if len(wf_msg) <= MAX_MESSAGE_LEN:
                    cmd_send(name, wf_msg, sender='cmux')
                else:
                    print(
                        f'cmux: warning — workflow file {workflow_path} is too long to inject '
                        f'({len(wf_msg)} chars, limit {MAX_MESSAGE_LEN}). '
                        f'Consider splitting it or using the file-based pattern: send a pointer, not the content.',
                        file=sys.stderr,
                    )
        except OSError:
            print(f'cmux: warning — workflow file not found: {workflow_path}', file=sys.stderr)

    if detach:
        print(f"cmux: agent '{name}' started  (cmux attach {name} to open)")
    else:
        cmd_attach(name)


def cmd_clone(source, name, detach=False, workspace=None, no_inject=False,
              unblock=False, allowed_tools=None):
    """Clone an existing agent: copy identity.md, start fresh (no session resume)."""
    import shutil as _shutil
    source_home = os.path.join(STATE_DIR, source)
    source_identity = os.path.join(source_home, 'identity.md')

    if not os.path.exists(source_identity):
        print(
            f"cmux: source agent '{source}' has no identity.md at {source_home}",
            file=sys.stderr,
        )
        sys.exit(1)

    new_home = os.path.join(STATE_DIR, name)
    if os.path.exists(os.path.join(new_home, 'identity.md')):
        print(
            f"cmux: '{name}' already has an identity.md — use 'cmux up {name}' to resume",
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(new_home, exist_ok=True)
    _shutil.copy2(source_identity, os.path.join(new_home, 'identity.md'))

    # Clone marker: triggers the clone startup message in _inject_startup_context,
    # then is deleted so subsequent wakeups are treated as normal resumes.
    with open(os.path.join(new_home, 'clone-source'), 'w') as f:
        f.write(source)

    # Inherit role from source DB registration if not already registered
    source_info = db.get_agent(source)
    if not db.get_agent(name) and source_info:
        db.register_agent(
            name,
            role=source_info.get('role'),
            workspace=workspace,
            allowed_tools=allowed_tools or source_info.get('allowed_tools'),
            no_inject=no_inject,
            unblock=unblock,
        )

    cmd_start(name, detach=detach, workspace=workspace, no_inject=no_inject,
              unblock=unblock, allowed_tools=allowed_tools)


def cmd_start_workspace(workspace):
    """Start all agents registered to a workspace that aren't already running."""
    agents = db.agents_in_workspace(workspace)
    if not agents:
        print(f"cmux: no agents registered for workspace '{workspace}'")
        print(f"      Register agents with: cmux agent register <name> --workspace {workspace}")
        return

    reg = load_registry()
    started = []
    already_running = []

    for agent_info in agents:
        name = agent_info['name']
        if name in reg and session_alive(reg[name]):
            already_running.append(name)
        else:
            print(f"cmux: starting '{name}' in workspace '{workspace}'...")
            cmd_start(
                name,
                initial_prompt=agent_info.get('initial_prompt'),
                detach=True,
                workspace=workspace,
                no_inject=bool(agent_info.get('no_inject', 0)),
                unblock=bool(agent_info.get('unblock', 0)),
                allowed_tools=agent_info.get('allowed_tools'),
                identity_path=agent_info.get('identity_path'),
            )
            started.append(name)

    if started:
        print(f"cmux: started {len(started)} agent(s): {', '.join(started)}")
    if already_running:
        print(f"cmux: already running: {', '.join(already_running)}")


def cmd_ls():
    """List all agents: running first, then registered-but-stopped."""
    reg = load_registry()

    dead = [n for n, info in reg.items() if not session_alive(info)]
    for n in dead:
        _cleanup_files(n)
        del reg[n]
    if dead:
        save_registry(reg)

    all_registered = db.list_agents()
    db_entries = {a['name']: a for a in all_registered}
    running_names = set(reg.keys())

    header = f'{"AGENT":<18} {"STATUS":<10} {"WORKSPACE":<14} ROLE'
    divider = '-' * 72

    rows = []
    # Running — sorted by workspace then name
    for name, info in sorted(reg.items(), key=lambda x: (x[1].get('workspace') or '', x[0])):
        ws = info.get('workspace') or '-'
        role = (db_entries.get(name, {}).get('role') or '')[:28]
        rows.append(f'{name:<18} {"up":<10} {ws:<14} {role}')

    # Stopped/registered
    for a in all_registered:
        if a['name'] not in running_names:
            ws = a.get('workspace') or '-'
            role = (a.get('role') or '')[:28]
            rows.append(f'{a["name"]:<18} {"down":<10} {ws:<14} {role}')

    if not rows:
        print('No agents registered. Run: cmux up <name>')
        return

    print(header)
    print(divider)
    for row in rows:
        print(row)

    if any(a['name'] not in running_names for a in all_registered):
        print(f'\n  cmux up <name>   or   cmux -s <workspace>   to bring agents up')


def cmd_send(name, message, sender=None, quiet=False):
    """Enqueue a message to a named session."""
    reg = _ensure_alive(name)
    if name not in reg:
        print(f"cmux: no session '{name}'", file=sys.stderr)
        sys.exit(1)

    if len(message) > MAX_MESSAGE_LEN:
        print(
            f"cmux: message too long ({len(message)} chars, limit {MAX_MESSAGE_LEN}). "
            f"Split into multiple messages.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not sender:
        sender = os.environ.get('CMUX_SESSION_NAME', 'cmux')

    msg = {'from': sender, 'body': message}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(reg[name]['socket'])
        s.sendall(json.dumps(msg).encode())
        ack = s.recv(256)
        s.close()
        result = json.loads(ack) if ack else {}
        if result.get('ok'):
            if not quiet:
                print(f"cmux: message queued for agent '{name}'")
        else:
            print(f"cmux: daemon error — {result.get('error', 'unknown')}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"cmux: could not reach daemon for '{name}': {e}", file=sys.stderr)
        sys.exit(1)


def cmd_attach(name):
    """Attach to the tmux session containing this agent. Detach with Ctrl-b d."""
    reg = _ensure_alive(name)
    if name not in reg:
        print(f"cmux: no session '{name}'", file=sys.stderr)
        sys.exit(1)
    info = reg[name]
    tmux_sess = info.get('tmux_session', f'cmux-{name}')
    window = info.get('tmux_window', name)
    os.execvp('tmux', ['tmux', 'attach', '-t', f'{tmux_sess}:{window}'])


def cmd_detach(name):
    """Detach all clients from the session containing this agent."""
    reg = load_registry()
    if name not in reg:
        print(f"cmux: no session '{name}'", file=sys.stderr)
        sys.exit(1)
    tmux_sess = reg[name].get('tmux_session', f'cmux-{name}')
    # Ignore non-zero exit — detach-client returns an error if no client is attached,
    # which is a no-op (session still alive), not a failure.
    subprocess.run(['tmux', 'detach-client', '-s', tmux_sess], capture_output=True)
    print(f"cmux: detached from agent '{name}'")


def cmd_stop(name):
    """Stop an agent. In a workspace, kills just the window; standalone kills the session."""
    reg = load_registry()
    if name not in reg:
        print(f"cmux: no session '{name}'", file=sys.stderr)
        sys.exit(1)

    info = reg[name]
    tmux_sess = info.get('tmux_session', f'cmux-{name}')
    workspace = info.get('workspace')

    if workspace:
        subprocess.run(
            ['tmux', 'kill-window', '-t', f'{tmux_sess}:{name}'],
            capture_output=True
        )
    else:
        subprocess.run(['tmux', 'kill-session', '-t', tmux_sess], capture_output=True)

    pid_file = os.path.join(STATE_DIR, f'{name}.daemon.pid')
    try:
        os.kill(int(open(pid_file).read().strip()), 15)
        os.unlink(pid_file)
    except Exception:
        pass

    _cleanup_files(name)
    del reg[name]
    save_registry(reg)
    print(f"cmux: agent '{name}' stopped")
    # Agent remains in agents.db — restart with `cmux start {name}`


# ------------------------------------------------------------------
# Agent registry commands
# ------------------------------------------------------------------

def cmd_agent_register(name, role=None, workspace=None, workflow_path=None,
                       initial_prompt=None, no_inject=False, unblock=False,
                       allowed_tools=None, identity_path=None):
    """Register an agent in the persistent catalog."""
    db.register_agent(name, role=role, workspace=workspace, workflow_path=workflow_path,
                      initial_prompt=initial_prompt, no_inject=no_inject, unblock=unblock,
                      allowed_tools=allowed_tools, identity_path=identity_path)
    print(f"cmux: registered '{name}'")
    if workspace:
        print(f"         workspace:     {workspace}")
    if role:
        print(f"         role:          {role}")
    if workflow_path:
        print(f"         workflow:      {workflow_path}")
    if allowed_tools:
        print(f"         allowed_tools: {allowed_tools}")
    if identity_path:
        print(f"         identity:      {identity_path}")


def cmd_agent_list():
    """Alias for cmux ls — shows all agents (running + stopped)."""
    cmd_ls()


def cmd_agent_rm(name):
    """Remove agent from catalog (does not stop a running agent)."""
    existing = db.get_agent(name)
    if not existing:
        print(f"cmux: '{name}' is not registered")
        return
    db.remove_agent(name)
    print(f"cmux: removed '{name}' from registry")

    reg = load_registry()
    if name in reg and session_alive(reg[name]):
        print(f"         note: '{name}' is still running — stop with: cmux stop {name}")


def cmd_rm(name, force=False):
    """De-register an agent. Agent must be down first.

    By default, the home directory is moved to ~/.cmux/archive/<epoch>_<name>/
    so conversation history and cq data are preserved but the agent won't resume.

    Pass force=True (cmux rm -f <name>) to hard-delete the home dir entirely.
    """
    import shutil as _shutil
    reg = load_registry()
    if name in reg and session_alive(reg[name]):
        print(f"cmux: '{name}' is still running — bring it down first: cmux down {name}",
              file=sys.stderr)
        sys.exit(1)
    db.remove_agent(name)
    reg = load_registry()
    if name in reg:
        del reg[name]
        save_registry(reg)
    home = os.path.join(STATE_DIR, name)
    if os.path.isdir(home):
        if force:
            _shutil.rmtree(home)
            print(f"cmux: '{name}' removed (home deleted)")
        else:
            archive_dir = os.path.join(STATE_DIR, 'archive')
            os.makedirs(archive_dir, exist_ok=True)
            dest = os.path.join(archive_dir, f'{int(time.time())}_{name}')
            _shutil.move(home, dest)
            print(f"cmux: '{name}' archived → {dest}")
    else:
        print(f"cmux: '{name}' de-registered")


def cmd_agent_import_sessions():
    """Bootstrap DB from sessions.json + KNOWN_AGENTS metadata."""
    sessions = load_registry()
    count = 0

    for name, info in sessions.items():
        known = KNOWN_AGENTS.get(name, {})
        db.register_agent(
            name,
            role=known.get('role'),
            workspace=info.get('workspace') or known.get('workspace'),
            workflow_path=known.get('workflow'),
            initial_prompt=info.get('initial_prompt'),
            no_inject=info.get('no_inject', False),
            unblock=info.get('unblock', False),
        )
        print(f"  registered: {name} (from sessions.json)")
        count += 1

    # Register agents in KNOWN_AGENTS that aren't in sessions.json
    for name, known in KNOWN_AGENTS.items():
        if name not in sessions:
            db.register_agent(
                name,
                role=known.get('role'),
                workspace=known.get('workspace'),
                workflow_path=known.get('workflow'),
            )
            print(f"  registered: {name} (from KNOWN_AGENTS, not currently running)")
            count += 1

    print(f"\ncmux: registered {count} agent(s) into {db.DB_PATH}")
    print("      Running agents are unaffected.")


# ------------------------------------------------------------------
# Session health check
# ------------------------------------------------------------------

def cmd_check():
    """Check all running agents for permission-prompt blockage."""
    reg = load_registry()
    alive = {n: info for n, info in reg.items() if session_alive(info)}

    if not alive:
        print('No agents running.')
        return

    print(f'Checking {len(alive)} running agent(s)...')
    stuck = []
    ok = []

    for name, info in sorted(alive.items()):
        target = info.get('tmux_target', _tmux_target(name, info.get('workspace')))
        r = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p', '-S', '-15'],
            capture_output=True, text=True
        )
        pane_raw = r.stdout if r.returncode == 0 else ''
        pane_text = pane_raw.lower()

        # A cmux message left unsubmitted in the input box strands the agent as
        # effectively as a permission prompt: is_idle() reports the non-empty
        # box as busy, so every later message queues behind it. The daemon's
        # reconciler recovers it on its own, so this is reported separately from
        # prompts that genuinely need a human.
        if any(pat in pane_text for pat in _PERM_PATTERNS) or \
                any(pat in pane_text for pat in _TRUST_DIALOG_PATTERNS):
            stuck.append((name, target, 'prompt'))
            print(f'  [STUCK] {name:<16} — blocked, needs interaction ({target})')
        elif _stuck_cmux_message(pane_raw) is not None:
            stuck.append((name, target, 'unsubmitted'))
            print(f'  [STUCK] {name:<16} — message left unsubmitted in the input box ({target})')
        else:
            ok.append(name)
            print(f'  [OK]    {name}')

    print()
    if stuck:
        prompts = [(n, t) for n, t, kind in stuck if kind == 'prompt']
        unsubmitted = [(n, t) for n, t, kind in stuck if kind == 'unsubmitted']
        if prompts:
            print(f'{len(prompts)} agent(s) blocked at permission prompt:')
            for name, target in prompts:
                print(f'  tmux attach -t {target}')
        if unsubmitted:
            print(f'{len(unsubmitted)} agent(s) with an unsubmitted message '
                  f'(the daemon retries these automatically):')
            for name, target in unsubmitted:
                print(f'  tmux attach -t {target}')
    else:
        print(f'All {len(ok)} agent(s) OK.')


def cmd_run(prompt):
    """Run an ephemeral Claude session with `prompt` written as CLAUDE.md.

    No home dir, no agent registry entry, no session tracking. The temp dir
    is deleted on exit — every invocation starts completely fresh.
    """
    import shutil as _shutil
    import tempfile as _tempfile
    claude_bin = os.environ.get('CMUX_CLAUDE_CMD', 'claude')
    tmp = _tempfile.mkdtemp(prefix='cmux-run-')
    try:
        with open(os.path.join(tmp, 'CLAUDE.md'), 'w') as f:
            f.write(prompt)
        os.chdir(tmp)
        subprocess.run([claude_bin])
    finally:
        os.chdir(os.path.expanduser('~'))
        _shutil.rmtree(tmp, ignore_errors=True)


def cmd_wizard():
    """Launch the cmux onboarding wizard as an ephemeral session.

    Bootstraps by running the opening pitch non-interactively first so the
    wizard speaks immediately, then resumes the same session interactively.
    Each run is completely fresh — no home dir, no session tracking.
    """
    import json as _json
    import shutil as _shutil
    import tempfile as _tempfile

    claude_bin = os.environ.get('CMUX_CLAUDE_CMD', 'claude')
    prompt_path = os.path.join(os.path.dirname(__file__), 'wizard.md')
    try:
        wizard_prompt = open(prompt_path).read().strip()
    except OSError:
        print(f'cmux: wizard prompt not found at {prompt_path}', file=sys.stderr)
        sys.exit(1)

    tmp = _tempfile.mkdtemp(prefix='cmux-wizard-')
    try:
        with open(os.path.join(tmp, 'CLAUDE.md'), 'w') as f:
            f.write(wizard_prompt)
        os.chdir(tmp)

        # Bootstrap: non-interactive run to trigger STEP 0 and capture session ID.
        boot = subprocess.run(
            [claude_bin, '-p', '.', '--output-format', 'json'],
            capture_output=True, text=True,
        )
        session_id = None
        if boot.returncode == 0 and boot.stdout.strip():
            try:
                data = _json.loads(boot.stdout)
                session_id = data.get('session_id')
                pitch = data.get('result', '').strip()
                if pitch:
                    print(pitch)
                    print()
            except (_json.JSONDecodeError, AttributeError):
                pass

        if session_id:
            subprocess.run([claude_bin, '--resume', session_id])
        else:
            print('cmux: type anything to begin (try "hi")')
            subprocess.run([claude_bin])
    finally:
        os.chdir(os.path.expanduser('~'))
        _shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

USAGE = """\
cmux — Claude Code multiplexer

Usage:
  cmux --wizard                             Interactive onboarding guide (start here!)
  cmux run "<system prompt>"                Ephemeral Claude session — no home dir, no history
  cmux [-s workspace] <agent>               Bring agent up and attach (shorthand)
  cmux [-s workspace] up <agent> [-d] [--no-inject] [--unblock] [--allowed-tools <tools>] [-i <path>] [-- "initial prompt"]
  cmux -f <claude-session-id> <agent> [-d] [--force]
                                             Promote an existing Claude session into <agent>
                                             (resumes that session id; run from the same cwd
                                             it was started in — same as any --resume).
                                             Refuses to overwrite an agent's existing, different,
                                             still-valid session unless --force is also given.
  cmux clone <source> <new-name> [-d] [--workspace <ws>] [--allowed-tools <tools>]
  cmux [-s workspace] down <agent>          Take agent offline (home dir preserved)
  cmux rm <agent>                           De-register agent from DB; home dir preserved (must be down first)
  cmux [-s workspace]                       Bring up ALL registered agents in workspace
  cmux ls                                   List agents (running + registered-but-stopped)
  cmux send <agent> <message>               Enqueue a message (max 2000 chars)
  cmux attach <agent>                       Attach terminal to agent
  cmux detach <agent>                       Detach (agent keeps running)
  cmux inbox <agent>                        Print and clear queued messages (--no-inject agents)
  cmux check                                Check all agents for permission-prompt blockage
  cmux upgrade                              Upgrade cmux to the latest version from GitHub
  cmux upgrade --testing [branch]           Install from a branch (default: 'testing') instead
                                             of main — for validating in-progress fixes on one
                                             machine without affecting `cmux upgrade` elsewhere

  start / stop still work as aliases for up / down.
  First start launches fresh; subsequent starts resume via --resume <session-id>.

Agent registry (persistent catalog):
  cmux agent register <name> [--role <r>] [--workspace <ws>] [--workflow <path>] [--no-inject] [--unblock] [--allowed-tools <tools>] [-i <identity-path>] [-- "prompt"]
  cmux agent list                           Show all registered agents (running + stopped)
  cmux agent import-sessions                Bootstrap registry from current sessions.json

Workspaces group agents into one tmux session as windows/tabs:
  cmux -s ol-loop up fran -d               Add fran to ol-loop workspace
  cmux -s ol-loop                          Bring up ALL ol-loop agents (post-reboot restore)

Task tracking: use cq (per-agent issue tracker). Run: cq issue list

Keyboard shortcuts (when attached):
  Ctrl-b d                                 Detach
  Ctrl-b n / Ctrl-b p                      Next / previous agent window
  Ctrl-b [                                 Scroll mode (q to exit)
"""


def _require_tmux():
    if subprocess.run(['which', 'tmux'], capture_output=True).returncode != 0:
        print('cmux: tmux is required but not found — install with: brew install tmux', file=sys.stderr)
        sys.exit(1)


def cmd_upgrade(branch=None):
    """Upgrade cmux from GitHub.

    branch=None installs the default branch (main) — the stable path every
    agent gets from a plain `cmux upgrade`. Passing a branch (via
    --testing [branch]) installs that branch instead, so an in-progress fix
    can be validated on one machine without changing what `cmux upgrade`
    installs anywhere else — nothing about the default path is touched.
    """
    import tempfile as _tempfile
    import shutil as _shutil
    tmp = _tempfile.mkdtemp()
    try:
        clone_cmd = ['git', 'clone', '--depth=1']
        if branch:
            clone_cmd += ['--branch', branch]
        clone_cmd += ['https://github.com/mekarpeles/cmux.git', os.path.join(tmp, 'cmux')]
        label = f"testing branch '{branch}'" if branch else 'latest'
        print(f'cmux: cloning {label}...')
        r = subprocess.run(clone_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'cmux: clone failed — {r.stderr.strip()}', file=sys.stderr)
            sys.exit(1)
        if branch:
            print(f"cmux: ⚠ installing from '{branch}' — not the stable release. "
                  f"Run `cmux upgrade` (no flags) to return to main.")
        print('cmux: upgrading...')
        env = os.environ.copy()
        env['UV_VENV_CLEAR'] = '1'  # uv-backed pipx refuses to overwrite existing venv without this
        env['UV_NO_CACHE'] = '1'    # prevent uv from serving stale cached sdists for VCS deps like claudio
        r2 = subprocess.run(['pipx', 'install', '--force', os.path.join(tmp, 'cmux')], env=env)
        if r2.returncode != 0:
            print(
                'cmux: upgrade failed. If pipx uses uv and the venv already exists, run:\n'
                '  UV_VENV_CLEAR=1 UV_NO_CACHE=1 pipx install --force git+https://github.com/mekarpeles/cmux.git',
                file=sys.stderr,
            )
            sys.exit(r2.returncode)
        # Force-reinject claudio separately: pipx resolves VCS deps at install time and uv may
        # cache them by commit hash even with UV_NO_CACHE, so an explicit inject guarantees the
        # latest commit is fetched from GitHub regardless of what was cached.
        print('cmux: upgrading claudio...')
        r3 = subprocess.run(
            ['pipx', 'inject', '--force', 'cmux', 'git+https://github.com/mekarpeles/claudio.git'],
            env=env,
        )
        if r3.returncode != 0:
            print('cmux: claudio upgrade failed — cmux itself was upgraded successfully.', file=sys.stderr)
        print('cmux: upgrade complete.')
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(USAGE)
        sys.exit(0)

    if args[0] in ('--wizard', 'wizard'):
        cmd_wizard()
        return

    if args[0] == 'upgrade':
        upgrade_args = args[1:]
        branch = None
        if '--testing' in upgrade_args:
            idx = upgrade_args.index('--testing')
            rest = upgrade_args[idx + 1:]
            branch = rest[0] if rest and not rest[0].startswith('-') else 'testing'
        cmd_upgrade(branch=branch)
        return

    if args[0] == 'run':
        if len(args) < 2:
            print('Usage: cmux run "<system prompt>"', file=sys.stderr)
            sys.exit(1)
        cmd_run(args[1])
        return

    _require_tmux()

    # Parse -s <workspace> and -f <claude-session-id> before the subcommand.
    # Either may appear first; both are optional and independent.
    workspace = None
    from_session = None
    while len(args) >= 2 and args[0] in ('-s', '-f'):
        if args[0] == '-s':
            workspace = args[1]
        else:
            from_session = args[1]
        args = args[2:]
    if workspace is not None and not args:
        # `cmux -s ol-loop` with no further args → start whole workspace
        cmd_start_workspace(workspace)
        return

    cmd = args[0]

    # ------------------------------------------------------------------
    # Agent registry subcommands
    # ------------------------------------------------------------------
    if cmd == 'agent':
        sub = args[1] if len(args) > 1 else ''
        if sub == 'list':
            cmd_agent_list()
        elif sub == 'rm':
            if len(args) < 3:
                print('cmux: agent rm requires a name', file=sys.stderr)
                sys.exit(1)
            cmd_agent_rm(args[2])
        elif sub == 'import-sessions':
            cmd_agent_import_sessions()
        elif sub == 'register':
            # cmux agent register <name> [--role r] [--workspace ws] [--workflow p] [--no-inject] [--allowed-tools t] [--identity p] [-- "prompt"]
            remaining = args[2:]
            if not remaining or remaining[0].startswith('-'):
                print('cmux: agent register requires a name', file=sys.stderr)
                sys.exit(1)
            name = remaining[0]
            reg_args = ['--identity' if a == '-i' else a for a in remaining[1:]]
            _, flags = _parse_flags(reg_args,
                                    kv=('role', 'workspace', 'workflow', 'allowed-tools', 'identity'),
                                    bools=('no-inject', 'unblock'))
            workspace_val = flags['workspace'] or workspace  # -s flag fallback
            cmd_agent_register(name, role=flags['role'], workspace=workspace_val,
                               workflow_path=flags['workflow'],
                               initial_prompt=flags.get('__prompt__'),
                               no_inject=flags['no-inject'],
                               unblock=flags['unblock'],
                               allowed_tools=flags.get('allowed-tools'),
                               identity_path=flags.get('identity'))
        else:
            print(f'cmux: unknown agent subcommand {sub!r}', file=sys.stderr)
            print('  subcommands: register, list, rm, import-sessions')
            sys.exit(1)
        return

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    if cmd == 'check':
        cmd_check()
        return

    # ------------------------------------------------------------------
    # Existing commands
    # ------------------------------------------------------------------

    # `rm` is top-level, not under `agent`
    if cmd == 'rm':
        rm_args = args[1:]
        force = '-f' in rm_args or '--force' in rm_args
        names = [a for a in rm_args if not a.startswith('-')]
        if not names:
            print('cmux: rm requires an agent name', file=sys.stderr)
            sys.exit(1)
        cmd_rm(names[0], force=force)
        return

    # ------------------------------------------------------------------
    # Clone
    # ------------------------------------------------------------------
    if cmd == 'clone':
        if len(args) < 3:
            print('cmux: clone requires a source and a new name', file=sys.stderr)
            print('  usage: cmux clone <source> <new-name> [-d] [--workspace <ws>]')
            sys.exit(1)
        source, new_name = args[1], args[2]
        rest = args[3:]
        detach = '-d' in rest or '--detach' in rest
        no_inject = '--no-inject' in rest
        unblock = '--unblock' in rest
        _, clone_flags = _parse_flags(rest, kv=('workspace', 'allowed-tools'))
        clone_ws = clone_flags.get('workspace') or workspace
        cmd_clone(source, new_name, detach=detach, workspace=clone_ws,
                  no_inject=no_inject, unblock=unblock,
                  allowed_tools=clone_flags.get('allowed-tools'))
        return

    # Normalise aliases: up=start, down=stop
    if cmd == 'up':
        cmd = 'start'
    elif cmd == 'down':
        cmd = 'stop'

    # Shorthand: `cmux <name> [-s ws] [flags] [-- "prompt"]` → up + attach
    # -s may appear before OR after the name; same for -i/--identity.
    if cmd not in ('start', 'ls', 'send', 'attach', 'detach', 'stop', 'inbox'):
        if cmd.startswith('-'):
            print(f'cmux: unknown option {cmd!r}', file=sys.stderr)
            print(USAGE)
            sys.exit(1)
        detach = '-d' in args or '--detach' in args
        no_inject = '--no-inject' in args
        unblock = '--unblock' in args
        force = '--force' in args
        flag_args = [a for a in args[1:]
                     if a not in ('-d', '--detach', '--no-inject', '--unblock', '--force')]
        # Normalise short flags so _parse_flags can handle them as kv pairs.
        flag_args = [{'--identity': '--identity', '-i': '--identity',
                      '--session': '--session', '-s': '--session'}.get(a, a)
                     for a in flag_args]
        _, sh_flags = _parse_flags(flag_args, kv=('allowed-tools', 'identity', 'session'))
        sh_workspace = sh_flags.get('session') or workspace
        kv_vals = {sh_flags.get('allowed-tools'), sh_flags.get('identity'), sh_flags.get('session')} - {None}
        remaining = [a for a in flag_args
                     if a not in ('--allowed-tools', '--identity', '--session') and a not in kv_vals]
        try:
            sep = remaining.index('--')
            initial_prompt = ' '.join(remaining[sep + 1:]) or None
        except ValueError:
            initial_prompt = None
        cmd_start(cmd, initial_prompt=initial_prompt, detach=detach, workspace=sh_workspace,
                  no_inject=no_inject, unblock=unblock,
                  allowed_tools=sh_flags.get('allowed-tools'),
                  identity_path=sh_flags.get('identity'),
                  from_session=from_session, force=force)
        return

    if cmd == 'start':
        if len(args) < 2:
            print('cmux: up requires an agent name', file=sys.stderr)
            sys.exit(1)
        detach = '-d' in args or '--detach' in args
        no_inject = '--no-inject' in args
        unblock = '--unblock' in args
        force = '--force' in args
        flag_args = [a for a in args[1:]
                     if a not in ('-d', '--detach', '--no-inject', '--unblock', '--force')]
        flag_args = [{'--identity': '--identity', '-i': '--identity',
                      '--session': '--session', '-s': '--session'}.get(a, a)
                     for a in flag_args]
        _, up_flags = _parse_flags(flag_args, kv=('allowed-tools', 'identity', 'session'))
        up_workspace = up_flags.get('session') or workspace
        kv_vals = {up_flags.get('allowed-tools'), up_flags.get('identity'), up_flags.get('session')} - {None}
        remaining = [a for a in flag_args
                     if a not in ('--allowed-tools', '--identity', '--session') and a not in kv_vals]
        name = remaining[0]
        try:
            sep = remaining.index('--')
            initial_prompt = ' '.join(remaining[sep + 1:]) or None
        except ValueError:
            initial_prompt = None
        cmd_start(name, initial_prompt=initial_prompt, detach=detach, workspace=up_workspace,
                  no_inject=no_inject, unblock=unblock,
                  allowed_tools=up_flags.get('allowed-tools'),
                  identity_path=up_flags.get('identity'),
                  from_session=from_session, force=force)

    elif cmd == 'ls':
        cmd_ls()

    elif cmd == 'send':
        if len(args) < 3:
            print('cmux: send requires a name and message', file=sys.stderr)
            sys.exit(1)
        sender = None
        msg_args = args[2:]
        if '--from' in msg_args:
            i = msg_args.index('--from')
            sender = msg_args[i + 1]
            msg_args = msg_args[:i] + msg_args[i + 2:]
        cmd_send(args[1], ' '.join(msg_args), sender)

    elif cmd == 'attach':
        if len(args) < 2:
            print('cmux: attach requires a session name', file=sys.stderr)
            sys.exit(1)
        cmd_attach(args[1])

    elif cmd == 'detach':
        if len(args) < 2:
            print('cmux: detach requires a session name', file=sys.stderr)
            sys.exit(1)
        cmd_detach(args[1])

    elif cmd == 'inbox':
        if len(args) < 2:
            print('cmux: inbox requires a session name', file=sys.stderr)
            sys.exit(1)
        cmd_inbox(args[1])

    elif cmd == 'stop':
        if len(args) < 2:
            print('cmux: down requires an agent name', file=sys.stderr)
            sys.exit(1)
        cmd_stop(args[1])


if __name__ == '__main__':
    main()
