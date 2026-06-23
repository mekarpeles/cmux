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
    for suffix in ('.sock', '.daemon.pid', '.daemon.log'):
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


def _snapshot_claude_sessions():
    """Return {filepath: mtime} for all current Claude session files."""
    projects = os.path.expanduser('~/.claude/projects')
    snapshot = {}
    if not os.path.isdir(projects):
        return snapshot
    for proj in os.listdir(projects):
        proj_path = os.path.join(projects, proj)
        if os.path.isdir(proj_path):
            for f in os.listdir(proj_path):
                if f.endswith('.jsonl'):
                    fp = os.path.join(proj_path, f)
                    try:
                        snapshot[fp] = os.path.getmtime(fp)
                    except OSError:
                        pass
    return snapshot


def _store_session_id(home, pre_snapshot):
    """Detect which Claude session file is new/updated since pre_snapshot and store its UUID."""
    post = _snapshot_claude_sessions()
    candidates = [
        fp for fp, mtime in post.items()
        if fp not in pre_snapshot or mtime > pre_snapshot[fp]
    ]
    if not candidates:
        return
    newest = max(candidates, key=lambda fp: post[fp])
    session_uuid = os.path.splitext(os.path.basename(newest))[0]
    try:
        with open(os.path.join(home, 'last-session-id'), 'w') as f:
            f.write(session_uuid)
    except OSError:
        pass


def _inject_identity(home):
    """Inject identity.md contents as a cmux message. Warns to stderr if too long."""
    identity_path = os.path.join(home, 'identity.md')
    if not os.path.exists(identity_path):
        return
    try:
        content = open(identity_path).read().strip()
    except Exception:
        return
    if not content:
        return
    msg = f'[cmux]: Your identity/role context from {identity_path}:\n\n{content}'
    if len(msg) <= MAX_MESSAGE_LEN:
        # Derive agent name from home dir basename for cmd_send
        name = os.path.basename(home)
        cmd_send(name, msg, sender='cmux')
    else:
        print(
            f'cmux: warning — {identity_path} is too long to inject '
            f'({len(msg)} chars, limit {MAX_MESSAGE_LEN}). '
            f'Trim it or split role definition from procedures into a separate workflow file.',
            file=sys.stderr,
        )


def cmd_start(name, initial_prompt=None, detach=False, workspace=None, no_inject=False, unblock=False):
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

    # Scaffold identity.md from initial_prompt if neither exists yet.
    if initial_prompt:
        identity_path = os.path.join(home, 'identity.md')
        if not os.path.exists(identity_path):
            with open(identity_path, 'w') as _f:
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
    if stored_id:
        claude_cmd = f'CMUX_SESSION_NAME={name} {claude_bin} --resume {stored_id}'
    else:
        claude_cmd = f'CMUX_SESSION_NAME={name} {claude_bin}'

    # Snapshot existing Claude session files so we can detect the new one after start.
    _pre_sessions = _snapshot_claude_sessions()

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
        'socket': os.path.join(STATE_DIR, f'{name}.sock'),
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
    )

    _wait_for_socket(reg[name]['socket'])

    # Detect and store the Claude session ID so next restart uses --resume.
    _store_session_id(home, _pre_sessions)

    cmux_info = (
        f'You are a cmux agent named "{name}". '
        f'Your home directory is {home} — your personal context, notes, and issue queue live there. '
        f'Your issue queue: run `cq issue list` (auto-resolves to your home dir). '
        f'Read {os.path.join(home, "MIGRATE.md")} to migrate your context into your home dir. '
        f'Wait for instructions before doing anything — do not introduce yourself, '
        f'send messages, or take any action until you receive a task. '
        f'Messages from other agents arrive automatically as: [sender@cmux]: <message>. '
        f'To message another agent: cmux send <agent-name> "<message>". '
        f'Your name ("{name}") is set in your environment — do NOT pass --from. '
        f'To see all running agents: cmux ls.'
    )
    cmd_send(name, cmux_info, sender='cmux')

    # Inject identity.md — gives the agent their role context on every startup
    # without relying solely on session history (which may be compacted).
    _inject_identity(home)

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


def cmd_send(name, message, sender=None):
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
                       initial_prompt=None, no_inject=False, unblock=False):
    """Register an agent in the persistent catalog."""
    db.register_agent(name, role=role, workspace=workspace, workflow_path=workflow_path,
                      initial_prompt=initial_prompt, no_inject=no_inject, unblock=unblock)
    print(f"cmux: registered '{name}'")
    if workspace:
        print(f"         workspace: {workspace}")
    if role:
        print(f"         role:      {role}")
    if workflow_path:
        print(f"         workflow:  {workflow_path}")


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
            print(f'  [STUCK] {name:<16} — permission prompt detected ({target})')
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


def cmd_wizard():
    """Launch the cmux wizard — an interactive onboarding guide.

    Runs claude directly in the current terminal from ~/.cmux/.wizard/, where
    wizard.md is installed as CLAUDE.md. Claude Code loads CLAUDE.md automatically
    as project context, so the wizard gets its full script with no flags needed.

    No agent name is claimed, no DB entry is created. The session lives in
    ~/.cmux/.wizard/ and resumes via --resume <id> on subsequent cmux --wizard calls.
    """
    prompt_path = os.path.join(os.path.dirname(__file__), 'wizard.md')
    try:
        wizard_prompt = open(prompt_path).read().strip()
    except OSError:
        print(f'cmux: wizard prompt not found at {prompt_path}', file=sys.stderr)
        sys.exit(1)

    wizard_dir = os.path.join(STATE_DIR, '.wizard')
    os.makedirs(wizard_dir, exist_ok=True)

    claude_md = os.path.join(wizard_dir, 'CLAUDE.md')
    with open(claude_md, 'w') as f:
        f.write(wizard_prompt)

    claude_bin = os.environ.get('CMUX_CLAUDE_CMD', 'claude')

    # Use --resume <id> if we have a stored session, plain claude on first run.
    session_id_path = os.path.join(wizard_dir, 'last-session-id')
    stored_id = None
    if os.path.exists(session_id_path):
        stored_id = open(session_id_path).read().strip() or None

    pre_snapshot = _snapshot_claude_sessions()

    print('cmux: launching wizard — your interactive cmux guide')
    print('      (/exit or Ctrl-C when done; next cmux --wizard resumes here)\n')

    os.chdir(wizard_dir)
    claude_args = [claude_bin, '--resume', stored_id] if stored_id else [claude_bin]
    subprocess.run(claude_args)

    # Store session ID so next run resumes this exact session.
    _store_session_id(wizard_dir, pre_snapshot)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

USAGE = """\
cmux — Claude Code multiplexer

Usage:
  cmux --wizard                             Interactive onboarding guide (start here!)
  cmux [-s workspace] <agent>               Bring agent up and attach (shorthand)
  cmux [-s workspace] up <agent> [-d] [--no-inject] [--unblock] [-- "initial prompt"]
  cmux [-s workspace] down <agent>          Take agent offline (home dir preserved)
  cmux rm <agent>                           De-register agent from DB; home dir preserved (must be down first)
  cmux [-s workspace]                       Bring up ALL registered agents in workspace
  cmux ls                                   List agents (running + registered-but-stopped)
  cmux send <agent> <message>               Enqueue a message (max 2000 chars)
  cmux attach <agent>                       Attach terminal to agent
  cmux detach <agent>                       Detach (agent keeps running)
  cmux inbox <agent>                        Print and clear queued messages (--no-inject agents)
  cmux check                                Check all agents for permission-prompt blockage

  start / stop still work as aliases for up / down.
  First start launches fresh; subsequent starts resume via --resume <session-id>.

Agent registry (persistent catalog):
  cmux agent register <name> [--role <r>] [--workspace <ws>] [--workflow <path>] [--no-inject] [--unblock] [-- "prompt"]
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


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(USAGE)
        sys.exit(0)

    if args[0] in ('--wizard', 'wizard'):
        cmd_wizard()
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
            # cmux agent register <name> [--role r] [--workspace ws] [--workflow p] [--no-inject] [-- "prompt"]
            remaining = args[2:]
            if not remaining or remaining[0].startswith('-'):
                print('cmux: agent register requires a name', file=sys.stderr)
                sys.exit(1)
            name = remaining[0]
            _, flags = _parse_flags(remaining[1:], kv=('role', 'workspace', 'workflow'),
                                    bools=('no-inject', 'unblock'))
            workspace_val = flags['workspace'] or workspace  # -s flag fallback
            cmd_agent_register(name, role=flags['role'], workspace=workspace_val,
                               workflow_path=flags['workflow'],
                               initial_prompt=flags.get('__prompt__'),
                               no_inject=flags['no-inject'],
                               unblock=flags['unblock'])
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
        remaining = [a for a in args[1:] if a not in ('-d', '--detach', '--no-inject', '--unblock')]
        name = remaining[0]
        try:
            sep = remaining.index('--')
            initial_prompt = ' '.join(remaining[sep + 1:]) or None
        except ValueError:
            initial_prompt = None
        cmd_start(name, initial_prompt=initial_prompt, detach=detach, workspace=workspace,
                  no_inject=no_inject, unblock=unblock)

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
