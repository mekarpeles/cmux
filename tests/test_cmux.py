"""
Integration tests for cmux CLI.

Uses a fake Claude process and isolated state dirs so real sessions
and ~/.cmux are never touched. Requires tmux to be installed.
"""

import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

FAKE_CLAUDE = os.path.join(os.path.dirname(__file__), 'fake_claude.py')
CMUX = 'cmux'


def _cmux(*args, state_dir, check=True, cwd=None):
    env = os.environ.copy()
    env['CMUX_STATE_DIR'] = state_dir
    env['CMUX_CLAUDE_CMD'] = f'{sys.executable} {FAKE_CLAUDE}'
    # Disable session-detect retries — fake_claude creates no JSONL files.
    env['CMUX_SESSION_DETECT_RETRIES'] = '1'
    env['CMUX_SESSION_DETECT_INTERVAL'] = '0'
    return subprocess.run(
        [CMUX, *args],
        capture_output=True, text=True, env=env,
        check=check, cwd=cwd,
    )


def _wait_socket(path, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _rnd(n=4):
    """Return a short random hex string for unique agent names."""
    return os.urandom(n).hex()


# ------------------------------------------------------------------
# Isolated tmux server for the whole test run.
#
# Without this, every `tmux new-session` a test spawns lands on the same
# default tmux server Mek's real PAM agents (lupin, fran, ...) run on — a
# crashed test or a name collision could touch a live agent's window.
# Setting TMUX_TMPDIR before any test runs redirects tmux's socket directory
# for this process and everything it spawns (the `cmux` CLI subprocess, and
# in turn the daemon subprocess it launches, all inherit os.environ), so the
# whole suite talks to a private tmux server that touches nothing real.
# tearDownModule kills that one server — a single point of cleanup that
# doesn't depend on any individual test's teardown succeeding.
# ------------------------------------------------------------------
_TMUX_TMPDIR = None


def setUpModule():
    global _TMUX_TMPDIR
    _TMUX_TMPDIR = tempfile.mkdtemp(prefix='cmux-test-tmux-')
    os.environ['TMUX_TMPDIR'] = _TMUX_TMPDIR


def tearDownModule():
    global _TMUX_TMPDIR
    subprocess.run(['tmux', 'kill-server'], capture_output=True)
    os.environ.pop('TMUX_TMPDIR', None)
    if _TMUX_TMPDIR:
        shutil.rmtree(_TMUX_TMPDIR, ignore_errors=True)
    _TMUX_TMPDIR = None


class _CmuxBase(unittest.TestCase):
    """Base class with setUp/tearDown/helpers. Contains no test methods."""

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix='cmux-test-')
        self._started = []

    def tearDown(self):
        for name in self._started:
            _cmux('stop', name, state_dir=self.state_dir, check=False)
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def _start(self, name, *extra, workspace=None):
        # Track before starting: if `cmux start` itself raises (check=True),
        # a partially-created tmux window/daemon must still get cleaned up.
        self._started.append(name)
        args = []
        if workspace:
            args += ['-s', workspace]
        args += ['start', name, '-d', *extra]
        return _cmux(*args, state_dir=self.state_dir)


class TestCmuxIntegration(_CmuxBase):

    # ------------------------------------------------------------------

    def test_start_creates_registry_entry(self):
        self._start('t1')
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn('t1', reg)
        self.assertEqual(reg['t1']['name'], 't1')

    def test_start_creates_socket(self):
        self._start('t2')
        sock = os.path.join(self.state_dir, 't2', 't2.sock')
        ready = _wait_socket(sock, timeout=10)
        self.assertTrue(ready, "daemon socket never became ready")

    def test_daemon_survives_shadowing_cwd(self):
        """Daemon starts even when the caller's cwd shadows one of its imports.

        `python -m` puts the cwd on sys.path, so a bare `claudio/` directory in
        the cwd (no __init__.py) would otherwise win the import as a namespace
        package and kill the daemon with
        `AttributeError: module 'claudio' has no attribute 'run'`.
        """
        shadow_cwd = tempfile.mkdtemp(prefix='cmux-shadow-')
        self.addCleanup(shutil.rmtree, shadow_cwd, True)
        os.mkdir(os.path.join(shadow_cwd, 'claudio'))  # decoy namespace package

        r = _cmux('start', 't9', '-d', state_dir=self.state_dir,
                  cwd=shadow_cwd, check=False)
        self._started.append('t9')
        sock = os.path.join(self.state_dir, 't9', 't9.sock')
        ready = _wait_socket(sock, timeout=10)

        log = os.path.join(self.state_dir, 't9.daemon.log')
        err = open(log).read().strip() if os.path.exists(log) else '(no daemon log)'
        self.assertTrue(
            ready,
            f'daemon died when cwd shadowed one of its imports '
            f'(cmux start rc={r.returncode}); daemon log:\n{err}'
        )

    def test_ls_shows_agent(self):
        self._start('t3')
        r = _cmux('ls', state_dir=self.state_dir)
        self.assertIn('t3', r.stdout)

    def test_ls_shows_workspace(self):
        self._start('t4', workspace='ws-test')
        r = _cmux('ls', state_dir=self.state_dir)
        self.assertIn('t4', r.stdout)
        self.assertIn('ws-test', r.stdout)

    def test_send_queues_message(self):
        self._start('t5')
        _wait_socket(os.path.join(self.state_dir, 't5', 't5.sock'))
        r = _cmux('send', 't5', 'hello from test', state_dir=self.state_dir)
        self.assertIn('queued', r.stdout)

    def test_send_unknown_agent_exits_nonzero(self):
        r = _cmux('send', 'nobody', 'hi', state_dir=self.state_dir, check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_stop_removes_registry_entry(self):
        self._start('t6')
        _wait_socket(os.path.join(self.state_dir, 't6', 't6.sock'))
        _cmux('stop', 't6', state_dir=self.state_dir)
        self._started.remove('t6')
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertNotIn('t6', reg)

    def test_stop_removes_socket(self):
        self._start('t7')
        sock = os.path.join(self.state_dir, 't7', 't7.sock')
        _wait_socket(sock)
        _cmux('stop', 't7', state_dir=self.state_dir)
        self._started.remove('t7')
        time.sleep(0.3)
        self.assertFalse(os.path.exists(sock))

    def test_message_delivered_to_pane(self):
        """Message sent via cmux send appears in the fake Claude's tmux pane."""
        # Pre-create identity.md so onboarding messages are skipped
        home = os.path.join(self.state_dir, 't8')
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'identity.md'), 'w') as f:
            f.write('test agent')
        self._start('t8')
        _wait_socket(os.path.join(self.state_dir, 't8', 't8.sock'))
        _cmux('send', 't8', 'test-payload-xyz', state_dir=self.state_dir)
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        target = reg['t8']['tmux_target']
        # Poll instead of a fixed sleep — verified delivery (paste + retry-until-
        # submitted, see daemon.py:_submit) has variable latency, and the startup
        # onboarding message must clear the queue before this one is even popped.
        deadline = time.time() + 15
        pane = ''
        while time.time() < deadline:
            pane = subprocess.run(
                ['tmux', 'capture-pane', '-t', target, '-p'],
                capture_output=True, text=True,
            ).stdout
            if 'test-payload-xyz' in pane:
                break
            time.sleep(0.3)
        self.assertIn('test-payload-xyz', pane)

    def test_workspace_agents_share_tmux_session(self):
        self._start('w1', workspace='shared')
        self._start('w2', workspace='shared')
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertEqual(reg['w1']['tmux_session'], reg['w2']['tmux_session'])

    def test_stop_workspace_agent_leaves_others(self):
        self._start('wa', workspace='ws2')
        self._start('wb', workspace='ws2')
        _wait_socket(os.path.join(self.state_dir, 'wa', 'wa.sock'))
        _cmux('stop', 'wa', state_dir=self.state_dir)
        self._started.remove('wa')
        # wb's window should still exist
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn('wb', reg)


    def test_stop_keeps_agent_in_db(self):
        """stop removes from sessions.json but leaves the agent in agents.db."""
        # Register so cmd_start upserts to DB
        _cmux('agent', 'register', 'tdb', '--role', 'TestRole', state_dir=self.state_dir)
        self._start('tdb')
        sock = os.path.join(self.state_dir, 'tdb', 'tdb.sock')
        _wait_socket(sock)

        _cmux('stop', 'tdb', state_dir=self.state_dir)
        self._started.remove('tdb')

        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertNotIn('tdb', reg)

        # Verify via raw SQLite — no need to patch db module globals
        db_path = os.path.join(self.state_dir, 'agents.db')
        self.assertTrue(os.path.exists(db_path), 'agents.db should exist after stop')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT name, role FROM agents WHERE name = ?', ('tdb',)).fetchone()
        conn.close()
        self.assertIsNotNone(row, 'agent should still be in agents.db after stop')
        self.assertEqual(row[0], 'tdb')
        self.assertEqual(row[1], 'TestRole')


# ------------------------------------------------------------------
# Unit tests for db.py
# ------------------------------------------------------------------

import cmux_lib.db as _db_module


class TestDb(unittest.TestCase):
    """Unit tests for db.py using an isolated temp DB."""

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix='cmux-db-test-')
        self._orig_state = _db_module.STATE_DIR
        self._orig_path = _db_module.DB_PATH
        _db_module.STATE_DIR = self.state_dir
        _db_module.DB_PATH = os.path.join(self.state_dir, 'agents.db')

    def tearDown(self):
        _db_module.STATE_DIR = self._orig_state
        _db_module.DB_PATH = self._orig_path
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_register_and_get_agent(self):
        _db_module.register_agent('alice', role='Coordinator', workspace='demo')
        agent = _db_module.get_agent('alice')
        self.assertIsNotNone(agent)
        self.assertEqual(agent['name'], 'alice')
        self.assertEqual(agent['role'], 'Coordinator')
        self.assertEqual(agent['workspace'], 'demo')

    def test_get_agent_returns_none_for_unknown(self):
        result = _db_module.get_agent('nobody')
        self.assertIsNone(result)

    def test_register_upserts_on_name(self):
        _db_module.register_agent('alice', role='Original')
        _db_module.register_agent('alice', role='Updated')
        agents = _db_module.list_agents()
        self.assertEqual(len([a for a in agents if a['name'] == 'alice']), 1)
        self.assertEqual(_db_module.get_agent('alice')['role'], 'Updated')

    def test_list_agents_empty(self):
        self.assertEqual(_db_module.list_agents(), [])

    def test_list_agents_multiple(self):
        _db_module.register_agent('bob', workspace='ws1')
        _db_module.register_agent('alice', workspace='ws1')
        _db_module.register_agent('carol')
        names = [a['name'] for a in _db_module.list_agents()]
        self.assertIn('alice', names)
        self.assertIn('bob', names)
        self.assertIn('carol', names)



# ------------------------------------------------------------------
# Unit tests for cmd_check (mocked tmux)
# ------------------------------------------------------------------

import cmux_lib.cli as _cli_module


class TestCmdCheck(unittest.TestCase):
    """Test cmd_check with mocked subprocess so no tmux needed."""

    def _fake_reg(self, state_dir, names):
        reg = {}
        for name in names:
            reg[name] = {
                'name': name,
                'workspace': None,
                'tmux_session': f'cmux-{name}',
                'tmux_window': name,
                'tmux_target': f'cmux-{name}:{name}',
                'socket': os.path.join(state_dir, name, f'{name}.sock'),
            }
        return reg

    def _run_check(self, pane_text, names=('alice',)):
        state_dir = tempfile.mkdtemp(prefix='cmux-check-test-')
        try:
            reg = self._fake_reg(state_dir, names)

            def mock_run(cmd, **kwargs):
                result = MagicMock()
                result.returncode = 0
                if 'list-windows' in cmd:
                    result.stdout = '\n'.join(names) + '\n'
                elif 'capture-pane' in cmd:
                    result.stdout = pane_text
                else:
                    result.stdout = ''
                return result

            with patch.object(_cli_module, 'load_registry', return_value=reg), \
                 patch.object(_cli_module, 'session_alive', return_value=True), \
                 patch('cmux_lib.cli.subprocess.run', side_effect=mock_run), \
                 patch('sys.stdout', new_callable=io.StringIO) as mock_out:
                _cli_module.cmd_check()
                return mock_out.getvalue()
        finally:
            shutil.rmtree(state_dir, ignore_errors=True)

    def test_ok_agent_reported_ok(self):
        output = self._run_check('Normal pane output, nothing suspicious here.')
        self.assertIn('[OK]', output)
        self.assertNotIn('[STUCK]', output)

    def test_stuck_agent_detected_yes_proceed(self):
        output = self._run_check('Do you want to allow this?\nYes, proceed\nAlways allow')
        self.assertIn('[STUCK]', output)

    def test_stuck_agent_detected_always_allow(self):
        output = self._run_check('Always allow in future sessions')
        self.assertIn('[STUCK]', output)

    def test_stuck_agent_shows_tmux_target(self):
        output = self._run_check('Yes, proceed')
        self.assertIn('cmux-alice:alice', output)

    def test_no_agents_running(self):
        with patch.object(_cli_module, 'load_registry', return_value={}), \
             patch('sys.stdout', new_callable=io.StringIO) as mock_out:
            _cli_module.cmd_check()
            output = mock_out.getvalue()
        self.assertIn('No agents running', output)

    def test_stuck_agent_detected_folder_trust_dialog(self):
        # Verbatim pane capture from Claude Code's folder-trust dialog. Before
        # the fix, none of _PERM_PATTERNS matched this text, so `cmux check`
        # reported [OK] for an agent stalled waiting on this prompt.
        pane_text = (
            ' Accessing workspace:\n\n'
            ' /Users/mek/.cmux/alice\n\n'
            ' Quick safety check: Is this a project you created or one you trust? '
            '(Like your\n own code, a well-known open source project, or work from '
            'your team). If not,\n take a moment to review what\'s in this folder first.\n\n'
            ' Claude Code\'ll be able to read, edit, and execute files here.\n\n'
            ' Security guide\n\n'
            ' ❯ 1. Yes, I trust this folder\n'
            '   2. No, exit\n\n'
            ' Enter to confirm · Esc to cancel\n'
        )
        output = self._run_check(pane_text)
        self.assertIn('[STUCK]', output)


# ------------------------------------------------------------------
# Unit tests for `cmux upgrade --testing [branch]`
# ------------------------------------------------------------------

class TestUpgradeTesting(unittest.TestCase):
    """cmux upgrade --testing installs a non-main branch on one machine
    without changing what a plain `cmux upgrade` installs elsewhere."""

    def test_upgrade_plain_defaults_to_main(self):
        with patch.object(_cli_module, 'cmd_upgrade') as mock_upgrade, \
             patch.object(sys, 'argv', ['cmux', 'upgrade']):
            _cli_module.main()
        mock_upgrade.assert_called_once_with(branch=None)

    def test_upgrade_testing_with_no_branch_defaults_to_testing(self):
        with patch.object(_cli_module, 'cmd_upgrade') as mock_upgrade, \
             patch.object(sys, 'argv', ['cmux', 'upgrade', '--testing']):
            _cli_module.main()
        mock_upgrade.assert_called_once_with(branch='testing')

    def test_upgrade_testing_with_explicit_branch(self):
        with patch.object(_cli_module, 'cmd_upgrade') as mock_upgrade, \
             patch.object(sys, 'argv', ['cmux', 'upgrade', '--testing', 'fix/folder-trust-dialog-check']):
            _cli_module.main()
        mock_upgrade.assert_called_once_with(branch='fix/folder-trust-dialog-check')

    def _first_clone_call(self, branch):
        """Run cmd_upgrade(branch) with a mocked, immediately-failing clone,
        and return the git command it invoked."""
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(list(cmd))
            result = MagicMock()
            result.returncode = 1  # stop right after the clone call
            result.stderr = 'stop here'
            return result

        with patch.object(_cli_module.subprocess, 'run', side_effect=mock_run), \
             self.assertRaises(SystemExit):
            _cli_module.cmd_upgrade(branch=branch)
        return calls[0]

    def test_upgrade_clones_main_when_no_branch_given(self):
        clone_call = self._first_clone_call(branch=None)
        self.assertNotIn('--branch', clone_call, 'plain upgrade must not pin a branch')

    def test_upgrade_clones_given_branch(self):
        clone_call = self._first_clone_call(branch='testing')
        self.assertIn('--branch', clone_call)
        self.assertEqual(clone_call[clone_call.index('--branch') + 1], 'testing')


# ------------------------------------------------------------------
# Unit tests for sanitize() in daemon.py
# ------------------------------------------------------------------

from cmux_lib.daemon import sanitize


class TestSanitize(unittest.TestCase):

    def test_newline_becomes_space(self):
        self.assertEqual(sanitize('hello\nworld'), 'hello world')

    def test_tab_becomes_space(self):
        self.assertEqual(sanitize('hello\tworld'), 'hello world')

    def test_carriage_return_becomes_space(self):
        self.assertEqual(sanitize('hello\rworld'), 'hello world')

    def test_multiple_whitespace_collapsed(self):
        self.assertEqual(sanitize('a   \n\t  b'), 'a b')

    def test_control_chars_stripped(self):
        # 0x01 (SOH), 0x07 (BEL), 0x7f (DEL)
        self.assertEqual(sanitize('a\x01b\x07c\x7fd'), 'abcd')

    def test_leading_trailing_whitespace_stripped(self):
        self.assertEqual(sanitize('  hello  '), 'hello')

    def test_empty_string_safe(self):
        self.assertEqual(sanitize(''), '')

    def test_plain_text_unchanged(self):
        self.assertEqual(sanitize('hello world'), 'hello world')

    def test_multiline_message_arrives_flat(self):
        # The key behavior callers must know: newlines are NOT preserved
        result = sanitize('line one\nline two\nline three')
        self.assertEqual(result, 'line one line two line three')
        self.assertNotIn('\n', result)


# ------------------------------------------------------------------
# Integration tests for --no-inject / inbox path
# ------------------------------------------------------------------

class TestNoInject(_CmuxBase):
    """Test file-based message delivery (--no-inject mode). No tmux pane injection."""

    def test_no_inject_writes_to_inbox_file(self):
        """Messages sent to a --no-inject agent land in inbox.jsonl, not pane."""
        name = f'ni{_rnd()}'
        # Pre-create identity.md so onboarding messages are skipped
        home = os.path.join(self.state_dir, name)
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'identity.md'), 'w') as f:
            f.write('test agent')
        self._start(name, '--no-inject')
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))

        payload = f'hello-{_rnd()}'
        _cmux('send', name, payload, state_dir=self.state_dir)
        time.sleep(2.0)  # daemon appends on next poll (~200ms), allow headroom

        inbox = os.path.join(self.state_dir, f'{name}.inbox.jsonl')
        self.assertTrue(os.path.exists(inbox), 'inbox.jsonl should exist')
        lines = open(inbox).readlines()
        self.assertTrue(any(payload in line for line in lines))

    def test_inbox_cmd_prints_and_clears(self):
        """cmux inbox prints queued messages and clears the file."""
        name = f'ni{_rnd()}'
        self._start(name, '--no-inject')
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))

        _cmux('send', name, 'msg-one', state_dir=self.state_dir)
        _cmux('send', name, 'msg-two', state_dir=self.state_dir)

        # Poll until both messages appear in the inbox (claudio delivers one
        # message per idle-check cycle; allow up to 10 seconds for all cycles).
        inbox = os.path.join(self.state_dir, f'{name}.inbox.jsonl')
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                content = open(inbox).read()
                if 'msg-one' in content and 'msg-two' in content:
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.2)

        r = _cmux('inbox', name, state_dir=self.state_dir)
        self.assertIn('msg-one', r.stdout)
        self.assertIn('msg-two', r.stdout)

        self.assertEqual(open(inbox).read(), '')

    def test_no_inject_message_not_in_pane(self):
        """--no-inject delivery does not inject into the tmux pane."""
        name = f'ni{_rnd()}'
        payload = f'pane-absence-{_rnd()}'
        self._start(name, '--no-inject')
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))

        _cmux('send', name, payload, state_dir=self.state_dir)
        time.sleep(1.5)

        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        target = reg[name]['tmux_target']
        pane = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p'],
            capture_output=True, text=True,
        ).stdout
        self.assertNotIn(payload, pane)


# ------------------------------------------------------------------
# Integration test for cmux detach
# ------------------------------------------------------------------

class TestDetach(_CmuxBase):

    def test_detach_exits_zero_no_client(self):
        """cmux detach exits 0 even when no client is attached (no-op)."""
        name = f'det{_rnd()}'
        self._start(name)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        r = _cmux('detach', name, state_dir=self.state_dir)
        self.assertEqual(r.returncode, 0)
        self.assertIn('detached', r.stdout)

    def test_detach_unknown_agent_exits_nonzero(self):
        r = _cmux('detach', 'nobody', state_dir=self.state_dir, check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_detach_leaves_session_alive(self):
        """After detach, the agent is still in sessions.json."""
        name = f'det{_rnd()}'
        self._start(name)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        _cmux('detach', name, state_dir=self.state_dir)
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn(name, reg)


# ------------------------------------------------------------------
# Integration test for workspace restart (cmux -s workspace)
# ------------------------------------------------------------------

class TestWorkspaceRestart(_CmuxBase):

    def test_workspace_restart_starts_stopped_agents(self):
        """cmux -s ws with no subcommand restarts registered stopped agents."""
        ws = f'rws{_rnd()}'
        a1, a2 = f'wr{_rnd()}', f'wr{_rnd()}'
        _cmux('agent', 'register', a1, '--workspace', ws, state_dir=self.state_dir)
        _cmux('agent', 'register', a2, '--workspace', ws, state_dir=self.state_dir)

        self._start(a1, workspace=ws)
        self._start(a2, workspace=ws)
        _wait_socket(os.path.join(self.state_dir, a1, f'{a1}.sock'))
        _wait_socket(os.path.join(self.state_dir, a2, f'{a2}.sock'))

        _cmux('stop', a1, state_dir=self.state_dir)
        _cmux('stop', a2, state_dir=self.state_dir)
        self._started.remove(a1)
        self._started.remove(a2)

        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertNotIn(a1, reg)
        self.assertNotIn(a2, reg)

        env = os.environ.copy()
        env['CMUX_STATE_DIR'] = self.state_dir
        env['CMUX_CLAUDE_CMD'] = f'{sys.executable} {FAKE_CLAUDE}'
        subprocess.run([CMUX, '-s', ws], env=env, capture_output=True, check=True)
        self._started += [a1, a2]

        _wait_socket(os.path.join(self.state_dir, a1, f'{a1}.sock'))
        _wait_socket(os.path.join(self.state_dir, a2, f'{a2}.sock'))

        reg2 = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn(a1, reg2)
        self.assertIn(a2, reg2)


# ------------------------------------------------------------------
# Self-heal: attach/send recreate a dead tmux session (e.g. after a reboot
# wipes the tmux server but sessions.json/agents.db survive on disk).
# ------------------------------------------------------------------

class TestSelfHeal(_CmuxBase):

    def _kill_tmux_session(self, name):
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        tmux_sess = reg[name]['tmux_session']
        subprocess.run(['tmux', 'kill-session', '-t', tmux_sess], capture_output=True)
        self.assertNotEqual(
            subprocess.run(['tmux', 'has-session', '-t', tmux_sess], capture_output=True).returncode,
            0,
            'tmux session should be gone before self-heal is exercised',
        )
        return tmux_sess

    def test_send_recreates_dead_tmux_session(self):
        """cmux send transparently recreates the tmux session if it's gone."""
        name = f'sh{_rnd()}'
        self._start(name)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        tmux_sess = self._kill_tmux_session(name)

        r = _cmux('send', name, 'hello after reboot', state_dir=self.state_dir, check=False)
        self.assertEqual(r.returncode, 0, f'stdout={r.stdout!r} stderr={r.stderr!r}')
        self.assertIn('queued', r.stdout)

        r2 = subprocess.run(['tmux', 'has-session', '-t', tmux_sess], capture_output=True)
        self.assertEqual(r2.returncode, 0, 'tmux session should have been recreated')

    def test_attach_recreates_dead_tmux_session(self):
        """cmux attach recreates the tmux session before exec'ing into tmux attach."""
        name = f'ah{_rnd()}'
        self._start(name)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        tmux_sess = self._kill_tmux_session(name)

        # The final `tmux attach` has no controlling terminal in this test harness
        # and may itself fail/exit nonzero — we only care that the session was
        # recreated before that exec happened.
        _cmux('attach', name, state_dir=self.state_dir, check=False)

        r = subprocess.run(['tmux', 'has-session', '-t', tmux_sess], capture_output=True)
        self.assertEqual(r.returncode, 0, 'tmux session should have been recreated by cmd_attach')


# ------------------------------------------------------------------
# Integration test for cmux agent import-sessions
# ------------------------------------------------------------------

class TestImportSessions(_CmuxBase):

    def test_import_sessions_registers_from_sessions_json(self):
        """import-sessions reads sessions.json and populates agents.db."""
        # Write a fake sessions.json with a known agent
        fake_reg = {
            'odie': {
                'name': 'odie',
                'workspace': 'ol-loop',
                'tmux_session': 'ol-loop',
                'tmux_window': 'odie',
                'tmux_target': 'ol-loop:odie',
                'socket': os.path.join(self.state_dir, 'odie', 'odie.sock'),
                'daemon_pid': 99999,
                'started': '2026-01-01T00:00:00Z',
                'initial_prompt': None,
                'no_inject': False,
            }
        }
        reg_path = os.path.join(self.state_dir, 'sessions.json')
        with open(reg_path, 'w') as f:
            json.dump(fake_reg, f)

        _cmux('agent', 'import-sessions', state_dir=self.state_dir)

        # Check agents.db via raw SQLite
        db_path = os.path.join(self.state_dir, 'agents.db')
        self.assertTrue(os.path.exists(db_path))
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT name, workspace, role FROM agents WHERE name = ?',
                           ('odie',)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'odie')
        self.assertEqual(row[1], 'ol-loop')
        # KNOWN_AGENTS has role for odie — import-sessions merges it
        self.assertIsNotNone(row[2])

    def test_import_sessions_registers_known_agents_not_in_sessions(self):
        """import-sessions also registers KNOWN_AGENTS entries absent from sessions.json."""
        # Empty sessions.json
        reg_path = os.path.join(self.state_dir, 'sessions.json')
        with open(reg_path, 'w') as f:
            json.dump({}, f)

        _cmux('agent', 'import-sessions', state_dir=self.state_dir)

        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        names = [r[0] for r in conn.execute('SELECT name FROM agents').fetchall()]
        conn.close()
        # All KNOWN_AGENTS should be registered
        self.assertIn('lupin', names)
        self.assertIn('fran', names)
        self.assertIn('pierre', names)


# ------------------------------------------------------------------
# Unit tests for _unblock_watcher in daemon.py
# ------------------------------------------------------------------

class TestUnblockWatcher(unittest.TestCase):
    """Unit tests for the _unblock_watcher background thread function."""

    def test_watcher_sends_escape_on_permission_pattern(self):
        """When pane contains a permission pattern, Escape is sent."""
        from cmux_lib.daemon import _unblock_watcher
        import threading

        escape_sent = threading.Event()

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_list = list(cmd)
            if 'capture-pane' in cmd_list:
                result.stdout = 'Yes, proceed\nDo you want to allow this?'
            elif 'send-keys' in cmd_list and 'Escape' in cmd_list:
                escape_sent.set()
            return result

        target = 'cmux-test:test'
        stop_event = threading.Event()
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.05, stop_event), daemon=True)
            t.start()
            try:
                self.assertTrue(escape_sent.wait(timeout=5.0), 'Escape key should have been sent')
            finally:
                stop_event.set()
                t.join(timeout=2.0)

    def test_watcher_injects_notification_after_escape(self):
        """After sending Escape, the internal notification message is injected."""
        from cmux_lib.daemon import _unblock_watcher
        import threading

        notification_sent = threading.Event()

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_list = list(cmd)
            if 'capture-pane' in cmd_list:
                result.stdout = 'needs permission'
            elif 'load-buffer' in cmd_list and b'[claudio@noreply]' in kwargs.get('input', b''):
                # notification now travels via bracketed paste (verified delivery)
                notification_sent.set()
            elif 'send-keys' in cmd_list and any('[claudio@noreply]' in a for a in cmd_list):
                notification_sent.set()  # legacy fallback path
            return result

        target = 'cmux-notify:test'
        stop_event = threading.Event()
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run), \
             patch('time.sleep', return_value=None):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.01, stop_event), daemon=True)
            t.start()
            try:
                self.assertTrue(notification_sent.wait(timeout=5.0), 'notification should have been injected')
            finally:
                stop_event.set()
                t.join(timeout=2.0)

    def test_watcher_no_action_on_clean_pane(self):
        """No send-keys calls when pane has no permission patterns."""
        from cmux_lib.daemon import _unblock_watcher
        import threading

        send_key_calls = []
        target = f'cmux-clean-{_rnd()}:test'

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_list = list(cmd)
            result.stdout = 'Normal Claude output, nothing to see here.'
            # Only count send-keys aimed at our target — filters out lingering threads
            # from other tests that share the same module-level subprocess.run patch.
            if 'send-keys' in cmd_list and target in cmd_list:
                send_key_calls.append(cmd_list)
            return result

        stop_event = threading.Event()
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.05, stop_event), daemon=True)
            t.start()
            time.sleep(0.3)  # allow ~6 poll iterations
            stop_event.set()
            t.join(timeout=2.0)

        self.assertEqual(len(send_key_calls), 0, 'no send-keys calls on a clean pane')

    def test_watcher_trusts_folder_on_trust_dialog(self):
        """Folder-trust dialog: watcher sends '1' + Enter — accepts the default
        "Yes, I trust this folder" option instead of Escape — then verifies
        the dialog actually cleared before notifying."""
        from cmux_lib.daemon import _unblock_watcher
        import threading

        keys_sent = []
        notify_sent = threading.Event()
        accepted = threading.Event()
        # Randomized target, kept even though each watcher thread is now
        # stop_event-joined at teardown — cheap insurance against any other
        # in-process caller of the same tmux target.
        target = f'cmux-trust-{_rnd()}:test'

        dialog_text = (
            'Quick safety check: Is this a project you created or one you trust?\n'
            '❯ 1. Yes, I trust this folder\n  2. No, exit'
        )
        idle_text = '❯\xa0Try "write a test for <filepath>"'

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ''
            cmd_list = list(cmd)
            if 'load-buffer' in cmd_list and b'[claudio@noreply]' in kwargs.get('input', b''):
                # notification now travels via bracketed paste (verified delivery)
                notify_sent.set()
                return result
            if target not in cmd_list:
                return result
            if 'capture-pane' in cmd_list:
                # Pane clears to the normal idle prompt only after '1'+Enter
                # was actually sent — simulates the dialog responding to input.
                result.stdout = idle_text if accepted.is_set() else dialog_text
            elif 'send-keys' in cmd_list:
                keys_sent.append(cmd_list)
                if cmd_list[-2:] == ['1', 'Enter']:
                    accepted.set()
                if any('[claudio@noreply]' in a for a in cmd_list):
                    notify_sent.set()
            return result

        stop_event = threading.Event()
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run), \
             patch('time.sleep', return_value=None):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.01, stop_event), daemon=True)
            t.start()
            try:
                self.assertTrue(notify_sent.wait(timeout=5.0),
                                 'watcher should confirm the dialog cleared and notify')
            finally:
                stop_event.set()
                t.join(timeout=2.0)

        first_call = keys_sent[0]
        self.assertIn('1', first_call, "watcher must send '1' to accept the trust prompt")
        self.assertNotIn('Escape', first_call, 'Escape hits "Esc to cancel" and quits Claude')

    def test_watcher_no_notify_if_trust_dialog_persists(self):
        """If accepting the dialog doesn't clear it (unexpected variant, e.g.
        the default option isn't "Yes" in some future Claude Code version),
        the watcher must not claim success — no "dismissed" notification."""
        from cmux_lib.daemon import _unblock_watcher
        import threading

        keys_sent = []
        target = f'cmux-trust-stuck-{_rnd()}:test'

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ''
            cmd_list = list(cmd)
            if target not in cmd_list:
                return result
            if 'capture-pane' in cmd_list:
                # Dialog never clears, no matter what's sent.
                result.stdout = 'Quick safety check: Is this a project you created or one you trust?'
            elif 'send-keys' in cmd_list:
                keys_sent.append(cmd_list)
            return result

        stop_event = threading.Event()
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run), \
             patch('time.sleep', return_value=None):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.01, stop_event), daemon=True)
            t.start()
            # Event.wait's timeout isn't backed by the patched time.sleep, so
            # this is a real wall-clock pause letting several poll+verify
            # cycles run before we inspect what the watcher did.
            threading.Event().wait(timeout=0.3)
            stop_event.set()
            t.join(timeout=2.0)

        accept_calls = [c for c in keys_sent if c[-2:] == ['1', 'Enter']]
        notify_calls = [c for c in keys_sent if any('[claudio@noreply]' in a for a in c)]
        self.assertTrue(accept_calls, 'watcher should still attempt to accept the dialog')
        self.assertEqual(notify_calls, [], 'must not notify "dismissed" while the dialog is still showing')

    def test_watcher_never_sends_escape_on_trust_dialog(self):
        """Regression guard: Escape on this dialog quits Claude and kills the
        tmux window (verified empirically against the real dialog) — the
        watcher must never send it for trust-dialog text."""
        from cmux_lib.daemon import _unblock_watcher
        import threading

        escape_sent = threading.Event()
        target = f'cmux-trust-esc-{_rnd()}:test'

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ''
            cmd_list = list(cmd)
            if target not in cmd_list:
                return result
            if 'capture-pane' in cmd_list:
                result.stdout = 'Quick safety check: Is this a project you created or one you trust?'
            elif 'send-keys' in cmd_list and 'Escape' in cmd_list:
                escape_sent.set()
            return result

        stop_event = threading.Event()
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.05, stop_event), daemon=True)
            t.start()
            try:
                self.assertFalse(escape_sent.wait(timeout=0.5), 'Escape must never be sent on the trust dialog')
            finally:
                stop_event.set()
                t.join(timeout=2.0)


class TestIsIdleOnTrustDialog(unittest.TestCase):
    """Regression test: is_idle() must not treat the trust dialog as a ready prompt.

    The dialog's highlighted option is prefixed with the same '❯' glyph
    is_idle() looks for on Claude's normal input prompt. It happens to still
    return False today because of the existing "text after ❯ isn't just the
    ghost hint" check — this test locks that behavior in so a future tweak to
    that heuristic can't silently regress it.
    """

    def test_is_idle_false_on_trust_dialog(self):
        from cmux_lib.daemon import make_is_idle

        pane_text = (
            ' Quick safety check: Is this a project you created or one you trust?\n\n'
            ' ❯ 1. Yes, I trust this folder\n'
            '   2. No, exit\n\n'
            ' Enter to confirm · Esc to cancel\n'
        )

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_list = list(cmd)
            if 'capture-pane' in cmd_list:
                result.stdout = pane_text
            elif 'display-message' in cmd_list:
                result.stdout = '1'  # cursor sits right after the '❯ ' marker
            return result

        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run):
            is_idle = make_is_idle('cmux-trust-idle:test')
            self.assertFalse(is_idle(), 'trust dialog must never be reported as idle/ready')


# ------------------------------------------------------------------
# Integration tests for --unblock flag
# ------------------------------------------------------------------

class TestUpDownRm(_CmuxBase):
    """Tests for cmux up / down (aliases) and cmux rm."""

    def test_up_is_alias_for_start(self):
        name = f'up{_rnd()}'
        _cmux('up', name, '-d', state_dir=self.state_dir)
        self._started.append(name)
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn(name, reg)

    def test_up_creates_agent_home_dir(self):
        name = f'up{_rnd()}'
        _cmux('up', name, '-d', state_dir=self.state_dir)
        self._started.append(name)
        home = os.path.join(self.state_dir, name)
        self.assertTrue(os.path.isdir(home), 'agent home dir should exist after up')

    def test_start_still_works_as_alias(self):
        name = f'st{_rnd()}'
        _cmux('start', name, '-d', state_dir=self.state_dir)
        self._started.append(name)
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn(name, reg)

    def test_up_registers_agent_in_db(self):
        """cmux up auto-registers even without prior agent register."""
        name = f'up{_rnd()}'
        _cmux('up', name, '-d', state_dir=self.state_dir)
        self._started.append(name)
        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT name FROM agents WHERE name = ?', (name,)).fetchone()
        conn.close()
        self.assertIsNotNone(row, 'up should auto-register agent in agents.db')

    def test_down_is_alias_for_stop(self):
        name = f'dn{_rnd()}'
        _cmux('up', name, '-d', state_dir=self.state_dir)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        _cmux('down', name, state_dir=self.state_dir)
        self._started.remove(name) if name in self._started else None
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertNotIn(name, reg)

    def test_rm_archives_home(self):
        name = f'rm{_rnd()}'
        _cmux('up', name, '-d', state_dir=self.state_dir)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        _cmux('down', name, state_dir=self.state_dir)
        self._started.remove(name) if name in self._started else None
        home = os.path.join(self.state_dir, name)
        self.assertTrue(os.path.isdir(home))

        r = _cmux('rm', name, state_dir=self.state_dir)
        # Original home dir must be gone (moved to archive)
        self.assertFalse(os.path.isdir(home), 'home dir should be moved to archive on rm')
        # Archive dir must contain an entry for this agent
        archive_dir = os.path.join(self.state_dir, 'archive')
        archived = [d for d in os.listdir(archive_dir) if d.endswith(f'_{name}')]
        self.assertTrue(archived, 'archived entry should exist under archive/')
        # DB entry must be gone
        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT name FROM agents WHERE name = ?', (name,)).fetchone()
        conn.close()
        self.assertIsNone(row, 'agent should be de-registered from agents.db after rm')
        self.assertIn('archived', r.stdout)

    def test_rm_refuses_running_agent(self):
        name = f'rm{_rnd()}'
        _cmux('up', name, '-d', state_dir=self.state_dir)
        self._started.append(name)
        r = _cmux('rm', name, state_dir=self.state_dir, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('down', r.stderr)


import cmux_lib.cli as _cli_module_for_session

# Disable retry waits in all tests — fake_claude creates no real JSONL files,
# so _store_session_id would otherwise sleep 4x3s per agent start.
_cli_module_for_session._SESSION_DETECT_RETRIES = 1
_cli_module_for_session._SESSION_DETECT_INTERVAL = 0


class TestSessionContinuity(_CmuxBase):
    """Tests for home dir scaffolding, session ID tracking, workflow/identity injection."""

    def test_up_injects_initial_prompt_as_message(self):
        """cmux up writes initial_prompt to initial-prompt.md and @-references it in the startup message."""
        name = f'sc{_rnd()}'
        _cmux('agent', 'register', name, '--no-inject', '--', 'You are a test agent.',
              state_dir=self.state_dir)
        self._start(name, '--no-inject')
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        time.sleep(2.5)  # allow startup message delivery

        # initial-prompt.md should be written to the agent's home dir
        home = os.path.join(self.state_dir, name)
        prompt_file = os.path.join(home, 'initial-prompt.md')
        self.assertTrue(os.path.exists(prompt_file), 'initial-prompt.md should exist in home dir')
        self.assertIn('You are a test agent', open(prompt_file).read())

        # The startup inbox message should include the prompt content inline
        inbox = os.path.join(self.state_dir, f'{name}.inbox.jsonl')
        self.assertTrue(os.path.exists(inbox), 'inbox should exist')
        content = open(inbox).read()
        self.assertIn('You are a test agent', content,
                      'startup message should inline the initial prompt content')

    def test_up_does_not_overwrite_existing_identity(self):
        """Existing identity.md is never overwritten by initial_prompt."""
        name = f'sc{_rnd()}'
        home = os.path.join(self.state_dir, name)
        os.makedirs(home, exist_ok=True)
        identity_path = os.path.join(home, 'identity.md')
        with open(identity_path, 'w') as f:
            f.write('Custom identity written by agent.')
        _cmux('agent', 'register', name, '--', 'Overwrite attempt.', state_dir=self.state_dir)
        self._start(name)
        content = open(identity_path).read()
        self.assertIn('Custom identity', content)
        self.assertNotIn('Overwrite attempt', content)

    def test_restart_succeeds_after_stop(self):
        """cmux up after down starts the agent again."""
        name = f'sc{_rnd()}'
        self._start(name)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        _cmux('down', name, state_dir=self.state_dir)
        self._started.remove(name) if name in self._started else None
        _cmux('up', name, '-d', state_dir=self.state_dir)
        self._started.append(name)
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn(name, reg)

    def test_session_id_not_written_when_no_new_sessions(self):
        """In the fake_claude test env no real Claude sessions are created,
        so last-session-id is not written — _store_session_id is a safe no-op."""
        name = f'sc{_rnd()}'
        self._start(name)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        sid_path = os.path.join(self.state_dir, name, 'last-session-id')
        # Either file doesn't exist (no sessions detected) or contains a valid UUID-like string
        if os.path.exists(sid_path):
            sid = open(sid_path).read().strip()
            self.assertTrue(len(sid) > 0)

    def test_snapshot_detect_new_session_file(self):
        """_store_session_id detects a new .jsonl file added after snapshot."""
        import tempfile, uuid as _uuid
        home = tempfile.mkdtemp(prefix='cmux-sess-test-')
        projects = tempfile.mkdtemp(prefix='cmux-claude-projects-')
        proj_dir = os.path.join(projects, '-Users-mek-Projects-pm')
        os.makedirs(proj_dir)

        # Patch the projects dir
        orig = _cli_module_for_session._snapshot_claude_sessions
        def _patched_snapshot(project_dir=None):
            snap = {}
            for f in os.listdir(proj_dir):
                if f.endswith('.jsonl'):
                    fp = os.path.join(proj_dir, f)
                    snap[fp] = os.path.getmtime(fp)
            return snap

        _cli_module_for_session._snapshot_claude_sessions = _patched_snapshot
        try:
            pre = _patched_snapshot()
            # Simulate Claude creating a new session file
            new_uuid = str(_uuid.uuid4())
            new_file = os.path.join(proj_dir, f'{new_uuid}.jsonl')
            with open(new_file, 'w') as f:
                f.write('{}')
            _cli_module_for_session._store_session_id(home, pre)
            sid = open(os.path.join(home, 'last-session-id')).read().strip()
            self.assertEqual(sid, new_uuid)
        finally:
            _cli_module_for_session._snapshot_claude_sessions = orig
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(projects, ignore_errors=True)

    def test_cwd_to_claude_project_dir(self):
        """_cwd_to_claude_project_dir replaces / with - in the path."""
        f = _cli_module_for_session._cwd_to_claude_project_dir
        result = f('/Users/mek/.cmux/alice')
        self.assertTrue(result.endswith('-Users-mek-.cmux-alice'))
        self.assertIn('.claude/projects', result)

    def test_snapshot_scoped_to_project_dir(self):
        """_snapshot_claude_sessions with project_dir only returns files in that dir."""
        import tempfile as _tf, uuid as _uuid
        projects = _tf.mkdtemp(prefix='cmux-projects-')
        dir_a = os.path.join(projects, 'dir-a')
        dir_b = os.path.join(projects, 'dir-b')
        os.makedirs(dir_a)
        os.makedirs(dir_b)
        uuid_a = str(_uuid.uuid4())
        uuid_b = str(_uuid.uuid4())
        open(os.path.join(dir_a, f'{uuid_a}.jsonl'), 'w').close()
        open(os.path.join(dir_b, f'{uuid_b}.jsonl'), 'w').close()
        try:
            snap = _cli_module_for_session._snapshot_claude_sessions(project_dir=dir_a)
            self.assertEqual(len(snap), 1)
            self.assertIn(uuid_a, list(snap.keys())[0])
            self.assertNotIn(uuid_b, ''.join(snap.keys()))
        finally:
            shutil.rmtree(projects, ignore_errors=True)

    def test_claude_session_exists_true_when_jsonl_present(self):
        """_claude_session_exists returns True when the JSONL file exists anywhere."""
        import tempfile as _tf, uuid as _uuid
        projects = _tf.mkdtemp(prefix='cmux-proj-')
        proj_dir = os.path.join(projects, 'some-project')
        os.makedirs(proj_dir)
        sid = str(_uuid.uuid4())
        open(os.path.join(proj_dir, f'{sid}.jsonl'), 'w').close()
        orig = _cli_module_for_session._claude_session_exists
        # Patch to use our temp dir
        def _patched(session_id):
            for d in os.listdir(projects):
                if os.path.exists(os.path.join(projects, d, f'{session_id}.jsonl')):
                    return True
            return False
        _cli_module_for_session._claude_session_exists = _patched
        try:
            self.assertTrue(_patched(sid))
            self.assertFalse(_patched(str(_uuid.uuid4())))
        finally:
            _cli_module_for_session._claude_session_exists = orig
            shutil.rmtree(projects, ignore_errors=True)

    def test_stale_session_id_cleared_on_start(self):
        """A last-session-id that no longer has a JSONL on disk is cleared before start.
        Prevents claude from receiving --resume <missing-id> and exiting immediately."""
        import uuid as _uuid
        from unittest.mock import patch as _patch
        name = f'stale{_rnd()}'
        home = os.path.join(self.state_dir, name)
        os.makedirs(home)
        stale_id = str(_uuid.uuid4())
        with open(os.path.join(home, 'last-session-id'), 'w') as f:
            f.write(stale_id)
        # Patch _claude_session_exists to return False (session gone)
        with _patch('cmux_lib.cli._claude_session_exists', return_value=False):
            self._start(name, '-d')
            _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        # last-session-id should be cleared
        sid_path = os.path.join(self.state_dir, name, 'last-session-id')
        if os.path.exists(sid_path):
            self.assertNotEqual(open(sid_path).read().strip(), stale_id,
                                'stale last-session-id should have been cleared')

    def test_startup_context_wakeup_when_identity_exists(self):
        """_inject_startup_context sends a one-line wakeup when identity.md exists."""
        from unittest.mock import patch as _patch
        home = tempfile.mkdtemp(prefix='cmux-id-test-')
        name = os.path.basename(home)
        identity_path = os.path.join(home, 'identity.md')
        try:
            with open(identity_path, 'w') as f:
                f.write('I am an agent.')
            with _patch('cmux_lib.cli.cmd_send') as mock_send:
                _cli_module_for_session._inject_startup_context(name, home)
            mock_send.assert_called_once()
            sent_msg = mock_send.call_args[0][1]
            self.assertIn(name, sent_msg)
            self.assertIn('Resuming', sent_msg)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_startup_context_onboarding_when_no_identity(self):
        """_inject_startup_context sends one message with session info + @ONBOARDING + @IDENTITY_GUIDE when identity.md is absent."""
        from unittest.mock import patch as _patch
        home = tempfile.mkdtemp(prefix='cmux-id-test-')
        name = os.path.basename(home)
        try:
            with _patch('cmux_lib.cli.cmd_send') as mock_send:
                _cli_module_for_session._inject_startup_context(name, home)
            self.assertEqual(mock_send.call_count, 1)
            msg = mock_send.call_args[0][1]
            self.assertIn(name, msg)
            self.assertIn('ONBOARDING.md', msg)
            self.assertIn('IDENTITY_GUIDE.md', msg)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_workflow_file_injected_at_startup(self):
        """Workflow file contents are sent at startup when workflow_path is registered."""
        import tempfile
        name = f'sc{_rnd()}'
        wf_file = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
        wf_file.write('Step 1: do the thing.\nStep 2: verify.')
        wf_file.close()
        try:
            _cmux('agent', 'register', name, '--workflow', wf_file.name, state_dir=self.state_dir)
            self._start(name)
            _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
            # Verify workflow path is in the DB
            db_path = os.path.join(self.state_dir, 'agents.db')
            conn = sqlite3.connect(db_path)
            row = conn.execute('SELECT workflow_path FROM agents WHERE name = ?', (name,)).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], wf_file.name)
        finally:
            os.unlink(wf_file.name)


class TestEphemeralRun(unittest.TestCase):
    """Tests for cmd_run (ephemeral sessions) and cmd_wizard (which uses cmd_run)."""

    def test_run_writes_prompt_as_claude_md(self):
        """cmd_run writes the prompt as CLAUDE.md in the temp dir."""
        from unittest.mock import patch as _patch
        captured = {}
        tmp_path = {}
        def fake_chdir(path):
            tmp_path['dir'] = path  # record temp dir without actually changing CWD
        def fake_run(args):
            d = tmp_path.get('dir', '')
            claude_md = os.path.join(d, 'CLAUDE.md')
            if d and os.path.exists(claude_md):
                captured['content'] = open(claude_md).read()
        with _patch('subprocess.run', side_effect=fake_run), \
             _patch('os.chdir', side_effect=fake_chdir):
            _cli_module_for_session.cmd_run('hello from test')
        self.assertEqual(captured.get('content'), 'hello from test')

    def test_run_calls_plain_claude_no_flags(self):
        """cmd_run uses plain claude with no --resume or --continue."""
        from unittest.mock import patch as _patch
        with _patch('subprocess.run') as mock_run, _patch('os.chdir'):
            _cli_module_for_session.cmd_run('test prompt')
        args = mock_run.call_args[0][0]
        self.assertNotIn('--resume', args)
        self.assertNotIn('--continue', args)

    def test_run_cleans_up_temp_dir(self):
        """cmd_run removes the temp dir after claude exits."""
        from unittest.mock import patch as _patch
        tmp_path = {}
        def fake_chdir(path):
            if 'dir' not in tmp_path:  # capture only the first chdir (to temp dir)
                tmp_path['dir'] = path
        with _patch('subprocess.run'), _patch('os.chdir', side_effect=fake_chdir):
            _cli_module_for_session.cmd_run('cleanup test')
        d = tmp_path.get('dir', '')
        self.assertTrue(d, 'expected os.chdir to be called with temp dir')
        self.assertFalse(os.path.exists(d), 'temp dir should be deleted after cmd_run')

    def test_wizard_writes_wizard_md_as_claude_md(self):
        """cmd_wizard writes wizard.md content as CLAUDE.md in the temp dir."""
        import json as _json
        from unittest.mock import patch as _patch, MagicMock
        captured = {}
        tmp_path = {}

        def fake_chdir(path):
            if 'dir' not in tmp_path:
                tmp_path['dir'] = path

        boot = MagicMock()
        boot.returncode = 0
        boot.stdout = _json.dumps({'result': 'Hi!', 'session_id': 'xyz'})

        def fake_run(args, **kwargs):
            d = tmp_path.get('dir', '')
            claude_md = os.path.join(d, 'CLAUDE.md') if d else ''
            if d and os.path.exists(claude_md):
                captured['content'] = open(claude_md).read()
            return boot

        with _patch('subprocess.run', side_effect=fake_run), \
             _patch('os.chdir', side_effect=fake_chdir):
            _cli_module_for_session.cmd_wizard()
        self.assertIn('wizard', captured.get('content', '').lower())

    def test_wizard_bootstraps_then_resumes(self):
        """cmd_wizard runs a -p boot call first, then resumes interactively."""
        import json as _json
        from unittest.mock import patch as _patch, MagicMock
        calls = []

        boot = MagicMock()
        boot.returncode = 0
        boot.stdout = _json.dumps({'result': 'Hi! Wizard pitch.', 'session_id': 'boot-sid-42'})

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return boot

        with _patch('subprocess.run', side_effect=fake_run), _patch('os.chdir'):
            _cli_module_for_session.cmd_wizard()

        claude_bin = os.environ.get('CMUX_CLAUDE_CMD', 'claude')
        boot_calls = [c for c in calls if c and c[0] == claude_bin and '-p' in c]
        resume_calls = [c for c in calls if '--resume' in c]
        self.assertTrue(boot_calls, 'expected a -p boot call')
        self.assertIn('--output-format', boot_calls[0])
        self.assertTrue(resume_calls, 'expected a --resume call')
        self.assertIn('boot-sid-42', resume_calls[0])

    def test_wizard_falls_back_to_plain_if_bootstrap_fails(self):
        """cmd_wizard falls back to plain interactive if the -p boot call fails."""
        from unittest.mock import patch as _patch, MagicMock
        calls = []

        failed = MagicMock()
        failed.returncode = 1
        failed.stdout = ''

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return failed

        with _patch('subprocess.run', side_effect=fake_run), _patch('os.chdir'):
            _cli_module_for_session.cmd_wizard()

        self.assertEqual(len(calls), 2, 'expected boot call + fallback call')
        self.assertIn('-p', calls[0])
        self.assertNotIn('--resume', calls[1])


class TestUnblockIntegration(_CmuxBase):

    def test_start_with_unblock_flag_recorded_in_registry(self):
        """--unblock flag is stored in sessions.json."""
        name = f'ub{_rnd()}'
        self._start(name, '--unblock')
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn(name, reg)
        self.assertTrue(reg[name].get('unblock'), 'unblock should be True in sessions.json')

    def test_agent_register_with_unblock_persists_in_db(self):
        """cmux agent register --unblock stores unblock=1 in agents.db."""
        name = f'ub{_rnd()}'
        _cmux('agent', 'register', name, '--unblock', state_dir=self.state_dir)
        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT name, unblock FROM agents WHERE name = ?', (name,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], 1, 'unblock column should be 1')


# ------------------------------------------------------------------
# Tests for --allowed-tools flag
# ------------------------------------------------------------------

class TestAllowedTools(_CmuxBase):

    def test_agent_register_allowed_tools_persists_in_db(self):
        """cmux agent register --allowed-tools stores the value in agents.db."""
        name = f'at{_rnd()}'
        tools = 'Bash,Read,Edit,Write'
        _cmux('agent', 'register', name, '--allowed-tools', tools, state_dir=self.state_dir)
        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT name, allowed_tools FROM agents WHERE name = ?', (name,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], tools)

    def test_allowed_tools_flag_in_claude_cmd(self):
        """cmd_start with allowed_tools passes --allowedTools to the tmux claude invocation."""
        from unittest.mock import patch as _patch, MagicMock
        import cmux_lib.cli as _cli

        name = f'at{_rnd()}'
        home = os.path.join(self.state_dir, name)
        os.makedirs(home, exist_ok=True)
        # Pre-create identity.md to skip onboarding injection
        with open(os.path.join(home, 'identity.md'), 'w') as f:
            f.write('test')

        tmux_calls = []

        def fake_run(args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = '\n'.join([name]) + '\n'
            tmux_calls.append(list(args))
            return r

        def fake_popen(args, **kwargs):
            tmux_calls.append(list(args))
            m = MagicMock()
            m.pid = 99999
            return m

        orig_state = _cli.STATE_DIR
        _cli.STATE_DIR = self.state_dir
        try:
            with _patch('cmux_lib.cli.subprocess.run', side_effect=fake_run), \
                 _patch('cmux_lib.cli.subprocess.Popen', side_effect=fake_popen), \
                 _patch('cmux_lib.cli._wait_for_socket'), \
                 _patch('cmux_lib.cli._inject_startup_context'), \
                 _patch('cmux_lib.cli._store_session_id'), \
                 _patch('cmux_lib.cli.cmd_attach'), \
                 _patch('cmux_lib.cli.save_registry'), \
                 _patch('cmux_lib.cli.load_registry', return_value={}):
                _cli.cmd_start(name, detach=True, allowed_tools='Bash,Read',
                               workspace=None, no_inject=False)
        finally:
            _cli.STATE_DIR = orig_state

        # The tmux new-session or new-window call embeds the full claude_cmd string
        new_sess_calls = [c for c in tmux_calls if 'new-session' in c or 'new-window' in c]
        self.assertTrue(new_sess_calls, 'expected a tmux new-session or new-window call')
        claude_cmd_arg = ' '.join(new_sess_calls[0])
        self.assertIn('--allowedTools', claude_cmd_arg)
        self.assertIn('Bash,Read', claude_cmd_arg)

    def _start_mocked(self, name, sess_exists):
        """Run cmd_start with tmux/daemon mocked out; return the captured argv lists."""
        from unittest.mock import patch as _patch, MagicMock
        import cmux_lib.cli as _cli

        home = os.path.join(self.state_dir, name)
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'identity.md'), 'w') as f:
            f.write('test')

        tmux_calls = []

        def fake_run(args, **kwargs):
            r = MagicMock()
            # has-session decides which branch cmd_start takes.
            if 'has-session' in args:
                r.returncode = 0 if sess_exists else 1
            else:
                r.returncode = 0
            # list-windows must report the window, or cmd_start's liveness
            # check concludes claude died and exits.
            r.stdout = f'{name}\n'
            tmux_calls.append(list(args))
            return r

        def fake_popen(args, **kwargs):
            m = MagicMock()
            m.pid = 99999
            return m

        orig_state = _cli.STATE_DIR
        _cli.STATE_DIR = self.state_dir
        try:
            with _patch('cmux_lib.cli.subprocess.run', side_effect=fake_run), \
                 _patch('cmux_lib.cli.subprocess.Popen', side_effect=fake_popen), \
                 _patch('cmux_lib.cli._wait_for_socket'), \
                 _patch('cmux_lib.cli._inject_startup_context'), \
                 _patch('cmux_lib.cli._store_session_id'), \
                 _patch('cmux_lib.cli.cmd_attach'), \
                 _patch('cmux_lib.cli.save_registry'), \
                 _patch('cmux_lib.cli.load_registry', return_value={}):
                _cli.cmd_start(name, detach=True, workspace=None, no_inject=False)
        finally:
            _cli.STATE_DIR = orig_state
        return tmux_calls

    def test_new_session_pinned_to_caller_cwd(self):
        """tmux new-session gets -c <caller cwd>, so the agent starts where cmux up ran."""
        calls = self._start_mocked(f'cw{_rnd()}', sess_exists=False)
        new_sess = [c for c in calls if 'new-session' in c]
        self.assertTrue(new_sess, 'expected a tmux new-session call')
        self.assertIn('-c', new_sess[0])
        self.assertEqual(new_sess[0][new_sess[0].index('-c') + 1], os.getcwd())

    def test_new_window_pinned_to_caller_cwd(self):
        """A window added to an existing session gets -c too, not the session's stale start-dir."""
        calls = self._start_mocked(f'cw{_rnd()}', sess_exists=True)
        new_win = [c for c in calls if 'new-window' in c]
        self.assertTrue(new_win, 'expected a tmux new-window call')
        self.assertIn('-c', new_win[0])
        self.assertEqual(new_win[0][new_win[0].index('-c') + 1], os.getcwd())

    def test_new_window_target_forces_session_resolution(self):
        """new-window -t uses a trailing colon so a same-named WINDOW can't win the lookup."""
        name = f'cw{_rnd()}'
        calls = self._start_mocked(name, sess_exists=True)
        new_win = [c for c in calls if 'new-window' in c]
        self.assertTrue(new_win, 'expected a tmux new-window call')
        target = new_win[0][new_win[0].index('-t') + 1]
        self.assertTrue(
            target.endswith(':'),
            f'new-window target {target!r} must end with ":" to force session resolution'
        )

    def test_allowed_tools_inherited_from_db_on_restart(self):
        """Registered allowed_tools are picked up on cmux up without re-specifying the flag."""
        name = f'at{_rnd()}'
        tools = 'Bash,Read,Write'
        _cmux('agent', 'register', name, '--allowed-tools', tools, state_dir=self.state_dir)

        # Verify DB entry
        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT allowed_tools FROM agents WHERE name = ?', (name,)).fetchone()
        conn.close()
        self.assertEqual(row[0], tools)


# ------------------------------------------------------------------
# Tests for cmux clone
# ------------------------------------------------------------------

class TestClone(_CmuxBase):

    def _make_source(self, name, identity_text='I am the source agent.', role=None):
        """Create a started agent with identity.md, optionally registered."""
        home = os.path.join(self.state_dir, name)
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'identity.md'), 'w') as f:
            f.write(identity_text)
        if role:
            _cmux('agent', 'register', name, '--role', role, state_dir=self.state_dir)
        self._start(name)
        _wait_socket(os.path.join(self.state_dir, name, f'{name}.sock'))
        return home

    def test_clone_copies_identity(self):
        """cmux clone copies source identity.md to new agent's home."""
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        self._make_source(src, identity_text='Role: original agent.')
        _cmux('clone', src, clone, '-d', state_dir=self.state_dir)
        self._started.append(clone)
        new_identity = os.path.join(self.state_dir, clone, 'identity.md')
        self.assertTrue(os.path.exists(new_identity))
        self.assertIn('original agent', open(new_identity).read())

    def test_clone_does_not_copy_cq(self):
        """cmux clone does not copy the source cq database."""
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        src_home = self._make_source(src)
        # Create a fake cq db in source
        cq_dir = os.path.join(src_home, '.cq')
        os.makedirs(cq_dir, exist_ok=True)
        with open(os.path.join(cq_dir, 'issues.db'), 'w') as f:
            f.write('fake db')
        _cmux('clone', src, clone, '-d', state_dir=self.state_dir)
        self._started.append(clone)
        clone_cq = os.path.join(self.state_dir, clone, '.cq', 'issues.db')
        self.assertFalse(os.path.exists(clone_cq), 'cq db should not be copied to clone')

    def test_clone_no_session_resume(self):
        """cmux clone does not carry the source's last-session-id to the new agent."""
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        src_home = self._make_source(src)
        with open(os.path.join(src_home, 'last-session-id'), 'w') as f:
            f.write('fake-session-uuid')
        _cmux('clone', src, clone, '-d', state_dir=self.state_dir)
        self._started.append(clone)
        clone_sid = os.path.join(self.state_dir, clone, 'last-session-id')
        if os.path.exists(clone_sid):
            # If file exists it must be a freshly detected session, not the source's
            self.assertNotEqual(open(clone_sid).read().strip(), 'fake-session-uuid')

    def test_clone_startup_message_references_source(self):
        """Clone startup message (via _inject_startup_context) names the source agent."""
        from unittest.mock import patch as _patch
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        # Set up source home
        src_home = os.path.join(self.state_dir, src)
        os.makedirs(src_home, exist_ok=True)
        with open(os.path.join(src_home, 'identity.md'), 'w') as f:
            f.write('source identity')
        # Set up clone home with copied identity + marker
        clone_home = os.path.join(self.state_dir, clone)
        os.makedirs(clone_home, exist_ok=True)
        import shutil
        shutil.copy2(os.path.join(src_home, 'identity.md'), os.path.join(clone_home, 'identity.md'))
        with open(os.path.join(clone_home, 'clone-source'), 'w') as f:
            f.write(src)

        with _patch('cmux_lib.cli.cmd_send') as mock_send:
            _cli_module_for_session._inject_startup_context(clone, clone_home)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        self.assertIn(src, msg)
        self.assertIn('clone_readme.md', msg)
        self.assertIn('clone', msg.lower())

    def test_clone_marker_deleted_after_startup(self):
        """clone-source marker is removed after _inject_startup_context sends the message."""
        from unittest.mock import patch as _patch
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        src_home = os.path.join(self.state_dir, src)
        os.makedirs(src_home, exist_ok=True)
        with open(os.path.join(src_home, 'identity.md'), 'w') as f:
            f.write('source')
        clone_home = os.path.join(self.state_dir, clone)
        os.makedirs(clone_home, exist_ok=True)
        import shutil
        shutil.copy2(os.path.join(src_home, 'identity.md'), os.path.join(clone_home, 'identity.md'))
        with open(os.path.join(clone_home, 'clone-source'), 'w') as f:
            f.write(src)

        with _patch('cmux_lib.cli.cmd_send'):
            _cli_module_for_session._inject_startup_context(clone, clone_home)

        self.assertFalse(
            os.path.exists(os.path.join(clone_home, 'clone-source')),
            'clone-source marker should be deleted after startup message is sent',
        )

    def test_clone_no_source_identity_exits_nonzero(self):
        """cmux clone fails with nonzero exit when source has no identity.md."""
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        # source home exists but has no identity.md
        os.makedirs(os.path.join(self.state_dir, src), exist_ok=True)
        r = _cmux('clone', src, clone, '-d', state_dir=self.state_dir, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('identity.md', r.stderr)

    def test_clone_existing_name_exits_nonzero(self):
        """cmux clone refuses to overwrite an agent that already has identity.md."""
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        self._make_source(src)
        # Pre-create clone identity
        clone_home = os.path.join(self.state_dir, clone)
        os.makedirs(clone_home, exist_ok=True)
        with open(os.path.join(clone_home, 'identity.md'), 'w') as f:
            f.write('already exists')
        r = _cmux('clone', src, clone, '-d', state_dir=self.state_dir, check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_clone_inherits_source_role_from_db(self):
        """Clone agent is registered in DB with source's role when source is registered."""
        src = f'src{_rnd()}'
        clone = f'cln{_rnd()}'
        self._make_source(src, role='Senior Coordinator')
        _cmux('clone', src, clone, '-d', state_dir=self.state_dir)
        self._started.append(clone)
        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT role FROM agents WHERE name = ?', (clone,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'Senior Coordinator')


# ------------------------------------------------------------------
# Tests for --identity flag
# ------------------------------------------------------------------

class TestIdentityFlag(_CmuxBase):

    def test_identity_flag_seeds_identity_md(self):
        """cmux up --identity <path> copies the file to the agent's home on first start."""
        name = f'id{_rnd()}'
        identity_src = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
        identity_src.write('Role: Template Coordinator.')
        identity_src.close()
        try:
            _cmux('up', name, '-d', '--identity', identity_src.name, state_dir=self.state_dir)
            self._started.append(name)
            identity_dst = os.path.join(self.state_dir, name, 'identity.md')
            self.assertTrue(os.path.exists(identity_dst))
            self.assertIn('Template Coordinator', open(identity_dst).read())
        finally:
            os.unlink(identity_src.name)

    def test_identity_flag_short_form(self):
        """cmux up -i <path> is equivalent to --identity."""
        name = f'id{_rnd()}'
        identity_src = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
        identity_src.write('Role: Short Flag Agent.')
        identity_src.close()
        try:
            _cmux('up', name, '-d', '-i', identity_src.name, state_dir=self.state_dir)
            self._started.append(name)
            identity_dst = os.path.join(self.state_dir, name, 'identity.md')
            self.assertTrue(os.path.exists(identity_dst))
            self.assertIn('Short Flag Agent', open(identity_dst).read())
        finally:
            os.unlink(identity_src.name)

    def test_identity_flag_does_not_overwrite_existing(self):
        """--identity is silently ignored when identity.md already exists."""
        name = f'id{_rnd()}'
        home = os.path.join(self.state_dir, name)
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'identity.md'), 'w') as f:
            f.write('Existing identity.')
        identity_src = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
        identity_src.write('Template should not overwrite.')
        identity_src.close()
        try:
            _cmux('up', name, '-d', '--identity', identity_src.name, state_dir=self.state_dir)
            self._started.append(name)
            content = open(os.path.join(home, 'identity.md')).read()
            self.assertIn('Existing identity', content)
            self.assertNotIn('should not overwrite', content)
        finally:
            os.unlink(identity_src.name)

    def test_identity_flag_missing_file_exits_nonzero(self):
        """cmux up --identity with a nonexistent path exits nonzero."""
        name = f'id{_rnd()}'
        r = _cmux('up', name, '-d', '--identity', '/tmp/does-not-exist-xyz.md',
                  state_dir=self.state_dir, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('not found', r.stderr)

    def test_agent_register_identity_persists_in_db(self):
        """cmux agent register --identity stores identity_path in agents.db."""
        name = f'id{_rnd()}'
        identity_src = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
        identity_src.write('Registered identity template.')
        identity_src.close()
        try:
            _cmux('agent', 'register', name, '--identity', identity_src.name,
                  state_dir=self.state_dir)
            db_path = os.path.join(self.state_dir, 'agents.db')
            conn = sqlite3.connect(db_path)
            row = conn.execute('SELECT identity_path FROM agents WHERE name = ?', (name,)).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], identity_src.name)
        finally:
            os.unlink(identity_src.name)

    def test_identity_from_db_applied_on_up(self):
        """Registered identity_path is used on cmux up when no identity.md exists yet."""
        name = f'id{_rnd()}'
        identity_src = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
        identity_src.write('DB-registered identity.')
        identity_src.close()
        try:
            _cmux('agent', 'register', name, '--identity', identity_src.name,
                  state_dir=self.state_dir)
            _cmux('up', name, '-d', state_dir=self.state_dir)
            self._started.append(name)
            identity_dst = os.path.join(self.state_dir, name, 'identity.md')
            self.assertTrue(os.path.exists(identity_dst))
            self.assertIn('DB-registered identity', open(identity_dst).read())
        finally:
            os.unlink(identity_src.name)


if __name__ == '__main__':
    unittest.main()


# ------------------------------------------------------------------
# Unit tests for verified delivery (_inject_text / _submitted / _submit)
# — the fix for the "message stuck in the input box" bug.
# ------------------------------------------------------------------

import cmux_lib.daemon as _daemon_module


class TestVerifiedDelivery(unittest.TestCase):
    def test_inject_uses_bracketed_paste(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0)

        with patch.object(_daemon_module.subprocess, 'run', side_effect=fake_run):
            _daemon_module._inject_text('s:0', 'hello world')
        self.assertEqual(calls[0][:2], ['tmux', 'load-buffer'])
        self.assertIn('paste-buffer', calls[1])
        self.assertIn('-p', calls[1], 'must be a BRACKETED paste (-p)')
        self.assertNotIn('send-keys', [c[1] for c in calls])

    def test_inject_falls_back_to_literal_send_keys(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=1 if cmd[1] == 'load-buffer' else 0)

        with patch.object(_daemon_module.subprocess, 'run', side_effect=fake_run):
            _daemon_module._inject_text('s:0', 'hello world')
        self.assertEqual(calls[1][1], 'send-keys')
        self.assertIn('-l', calls[1], 'fallback must type literally, never key-name lookup')

    def test_submitted_detects_stuck_input(self):
        pane = 'some scrollback\n❯ [cmux]: test-probe still sitting here\n  status bar'
        with patch.object(_daemon_module, 'pane_content', return_value=pane):
            self.assertFalse(_daemon_module._submitted('s:0', '[cmux]: test-probe still sitting here'))

    def test_submitted_ignores_conversation_echo_and_ghost_hint(self):
        # After a REAL submit: echo sits ABOVE the prompt, input shows only the ghost hint.
        pane = '> [cmux]: test-probe message\n\n❯\xa0Try "create a util..."\n  status'
        with patch.object(_daemon_module, 'pane_content', return_value=pane):
            self.assertTrue(_daemon_module._submitted('s:0', '[cmux]: test-probe message'))

    def test_submitted_true_when_no_prompt_visible(self):
        with patch.object(_daemon_module, 'pane_content', return_value='redrawing…'):
            self.assertTrue(_daemon_module._submitted('s:0', 'anything'))

    def test_submit_retries_until_input_clears(self):
        stuck = '❯ [cmux]: probe-xyz stuck'
        clear = '> [cmux]: probe-xyz stuck\n❯\xa0'
        panes = [stuck, stuck, clear]  # cleared on the 3rd check
        enters = []

        def fake_run(cmd, **kw):
            if 'send-keys' in cmd and 'Enter' in cmd:
                enters.append(cmd)
            return MagicMock(returncode=0)

        with patch.object(_daemon_module.subprocess, 'run', side_effect=fake_run), \
             patch.object(_daemon_module, 'pane_content', side_effect=panes), \
             patch.object(_daemon_module.time, 'sleep'):
            ok = _daemon_module._submit('s:0', '[cmux]: probe-xyz stuck')
        self.assertTrue(ok)
        self.assertEqual(len(enters), 3, 'one Enter per verification attempt')

    def test_submit_gives_up_loudly_after_retries(self):
        stuck = '❯ [cmux]: probe-abc never leaves'
        enters = []

        def fake_run(cmd, **kw):
            if 'send-keys' in cmd and 'Enter' in cmd:
                enters.append(cmd)
            return MagicMock(returncode=0)

        with patch.object(_daemon_module.subprocess, 'run', side_effect=fake_run), \
             patch.object(_daemon_module, 'pane_content', return_value=stuck), \
             patch.object(_daemon_module.time, 'sleep'):
            ok = _daemon_module._submit('s:0', '[cmux]: probe-abc never leaves')
        self.assertFalse(ok)
        self.assertEqual(len(enters), _daemon_module._SUBMIT_RETRIES)

    def test_deliver_short_message_injects_and_submits(self):
        seen = {'inject': None, 'submit': None}
        with patch.object(_daemon_module, '_inject_text',
                          side_effect=lambda t, x: seen.__setitem__('inject', x)), \
             patch.object(_daemon_module, '_submit',
                          side_effect=lambda t, x: seen.__setitem__('submit', x) or True), \
             patch.object(_daemon_module.time, 'sleep'):
            deliver = _daemon_module.make_deliver('unittestagent', 's:0')
            deliver({'from': 'tester', 'body': 'short body'})
        self.assertEqual(seen['inject'], '[tester@cmux]: short body')
        self.assertEqual(seen['submit'], seen['inject'])

    def test_deliver_long_message_uses_file_pointer(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(_daemon_module, 'STATE_DIR', td), \
                 patch.object(_daemon_module, '_inject_text') as inj, \
                 patch.object(_daemon_module, '_submit', return_value=True), \
                 patch.object(_daemon_module.time, 'sleep'):
                deliver = _daemon_module.make_deliver('longagent', 's:0')
                deliver({'from': 'tester', 'body': 'x' * 400})
            text = inj.call_args[0][1]
            self.assertTrue(text.startswith('[cmux]: @'))
            path = text.split('@', 1)[1]
            self.assertIn('longagent', path)
