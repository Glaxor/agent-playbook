"""Unit tests for the pure/portable helpers in claude_runner and playbook_mcp."""
from __future__ import annotations

import os
import subprocess
import sys

import claude_runner
import playbook_mcp
from claude_runner import Limit, classify_limit, parse_reset_seconds, pid_alive

from conftest import wait_for


# --------------------------------------------------------------------------- #
# classify_limit
# --------------------------------------------------------------------------- #
def test_classify_usage():
    assert classify_limit("Claude AI usage limit reached.") == Limit.USAGE
    assert classify_limit("Your limit will reset at 3pm (America/New_York)") == Limit.USAGE


def test_classify_transient():
    assert classify_limit("We are temporarily limiting requests") == Limit.TRANSIENT
    assert classify_limit("you have been rate limited") == Limit.TRANSIENT
    assert classify_limit("this is not your usage limit") == Limit.TRANSIENT


def test_classify_none():
    assert classify_limit("") == Limit.NONE
    assert classify_limit("all tests pass, committed") == Limit.NONE


# --------------------------------------------------------------------------- #
# parse_reset_seconds
# --------------------------------------------------------------------------- #
def test_parse_reset_basic():
    s = parse_reset_seconds("Your limit will reset at 3pm (America/New_York).", 86400)
    assert s is not None and 0 <= s <= 86400


def test_parse_reset_with_minutes():
    s = parse_reset_seconds("limit will reset at 11:30am (Europe/Berlin)", 86400)
    assert s is not None and 0 <= s <= 86400


def test_parse_reset_capped():
    s = parse_reset_seconds("reset at 3pm (America/New_York)", 10)
    assert s is not None and s <= 10


def test_parse_reset_unparseable():
    assert parse_reset_seconds("usage limit reached", 86400) is None
    assert parse_reset_seconds("reset at some point soon", 86400) is None
    assert parse_reset_seconds("", 86400) is None


# --------------------------------------------------------------------------- #
# pid_alive / _alive — must detect liveness WITHOUT killing the process
# (the old MCP _alive used os.kill(pid, 0), which terminates on Windows)
# --------------------------------------------------------------------------- #
def test_pid_alive_self():
    assert pid_alive(os.getpid()) is True
    assert playbook_mcp._alive(os.getpid()) is True


def test_pid_alive_bogus():
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(-5) is False
    assert playbook_mcp._alive(0) is False


def test_pid_alive_does_not_kill():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert pid_alive(proc.pid) is True
        assert playbook_mcp._alive(proc.pid) is True
        # the check must NOT have terminated it (regression: os.kill(pid, 0) on
        # Windows == TerminateProcess)
        assert proc.poll() is None, "liveness check killed the process!"
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert wait_for(lambda: not pid_alive(proc.pid), timeout=10)
    assert playbook_mcp._alive(proc.pid) is False
