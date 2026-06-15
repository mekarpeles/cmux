#!/usr/bin/env python3
"""
Fake Claude process for cmux integration tests.

Prints the idle prompt (❯) immediately and keeps refreshing it so the
daemon's is_idle() detector always returns True. Stays alive until killed.
"""
import sys
import time

while True:
    print('\n❯ ', end='', flush=True)
    time.sleep(1)
