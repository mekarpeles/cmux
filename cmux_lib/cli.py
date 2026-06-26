"""cmux — Claude multiplexer. Manage persistent Claude sessions."""

import json
import os
import socket
import subprocess
import sys
import time

from cmux_lib import db
from cmux_lib.daemon import _PERM_PATTERNS

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
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except Exception:
        return {}


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
    with open(REGISTRY, 'w') as f:
        json.dump(reg, f, indent=2)


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


def _inject_startup_context(name, home):
    """Inject exactly one startup message.

    Clone start (clone-source file present):
        name + home + source info + @clone_readme.md; marker deleted after send.
    First start (no identity.md):
        name + home + @initial-prompt.md (if any) + @ONBOARDING + @IDENTITY_GUIDE
    Subsequent starts:
        inline wakeup line + @initial-prompt.md (if any)

    initial-prompt.md is written by cmd_start before this call.
    clone-source is written by cmd_clone before calling cmd_start.
    """
    identity_path = os.path.join(home, 'identity.md')
    prompt_path = os.path.join(home, 'initial-prompt.md')
    clone_marker = os.path.join(home, 'clone-source')
    has_prompt = os.path.exists(prompt_path)

    if os.path.exists(clone_marker):
        source_name = open(clone_marker).read().strip()
        source_home = os.path.join(os.path.dirname(home), source_name)
        parts = [
            f'Session: "{name}" — home: {home}.',
            f'You are a clone of "{source_name}" (source home: {source_home}).',
        ]
        if has_prompt:
            parts.append(f'@{prompt_path}')
        parts.append(f'@{os.path.join(_PKG_DIR, "clone_readme.md")}')
        cmd_send(name, ' '.join(parts), sender='cmux', quiet=True)
        os.unlink(clone_marker)  # delivered — subsequent wakeups are normal
        return

    has_identity = os.path.exists(identity_path)
    parts = []
    if not has_identity:
        parts.append(f'Session: "{name}" — home: {home}')
    else:
        parts.append(f'Resuming "{name}" (ref: {identity_path}).')

    if has_prompt:
        parts.append(f'@{prompt_path}')

    if not has_identity:
        parts.append(f'@{os.path.join(_PKG_DIR, "ONBOARDING.md")}')
        parts.append(f'@{os.path.join(_PKG_DIR, "IDENTITY_GUIDE.md")}')

    cmd_send(name, ' '.join(parts), sender='cmux', quiet=True)


def cmd_start(name, initial_prompt=None, detach=False, workspace=None, no_inject=False,
              unblock=False, allowed_tools=None, identity_path=None):
    """Start a new cmux session (window). Uses DB registration if available."""
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

    claude_bin = os.environ.get('CMUX_CLAUDE_CMD', 'claude')
    # Use --resume <id> if we tracked the agent's last session, plain claude
    # on first start (no flags = new session, which we then track for next time).
    session_id_path = os.path.join(home, 'last-session-id')
    stored_id = None
    if os.path.exists(session_id_path):
        stored_id = open(session_id_path).read().strip() or None
    env_prefix = f'CMUX_SESSION_NAME={name} CLAUDIO_STATE_DIR={home}'
    allowed_tools_flag = f' --allowedTools {allowed_tools}' if allowed_tools else ''
    if stored_id:
        claude_cmd = f'{env_prefix} {claude_bin} --resume {stored_id}{allowed_tools_flag}'
    else:
        claude_cmd = f'{env_prefix} {claude_bin}{allowed_tools_flag}'

    # Scope session detection to the CWD we're launching from — avoids picking up
    # other active Claude sessions as false positives.
    _agent_cwd = os.getcwd()
    _project_dir = _cwd_to_claude_project_dir(_agent_cwd)
    _pre_sessions = _snapshot_claude_sessions(project_dir=_project_dir)

    sess_exists = subprocess.run(
        ['tmux', 'has-session', '-t', tmux_sess], capture_output=True
    ).returncode == 0

    if not sess_exists:
        subprocess.run(
            ['tmux', 'new-session', '-d', '-s', tmux_sess, '-n', name, claude_cmd],
            check=True
        )
    else:
        subprocess.run(
            ['tmux', 'new-window', '-t', tmux_sess, '-n', name, claude_cmd],
            check=True
        )

    daemon_log = os.path.join(STATE_DIR, f'{name}.daemon.log')
    try:
        os.unlink(os.path.join(STATE_DIR, f'{name}.daemon.pid'))
    except FileNotFoundError:
        pass
    daemon_args = [sys.executable, '-m', 'cmux_lib.daemon', name, target]
    if no_inject:
        daemon_args.append('--no-inject')
    if unblock:
        daemon_args.append('--unblock')
    daemon_proc = subprocess.Popen(
        daemon_args,
        stdout=open(daemon_log, 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
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

    _inject_startup_context(name, home)

    # Detect and store Claude session ID after message injection — JSONL is created
    # lazily (often after the first exchange), so checking immediately after socket
    # ready misses it. Retries with delay until the file appears.
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
    reg = load_registry()
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
    reg = load_registry()
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


def cmd_rm(name):
    """De-register an agent: removes from agents.db only. Agent must be down first.

    The home directory (~/.cmux/{name}/) is intentionally preserved — it contains
    cq history, scripts, and logs that have provenance value. Remove it manually
    if you need to reclaim the space.
    """
    reg = load_registry()
    if name in reg and session_alive(reg[name]):
        print(f"cmux: '{name}' is still running — bring it down first: cmux down {name}",
              file=sys.stderr)
        sys.exit(1)
    db.remove_agent(name)
    home = os.path.join(STATE_DIR, name)
    print(f"cmux: '{name}' de-registered")
    if os.path.isdir(home):
        print(f"         home dir preserved: {home}")


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
        pane_text = r.stdout.lower() if r.returncode == 0 else ''

        is_stuck = any(pat in pane_text for pat in _PERM_PATTERNS)
        if is_stuck:
            stuck.append((name, target))
            print(f'  [STUCK] {name:<16} — blocked, needs interaction ({target})')
        else:
            ok.append(name)
            print(f'  [OK]    {name}')

    print()
    if stuck:
        print(f'{len(stuck)} agent(s) blocked at permission prompt:')
        for name, target in stuck:
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


def cmd_upgrade():
    """Upgrade cmux to the latest version from GitHub."""
    import tempfile as _tempfile
    import shutil as _shutil
    tmp = _tempfile.mkdtemp()
    try:
        print('cmux: cloning latest...')
        r = subprocess.run(
            ['git', 'clone', '--depth=1', 'https://github.com/mekarpeles/cmux.git', os.path.join(tmp, 'cmux')],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f'cmux: clone failed — {r.stderr.strip()}', file=sys.stderr)
            sys.exit(1)
        print('cmux: upgrading...')
        r2 = subprocess.run(['pipx', 'install', '--force', os.path.join(tmp, 'cmux')])
        if r2.returncode != 0:
            sys.exit(r2.returncode)
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
        cmd_upgrade()
        return

    if args[0] == 'run':
        if len(args) < 2:
            print('Usage: cmux run "<system prompt>"', file=sys.stderr)
            sys.exit(1)
        cmd_run(args[1])
        return

    _require_tmux()

    # Parse -s <workspace> before the subcommand
    workspace = None
    if len(args) >= 2 and args[0] == '-s':
        workspace = args[1]
        args = args[2:]
        if not args:
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
        if len(args) < 2:
            print('cmux: rm requires an agent name', file=sys.stderr)
            sys.exit(1)
        cmd_rm(args[1])
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

    # Shorthand: `cmux [-s ws] <name>` → up + attach
    if cmd not in ('start', 'ls', 'send', 'attach', 'detach', 'stop', 'inbox'):
        if cmd.startswith('-'):
            print(f'cmux: unknown option {cmd!r}', file=sys.stderr)
            print(USAGE)
            sys.exit(1)
        cmd_start(cmd, detach=False, workspace=workspace)
        return

    if cmd == 'start':
        if len(args) < 2:
            print('cmux: up requires an agent name', file=sys.stderr)
            sys.exit(1)
        detach = '-d' in args or '--detach' in args
        no_inject = '--no-inject' in args
        unblock = '--unblock' in args
        flag_args = [a for a in args[1:] if a not in ('-d', '--detach', '--no-inject', '--unblock')]
        flag_args = ['--identity' if a == '-i' else a for a in flag_args]
        _, up_flags = _parse_flags(flag_args, kv=('allowed-tools', 'identity'))
        kv_vals = {up_flags.get('allowed-tools'), up_flags.get('identity')} - {None}
        remaining = [a for a in flag_args
                     if a not in ('--allowed-tools', '--identity') and a not in kv_vals]
        name = remaining[0]
        try:
            sep = remaining.index('--')
            initial_prompt = ' '.join(remaining[sep + 1:]) or None
        except ValueError:
            initial_prompt = None
        cmd_start(name, initial_prompt=initial_prompt, detach=detach, workspace=workspace,
                  no_inject=no_inject, unblock=unblock,
                  allowed_tools=up_flags.get('allowed-tools'),
                  identity_path=up_flags.get('identity'))

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
