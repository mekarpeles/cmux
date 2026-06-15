"""
Integration tests for cmux CLI.

Uses a fake Claude process and isolated state dirs so real sessions
and ~/.cmux are never touched. Requires tmux to be installed.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

FAKE_CLAUDE = os.path.join(os.path.dirname(__file__), 'fake_claude.py')
CMUX = 'cmux'


def _cmux(*args, state_dir, check=True):
    env = os.environ.copy()
    env['CMUX_STATE_DIR'] = state_dir
    env['CMUX_CLAUDE_CMD'] = f'{sys.executable} {FAKE_CLAUDE}'
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


class TestCmuxIntegration(unittest.TestCase):

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix='cmux-test-')
        self._started = []

    def tearDown(self):
        # Stop any agents started during the test
        for name in self._started:
            _cmux('stop', name, state_dir=self.state_dir, check=False)
        # Kill lingering tmux sessions
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


if __name__ == '__main__':
    unittest.main()
