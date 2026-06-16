#!/usr/bin/env python3
"""
Fake Claude process for cmux integration tests.

Prints the idle prompt (❯) immediately and keeps refreshing it so the
daemon's is_idle() detector always returns True. Stays alive until killed.
"""
import sys
import time

while True:
    # Mimic Claude Code's real idle prompt: ❯ followed by NBSP + ghost hint text.
    # This is what tmux capture-pane sees in a real session.
    print('\n❯\xa0Try "create a util logging.py that..."', end='', flush=True)
    time.sleep(1)
