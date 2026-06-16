"""cmux — Claude multiplexer. Manage persistent Claude sessions."""

import json
import os
import socket
import subprocess
import sys
import time

STATE_DIR = os.environ.get('CMUX_STATE_DIR', os.path.expanduser('~/.cmux'))
REGISTRY = os.path.join(STATE_DIR, 'sessions.json')
MAX_MESSAGE_LEN = 2000


# ------------------------------------------------------------------
# Registry helpers
# ------------------------------------------------------------------

def load_registry():
    try:
        return json.load(open(REGISTRY))
    except Exception:
        return {}


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
    # Clear after reading
    open(inbox_path, 'w').close()


def cmd_start(name, initial_prompt=None, detach=False, workspace=None, no_inject=False):
    """Start a new cmux session (window). Attaches immediately unless --detach."""
    reg = load_registry()

    if name in reg and session_alive(reg[name]):
        print(f"cmux: agent '{name}' already running — attaching")
        if not detach:
            cmd_attach(name)
        return

    tmux_sess = _tmux_session(name, workspace)
    target = _tmux_target(name, workspace)

    claude_bin = os.environ.get('CMUX_CLAUDE_CMD', 'claude')
    claude_cmd = f'CMUX_SESSION_NAME={name} {claude_bin}'

    os.makedirs(STATE_DIR, exist_ok=True)

    # Create the tmux session if it doesn't exist, otherwise add a window
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
    daemon_pid_file = os.path.join(STATE_DIR, f'{name}.daemon.pid')
    daemon_args = [sys.executable, '-m', 'cmux_lib.daemon', name, target]
    if no_inject:
        daemon_args.append('--no-inject')
    daemon_proc = subprocess.Popen(
        daemon_args,
        stdout=open(daemon_log, 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(daemon_pid_file, 'w') as f:
        f.write(str(daemon_proc.pid))

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
    }
    save_registry(reg)

    # Wait for daemon socket to be ready, then enqueue startup messages
    _wait_for_socket(reg[name]['socket'])
    cmux_info = (
        f'You are a cmux agent named "{name}". '
        f'Wait for instructions before doing anything — do not introduce yourself, '
        f'send messages, or take any action until you receive a task. '
        f'Messages from other agents are delivered to you automatically when you are idle — '
        f'you do NOT need to poll, fetch, or receive them. They arrive as: [sender@cmux]: <message>. '
        f'To message another agent, run: cmux send <agent-name> "<your message>". '
        f'Your name ("{name}") is already known from your environment — do NOT pass --from. '
        f'To see all running agents, run: cmux ls. '
        f'Do NOT use `cmux start` unless instructed — you are not responsible for starting other agents.'
    )
    if initial_prompt:
        cmd_send(name, f'{cmux_info}\n\n{initial_prompt}', sender='cmux')
    else:
        cmd_send(name, cmux_info, sender='cmux')

    if detach:
        print(f"cmux: agent '{name}' started  (cmux attach {name} to open)")
    else:
        cmd_attach(name)


def cmd_ls():
    """List all agents, pruning dead ones."""
    reg = load_registry()
    if not reg:
        print('No agents running.')
        return

    dead = [n for n, info in reg.items() if not session_alive(info)]
    for n in dead:
        _cleanup_files(n)
        del reg[n]
    if dead:
        save_registry(reg)
    if not reg:
        print('No agents running.')
        return

    print(f'{"AGENT":<20} {"WORKSPACE":<16} STARTED')
    print('-' * 60)
    for name, info in sorted(reg.items(), key=lambda x: (x[1].get('workspace') or '', x[0])):
        ws = info.get('workspace') or '-'
        print(f'{name:<20} {ws:<16} {info.get("started", "")}')


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
    # Attach to the session and select the right window
    os.execvp('tmux', ['tmux', 'attach', '-t', f'{tmux_sess}:{window}'])


def cmd_detach(name):
    """Detach all clients from the session containing this agent."""
    reg = load_registry()
    if name not in reg:
        print(f"cmux: no session '{name}'", file=sys.stderr)
        sys.exit(1)
    tmux_sess = reg[name].get('tmux_session', f'cmux-{name}')
    subprocess.run(['tmux', 'detach-client', '-s', tmux_sess], check=True)
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
        # Kill just this window, leave the rest of the workspace alive
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


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

USAGE = """\
cmux — Claude Code multiplexer

Usage:
  cmux [-s workspace] <agent>               Start agent and attach (shorthand)
  cmux [-s workspace] start <agent> [-d] [--no-inject] [-- "initial prompt"]
  cmux ls                                   List agents
  cmux send <agent> <message>               Enqueue a message (max 2000 chars)
  cmux attach <agent>                       Attach terminal to agent
  cmux detach <agent>                       Detach (agent keeps running)
  cmux stop <agent>                         Stop agent
  cmux inbox <agent>                        Print and clear queued messages (--no-inject agents)

Flags:
  --no-inject   Messages are written to a file instead of injected via tmux send-keys.
                Use for coordinator sessions (e.g. lupus) where a human is actively typing
                in the pane. Read messages with: cmux inbox <agent>

Workspaces group agents into one tmux session as windows/tabs:
  cmux -s myproject start alice -d
  cmux -s myproject start bob -d
  cmux attach alice                         Opens myproject on alice's window

Keyboard shortcuts (when attached):
  Ctrl-b d                                  Detach
  Ctrl-b n / Ctrl-b p                       Next / previous agent window
  Ctrl-b [                                  Scroll mode (q to exit)
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

    _require_tmux()

    # Parse -s <workspace> before the subcommand
    workspace = None
    if len(args) >= 2 and args[0] == '-s':
        workspace = args[1]
        args = args[2:]
        if not args:
            print('cmux: -s requires a subcommand or name after the workspace', file=sys.stderr)
            sys.exit(1)

    cmd = args[0]

    # Shorthand: `cmux [-s ws] lupus` → start + attach
    if cmd not in ('start', 'ls', 'send', 'attach', 'detach', 'stop', 'inbox'):
        if cmd.startswith('-'):
            print(f'cmux: unknown option {cmd!r}', file=sys.stderr)
            print(USAGE)
            sys.exit(1)
        cmd_start(cmd, detach=False, workspace=workspace)
        return

    if cmd == 'start':
        if len(args) < 2:
            print('cmux: start requires a session name', file=sys.stderr)
            sys.exit(1)
        detach = '-d' in args or '--detach' in args
        no_inject = '--no-inject' in args
        remaining = [a for a in args[1:] if a not in ('-d', '--detach', '--no-inject')]
        name = remaining[0]
        try:
            sep = remaining.index('--')
            initial_prompt = ' '.join(remaining[sep + 1:]) or None
        except ValueError:
            initial_prompt = None
        cmd_start(name, initial_prompt=initial_prompt, detach=detach, workspace=workspace, no_inject=no_inject)

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
            print('cmux: stop requires a session name', file=sys.stderr)
            sys.exit(1)
        cmd_stop(args[1])


if __name__ == '__main__':
    main()
