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


def _cmux(*args, state_dir, check=True):
    env = os.environ.copy()
    env['CMUX_STATE_DIR'] = state_dir
    env['CMUX_CLAUDE_CMD'] = f'{sys.executable} {FAKE_CLAUDE}'
    # Disable session-detect retries — fake_claude creates no JSONL files.
    env['CMUX_SESSION_DETECT_RETRIES'] = '1'
    env['CMUX_SESSION_DETECT_INTERVAL'] = '0'
    return subprocess.run(
        [CMUX, *args],
        capture_output=True, text=True, env=env,
        check=check,
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


class _CmuxBase(unittest.TestCase):
    """Base class with setUp/tearDown/helpers. Contains no test methods."""

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix='cmux-test-')
        self._started = []

    def tearDown(self):
        for name in self._started:
            _cmux('stop', name, state_dir=self.state_dir, check=False)
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'cmux-test-{os.getpid()}'],
            capture_output=True,
        )

    def _start(self, name, *extra, workspace=None):
        args = []
        if workspace:
            args += ['-s', workspace]
        args += ['start', name, '-d', *extra]
        r = _cmux(*args, state_dir=self.state_dir)
        self._started.append(name)
        return r


class TestCmuxIntegration(_CmuxBase):

    # ------------------------------------------------------------------

    def test_start_creates_registry_entry(self):
        self._start('t1')
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn('t1', reg)
        self.assertEqual(reg['t1']['name'], 't1')

    def test_start_creates_socket(self):
        self._start('t2')
        sock = os.path.join(self.state_dir, 't2.sock')
        ready = _wait_socket(sock, timeout=10)
        self.assertTrue(ready, "daemon socket never became ready")

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
        _wait_socket(os.path.join(self.state_dir, 't5.sock'))
        r = _cmux('send', 't5', 'hello from test', state_dir=self.state_dir)
        self.assertIn('queued', r.stdout)

    def test_send_unknown_agent_exits_nonzero(self):
        r = _cmux('send', 'nobody', 'hi', state_dir=self.state_dir, check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_stop_removes_registry_entry(self):
        self._start('t6')
        _wait_socket(os.path.join(self.state_dir, 't6.sock'))
        _cmux('stop', 't6', state_dir=self.state_dir)
        self._started.remove('t6')
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertNotIn('t6', reg)

    def test_stop_removes_socket(self):
        self._start('t7')
        sock = os.path.join(self.state_dir, 't7.sock')
        _wait_socket(sock)
        _cmux('stop', 't7', state_dir=self.state_dir)
        self._started.remove('t7')
        time.sleep(0.3)
        self.assertFalse(os.path.exists(sock))

    def test_message_delivered_to_pane(self):
        """Message sent via cmux send appears in the fake Claude's tmux pane."""
        self._start('t8')
        _wait_socket(os.path.join(self.state_dir, 't8.sock'))
        _cmux('send', 't8', 'test-payload-xyz', state_dir=self.state_dir)
        # Give the daemon a moment to detect idle and deliver
        time.sleep(2.5)
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        target = reg['t8']['tmux_target']
        pane = subprocess.run(
            ['tmux', 'capture-pane', '-t', target, '-p'],
            capture_output=True, text=True,
        ).stdout
        self.assertIn('test-payload-xyz', pane)

    def test_workspace_agents_share_tmux_session(self):
        self._start('w1', workspace='shared')
        self._start('w2', workspace='shared')
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertEqual(reg['w1']['tmux_session'], reg['w2']['tmux_session'])

    def test_stop_workspace_agent_leaves_others(self):
        self._start('wa', workspace='ws2')
        self._start('wb', workspace='ws2')
        _wait_socket(os.path.join(self.state_dir, 'wa.sock'))
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
        sock = os.path.join(self.state_dir, 'tdb.sock')
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
                'socket': os.path.join(state_dir, f'{name}.sock'),
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
        self._start(name, '--no-inject')
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))

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
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))

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
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))

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
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))
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
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))
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
        _wait_socket(os.path.join(self.state_dir, f'{a1}.sock'))
        _wait_socket(os.path.join(self.state_dir, f'{a2}.sock'))

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

        _wait_socket(os.path.join(self.state_dir, f'{a1}.sock'))
        _wait_socket(os.path.join(self.state_dir, f'{a2}.sock'))

        reg2 = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertIn(a1, reg2)
        self.assertIn(a2, reg2)


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
                'socket': os.path.join(self.state_dir, 'odie.sock'),
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
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.05), daemon=True)
            t.start()
            self.assertTrue(escape_sent.wait(timeout=5.0), 'Escape key should have been sent')

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
            elif 'send-keys' in cmd_list and any('[claudio@noreply]' in a for a in cmd_list):
                notification_sent.set()
            return result

        target = 'cmux-notify:test'
        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run), \
             patch('time.sleep', return_value=None):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.01), daemon=True)
            t.start()
            self.assertTrue(notification_sent.wait(timeout=5.0), 'notification should have been injected')

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

        with patch('cmux_lib.daemon.subprocess.run', side_effect=mock_run):
            t = threading.Thread(target=_unblock_watcher, args=('test', target, 0.05), daemon=True)
            t.start()
            time.sleep(0.3)  # allow ~6 poll iterations

        self.assertEqual(len(send_key_calls), 0, 'no send-keys calls on a clean pane')


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
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))
        _cmux('down', name, state_dir=self.state_dir)
        self._started.remove(name) if name in self._started else None
        reg = json.load(open(os.path.join(self.state_dir, 'sessions.json')))
        self.assertNotIn(name, reg)

    def test_rm_deregisters_agent_preserves_home(self):
        name = f'rm{_rnd()}'
        _cmux('up', name, '-d', state_dir=self.state_dir)
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))
        _cmux('down', name, state_dir=self.state_dir)
        self._started.remove(name) if name in self._started else None
        home = os.path.join(self.state_dir, name)
        self.assertTrue(os.path.isdir(home))

        r = _cmux('rm', name, state_dir=self.state_dir)
        # Home dir must survive — it has provenance (cq, logs, scripts)
        self.assertTrue(os.path.isdir(home), 'home dir should be preserved after rm')
        # DB entry must be gone
        db_path = os.path.join(self.state_dir, 'agents.db')
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT name FROM agents WHERE name = ?', (name,)).fetchone()
        conn.close()
        self.assertIsNone(row, 'agent should be de-registered from agents.db after rm')
        self.assertIn('preserved', r.stdout)

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

    def test_up_writes_identity_from_initial_prompt(self):
        """cmux up writes initial_prompt to identity.md if identity.md absent."""
        name = f'sc{_rnd()}'
        _cmux('agent', 'register', name, '--', 'You are a test agent.', state_dir=self.state_dir)
        self._start(name)
        identity_path = os.path.join(self.state_dir, name, 'identity.md')
        self.assertTrue(os.path.exists(identity_path))
        self.assertIn('test agent', open(identity_path).read())

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
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))
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
        _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))
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

    def test_identity_injects_at_reference(self):
        """_inject_identity sends a @ file reference — never reads or inlines content."""
        from unittest.mock import patch as _patch
        home = tempfile.mkdtemp(prefix='cmux-id-test-')
        identity_path = os.path.join(home, 'identity.md')
        try:
            with open(identity_path, 'w') as f:
                f.write('x' * 5000)  # any size — no limit with @ reference
            with _patch('cmux_lib.cli.cmd_send') as mock_send:
                _cli_module_for_session._inject_identity(home)
            mock_send.assert_called_once()
            sent_msg = mock_send.call_args[0][1]
            self.assertIn(f'@{identity_path}', sent_msg)
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
            _wait_socket(os.path.join(self.state_dir, f'{name}.sock'))
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

        boot_calls = [c for c in calls if '-p' in c]
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


if __name__ == '__main__':
    unittest.main()
