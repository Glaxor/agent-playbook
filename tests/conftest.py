"""Shared fixtures: stub agent binaries driven by JSON plan files.

Each stub replaces a real CLI via its *_BIN env var. Every invocation pops the
next step from its plan file (a JSON list), emits a response in that vendor's
real output format, and appends {argv, prompt} to its calls file.
Step kinds: success | usage | transient | error.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "claude_runner.py"

sys.path.insert(0, str(ROOT))

STUB_COMMON = '''\
import json, os, sys

FLAVOR = {flavor!r}
# The runner's contract is UTF-8 bytes on stdin (like the real Node CLIs);
# decode explicitly so the stub doesn't fall back to the locale codepage.
# Normalize CRLF like text mode would (Windows pipes deliver CRLF).
prompt = sys.stdin.buffer.read().decode("utf-8").replace("\\r\\n", "\\n")
plan_path = os.environ[FLAVOR.upper() + "_STUB_PLAN"]
with open(plan_path) as f:
    plan = json.load(f)
step = plan.pop(0) if plan else {{"kind": "success"}}
with open(plan_path, "w") as f:
    json.dump(plan, f)

calls_path = os.environ.get(FLAVOR.upper() + "_STUB_CALLS")
if calls_path:
    with open(calls_path, "a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "prompt": prompt}}) + "\\n")

kind = step.get("kind", "success")

if kind == "hang":                    # simulate a stuck agent call (any flavor)
    import time as _time
    _time.sleep(float(step.get("sleep", 30)))
    kind = "success"                  # if not killed by the timeout, succeed

if FLAVOR == "claude":
    if kind == "success":
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                          "result": step.get("result", "done"),
                          "session_id": step.get("session_id", "stub-session-0001"),
                          "total_cost_usd": step.get("cost", 0.01), "num_turns": 1}}))
        sys.exit(0)
    if kind == "usage":
        print(json.dumps({{"type": "result", "subtype": "error", "is_error": True,
                          "result": step.get("result", "Claude AI usage limit reached."),
                          "session_id": step.get("session_id"),
                          "total_cost_usd": 0.0, "num_turns": 0}}))
        sys.exit(1)
    if kind == "transient":
        print("We are temporarily limiting requests. This is not your usage limit.",
              file=sys.stderr)
        sys.exit(1)
    print(json.dumps({{"type": "result", "subtype": "error", "is_error": True,
                      "result": step.get("result", "boom"),
                      "session_id": step.get("session_id")}}))
    sys.exit(1)

if FLAVOR == "codex":
    if kind == "success":
        print(json.dumps({{"type": "thread.started",
                          "thread_id": step.get("session_id", "codex-thread-0001")}}))
        print(json.dumps({{"type": "item.completed",
                          "item": {{"type": "agent_message",
                                   "text": step.get("result", "done")}}}}))
        sys.exit(0)
    if kind == "usage":
        print("You've hit your usage limit. Try again later.", file=sys.stderr)
        sys.exit(1)
    if kind == "transient":
        print("429 Too Many Requests", file=sys.stderr)
        sys.exit(1)
    print(step.get("result", "codex boom"), file=sys.stderr)
    sys.exit(1)

if FLAVOR == "gemini":
    if kind == "old_cli_success":
        # simulate gemini 0.1.x: --output-format is an unknown flag
        if "--output-format" in sys.argv:
            with open(plan_path, "w") as f:
                json.dump([step] + plan, f)      # retry consumes the same step
            print("Unknown arguments: output-format, outputFormat", file=sys.stderr)
            sys.exit(1)
        print(step.get("result", "done"))
        sys.exit(0)
    if kind == "success":
        print(json.dumps({{"response": step.get("result", "done"),
                          "stats": {{"turns": 1}}}}))
        sys.exit(0)
    if kind == "usage":
        print("Quota exceeded for quota metric 'Gemini Requests'.", file=sys.stderr)
        sys.exit(1)
    if kind == "transient":
        print("429 rate limited, please retry", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({{"error": {{"message": step.get("result", "gemini boom")}}}}))
    sys.exit(1)
'''


class Stub:
    """One stub agent binary + its plan/calls files."""

    def __init__(self, flavor: str, tmp_path: Path):
        self.flavor = flavor
        stub_py = tmp_path / f"stub_{flavor}.py"
        stub_py.write_text(STUB_COMMON.format(flavor=flavor))
        if os.name == "nt":
            self.bin = tmp_path / f"{flavor}-stub.bat"
            self.bin.write_text(f'@echo off\r\n"{sys.executable}" "{stub_py}" %*\r\n')
        else:
            self.bin = tmp_path / f"{flavor}-stub"
            self.bin.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub_py}" "$@"\n')
            self.bin.chmod(0o755)
        self.plan_path = tmp_path / f"{flavor}_plan.json"
        self.calls_path = tmp_path / f"{flavor}_calls.jsonl"
        self.set_plan([])

    def set_plan(self, steps: list[dict]) -> None:
        self.plan_path.write_text(json.dumps(steps))

    def remaining_plan(self) -> list[dict]:
        return json.loads(self.plan_path.read_text())

    def calls(self) -> list[dict]:
        if not self.calls_path.exists():
            return []
        return [json.loads(l) for l in self.calls_path.read_text().splitlines() if l.strip()]

    def env_vars(self) -> dict:
        return {f"{self.flavor.upper()}_BIN": str(self.bin),
                f"{self.flavor.upper()}_STUB_PLAN": str(self.plan_path),
                f"{self.flavor.upper()}_STUB_CALLS": str(self.calls_path)}


class AgentStubs:
    """claude + codex + gemini stubs sharing one tmp dir and one env()."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "runner-home"    # isolates ~/.claude-runner
        self.claude = Stub("claude", tmp_path)
        self.codex = Stub("codex", tmp_path)
        self.gemini = Stub("gemini", tmp_path)

    # convenience passthroughs so existing tests keep reading naturally
    def set_plan(self, steps):
        self.claude.set_plan(steps)

    def calls(self):
        return self.claude.calls()

    def env(self, **extra) -> dict:
        env = {**os.environ, "CLAUDE_RUNNER_HOME": str(self.home)}
        for s in (self.claude, self.codex, self.gemini):
            env.update(s.env_vars())
        env.update({k: str(v) for k, v in extra.items()})
        return env


@pytest.fixture
def stub(tmp_path: Path) -> AgentStubs:
    return AgentStubs(tmp_path)


def write_playbook(path: Path, instructions: list, **top) -> Path:
    import yaml
    pb = {"session": "keep",
          "continue_on_limit": True,
          "notify_backend": "none",
          "defaults": {"cwd": str(path.parent), "permission_mode": "dontAsk"},
          "limits": {"poll_interval_sec": 1, "resume_poll_sec": 1,
                     "transient_base_sec": 0},
          "instructions": instructions}
    pb.update(top)
    path.write_text(yaml.safe_dump(pb))
    return path


def run_runner(args: list[str], cwd: Path, env: dict,
               timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RUNNER), *args],
                          cwd=str(cwd), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def start_runner(args: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(RUNNER), *args],
                            cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace")


def wait_for(cond, timeout: float = 30, interval: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def read_state(pb_path: Path) -> dict:
    sp = pb_path.with_suffix(".state.json")
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text())
    except Exception:
        return {}
