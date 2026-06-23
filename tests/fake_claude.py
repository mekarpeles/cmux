#!/usr/bin/env python3
"""
Fake Claude process for cmux integration tests.

Prints the idle prompt (❯) immediately and keeps refreshing it so the
daemon's is_idle() detector always returns True. Stays alive until killed.

After printing the full ghost-hint line, the cursor is repositioned to
column 2 (right after ❯\xa0) via a carriage-return + cursor-right escape.
This matches the real Claude Code idle state that is_idle() expects:
  - '❯' visible in pane   ✓
  - cursor_x <= 2         ✓  (column 2, after ❯ + NBSP)
  - no typed text         ✓  (cursor_x >= 2, text-check path skipped)
"""
import sys
import time

while True:
    # Print the idle prompt with ghost hint text (mimics real Claude Code pane).
    print('\n❯\xa0Try "create a util logging.py that..."', end='', flush=True)
    # Reposition cursor to column 2 (after ❯\xa0) so is_idle() cursor check passes.
    # \r moves to column 0; \033[2C moves right 2 columns → column 2.
    print('\r\033[2C', end='', flush=True)
    time.sleep(1)
