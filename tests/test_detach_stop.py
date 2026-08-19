"""Detach, pid-lock, stop, and MCP core_* tests — the Windows-critical paths."""
from __future__ import annotations

import json
import os

import playbook_mcp

from conftest import (RUNNER, read_state, run_runner, start_runner, wait_for,
                      write_playbook)


def _log_text(pb):
    lf = pb.parent / (pb.stem + ".logs") / "runner.log"
    return lf.read_text() if lf.exists() else ""


def _waiting_playbook(stub, tmp_path, first_success=False):
    """A playbook whose runner ends up parked in a long usage-limit wait."""
    instructions = ([{"prompt": "warmup"}] if first_success else []) + [{"prompt": "job"}]
    pb = write_playbook(tmp_path / "pb.yaml", instructions,
                        limits={"poll_interval_sec": 3600, "resume_poll_sec": 3600,
                                "transient_base_sec": 0})
    plan = ([{"kind": "success"}] if first_success else []) + [{"kind": "usage"}] * 50
    stub.set_plan(plan)
    return pb


# --------------------------------------------------------------------------- #
# detach
# --------------------------------------------------------------------------- #
def test_detach_runs_to_completion(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "quick job"}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb), "--detach"], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pid=" in r.stdout

    assert wait_for(lambda: read_state(pb).get("next_index") == 1, timeout=60), \
        "detached run never completed; log:\n" + _log_text(pb)
    assert wait_for(lambda: "PLAYBOOK COMPLETE" in _log_text(pb), timeout=30)
    # pidfile is cleaned up on normal exit
    pid_file = pb.parent / "pb.logs" / "run.pid"
    assert wait_for(lambda: not pid_file.exists(), timeout=30)


# --------------------------------------------------------------------------- #
# pid lock: a second runner must refuse to share the playbook
# --------------------------------------------------------------------------- #
def test_second_runner_refuses_locked_playbook(stub, tmp_path):
    pb = _waiting_playbook(stub, tmp_path)
    proc = start_runner([str(pb)], tmp_path, stub.env())
    try:
        pid_file = pb.parent / "pb.logs" / "run.pid"
        assert wait_for(pid_file.exists, timeout=30)
        assert int(pid_file.read_text()) == proc.pid

        r2 = run_runner([str(pb)], tmp_path, stub.env())
        assert r2.returncode == 1
        assert "already has this playbook" in r2.stdout
        assert proc.poll() is None                 # original still running
    finally:
        (pb.parent / "pb.logs" / "stop.request").write_text("stop")
        proc.wait(timeout=60)


# --------------------------------------------------------------------------- #
# graceful stop via stop.request (works on Windows, no signals needed)
# --------------------------------------------------------------------------- #
def test_stop_request_interrupts_usage_wait(stub, tmp_path):
    pb = _waiting_playbook(stub, tmp_path, first_success=True)
    proc = start_runner([str(pb)], tmp_path, stub.env())
    try:
        assert wait_for(lambda: "USAGE LIMIT" in (proc and _log_text(pb)) or
                        read_state(pb).get("next_index") == 1, timeout=60)
        # parked in the wait loop now; ask it to stop
        (pb.parent / "pb.logs" / "stop.request").write_text("stop")
        assert proc.wait(timeout=60) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

    assert read_state(pb).get("next_index") == 1     # warmup done, job pending
    assert not (pb.parent / "pb.logs" / "run.pid").exists()
    assert not (pb.parent / "pb.logs" / "stop.request").exists()

    # resuming picks up at instruction 2 and finishes
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert read_state(pb).get("next_index") == 2


# --------------------------------------------------------------------------- #
# MCP core functions against a live run
# --------------------------------------------------------------------------- #
def test_mcp_start_status_stop_cycle(stub, tmp_path, monkeypatch):
    for k, v in stub.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CLAUDE_RUNNER", str(RUNNER))

    pb = _waiting_playbook(stub, tmp_path, first_success=True)

    st = playbook_mcp.core_status(str(pb))
    assert st["ok"] and st["phase"] == "not started" and st["total_instructions"] == 2

    started = playbook_mcp.core_start(str(pb))
    assert started["ok"], started
    pid = started["pid"]
    try:
        # double-start is rejected while running
        assert wait_for(lambda: playbook_mcp._alive(pid), timeout=30)
        again = playbook_mcp.core_start(str(pb))
        assert not again["ok"] and "Already running" in again["error"]

        # reaches the usage wait: 1 of 2 done, still running — and the status
        # check itself must not kill it (the old Windows bug)
        assert wait_for(lambda: playbook_mcp.core_status(str(pb))["done_instructions"] == 1,
                        timeout=60)
        # wait until prompt #2 has actually hit the limit and the runner is
        # parked in the wait — stopping earlier would race the log assertion below
        assert wait_for(lambda: any("USAGE LIMIT" in l for l in
                                    playbook_mcp.core_logs(str(pb), lines=50).get("lines", [])),
                        timeout=60)
        st = playbook_mcp.core_status(str(pb))
        assert st["phase"] == "running" and st["running"] is True
        assert playbook_mcp._alive(pid), "status check killed the runner!"

        stopped = playbook_mcp.core_stop(str(pb))
        assert stopped["ok"] and stopped["stopped"] is True
        assert wait_for(lambda: not playbook_mcp._alive(pid), timeout=30)
    finally:
        if playbook_mcp._alive(pid):
            playbook_mcp.core_stop(str(pb))

    st = playbook_mcp.core_status(str(pb))
    assert st["phase"] == "stopped" and st["running"] is False
    assert st["done_instructions"] == 1

    # logs tool returns content
    lg = playbook_mcp.core_logs(str(pb), lines=50)
    assert lg["ok"] and any("USAGE LIMIT" in l for l in lg["lines"])

    # resume to completion, status flips to complete
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    st = playbook_mcp.core_status(str(pb))
    assert st["phase"] == "complete" and st["done_instructions"] == 2


def test_mcp_scaffold(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_RUNNER", str(RUNNER))
    res = playbook_mcp.core_scaffold(str(tmp_path))
    assert res["ok"] and res["created"], res
    assert (tmp_path / "playbook.yaml").exists()
    res2 = playbook_mcp.core_scaffold(str(tmp_path))
    assert res2["ok"] and not res2["created"]


def test_mcp_stop_without_pid(tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "x"}])
    res = playbook_mcp.core_stop(str(pb))
    assert not res["ok"] and "nothing to stop" in res["error"].lower()
