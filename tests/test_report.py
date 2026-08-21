"""Run-report tests: <playbook stem>.report.md is written next to the state
file at the end of every run (complete, hard failure, budget stop, or
stop.request), and a report bug must never affect the run's own exit code."""
from __future__ import annotations

import json

from conftest import read_state, run_runner, write_playbook

# CLAUDE_RUNNER_DETACHED is set by --detach children (and can leak in from an
# ambient parent runner in some shells) to suppress the runner.log file; clear
# it explicitly wherever a test needs that file for the report's log tail.
def _env(stub, **extra):
    return stub.env(CLAUDE_RUNNER_DETACHED="", **extra)


def report_path(pb):
    return pb.with_name(pb.stem + ".report.md")


def test_report_written_on_success_all_done(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "one"}, {"notify": "checkpoint"}, {"prompt": "two"}])
    stub.set_plan([{"kind": "success", "cost": 0.01}, {"kind": "success", "cost": 0.02}])
    r = run_runner([str(pb)], tmp_path, _env(stub))
    assert r.returncode == 0, r.stdout + r.stderr

    rp = report_path(pb)
    assert rp.exists()
    text = rp.read_text(encoding="utf-8")
    assert "**Outcome:** complete" in text
    assert "**Started:**" in text and "**Ended:**" in text and "**Duration:**" in text
    assert "| 1 | prompt | - | done |" in text
    assert "| 2 | notify | - | done |" in text
    assert "| 3 | prompt | - | done |" in text
    assert "**Total cost:** $0.0300" in text
    assert "## Last log lines" not in text        # only shown on failure


def test_report_overwritten_each_run(stub, tmp_path):
    """Re-running (e.g. after --restart) overwrites the old report, not append."""
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "one"}])
    stub.set_plan([{"kind": "success"}])
    r1 = run_runner([str(pb)], tmp_path, _env(stub))
    assert r1.returncode == 0, r1.stdout + r1.stderr
    first = report_path(pb).read_text(encoding="utf-8")

    stub.set_plan([{"kind": "success"}])
    r2 = run_runner([str(pb), "--restart"], tmp_path, _env(stub))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    second = report_path(pb).read_text(encoding="utf-8")
    assert "**Outcome:** complete" in second
    assert second.count("| 1 | prompt |") == 1     # not appended twice


def test_report_written_on_hard_failure_marks_failed_instruction(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "good"}, {"prompt": "bad"}, {"prompt": "never runs"}])
    stub.set_plan([{"kind": "success"}, {"kind": "error", "result": "boom"}])
    r = run_runner([str(pb)], tmp_path, _env(stub))
    assert r.returncode == 1

    text = report_path(pb).read_text(encoding="utf-8")
    assert "**Outcome:** failed at prompt #2" in text
    assert "| 1 | prompt | - | done |" in text
    assert "| 2 | prompt | - | failed |" in text
    assert "| 3 | prompt | - | pending |" in text
    assert "## Last log lines" in text
    assert "boom" in text.split("## Last log lines")[1]


def test_report_written_on_budget_stop(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "one"}, {"prompt": "two"}],
                        max_cost_usd=0.005)
    stub.set_plan([{"kind": "success", "cost": 0.01}, {"kind": "success", "cost": 0.01}])
    r = run_runner([str(pb)], tmp_path, _env(stub))
    assert r.returncode == 1

    text = report_path(pb).read_text(encoding="utf-8")
    assert "**Outcome:** failed (budget exhausted) before prompt #2" in text
    assert "| 1 | prompt | - | done |" in text
    assert "| 2 | prompt | - | pending |" in text
    assert "**Total cost:** $0.0100" in text


def test_report_written_on_stop_request(stub, tmp_path):
    """Park the runner in its usage-limit wait (checked every few seconds via
    sleep_with_heartbeat), then ask it to stop — same pattern as
    test_detach_stop.py's test_stop_request_interrupts_usage_wait."""
    from conftest import start_runner, wait_for

    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "one"}, {"prompt": "two"}],
                        limits={"poll_interval_sec": 3600, "resume_poll_sec": 3600,
                                "transient_base_sec": 0})
    stub.set_plan([{"kind": "usage"}] * 50)
    proc = start_runner([str(pb)], tmp_path, _env(stub))
    try:
        logs_dir = tmp_path / "pb.logs"
        assert wait_for(lambda: (logs_dir / "runner.log").exists()
                        and "USAGE LIMIT" in (logs_dir / "runner.log").read_text(), timeout=30)
        (logs_dir / "stop.request").write_text("stop")
        assert proc.wait(timeout=30) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)

    text = report_path(pb).read_text(encoding="utf-8")
    assert "**Outcome:** stopped (stop.request) before prompt #1" in text
    assert "| 1 | prompt | - | pending |" in text


def test_malformed_report_data_does_not_break_run(stub, tmp_path):
    """A corrupted `instr_status` in the state file (wrong type entirely) must
    not crash the run or change its exit code — the report write only logs."""
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "one"}])
    state_path = pb.with_suffix(".state.json")
    state_path.write_text(json.dumps({
        "next_index": 0, "sessions": {}, "session_id": None,
        "total_cost_usd": 0.0,
        "instr_status": "not-a-list",
    }))
    stub.set_plan([{"kind": "success", "cost": 0.01}])
    r = run_runner([str(pb)], tmp_path, _env(stub))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PLAYBOOK COMPLETE" in r.stdout
    assert "Traceback" not in r.stdout and "Traceback" not in r.stderr
    # the state file itself is repaired going forward
    assert isinstance(read_state(pb).get("instr_status"), list)


def test_malformed_report_row_entries_do_not_break_run(stub, tmp_path):
    """A structurally valid list with garbage per-instruction entries (wrong
    types inside, from e.g. a hand-edited or half-written state file) must
    still let the run complete normally and produce a report."""
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "one"}, {"prompt": "two"}])
    state_path = pb.with_suffix(".state.json")
    state_path.write_text(json.dumps({
        "next_index": 1, "sessions": {}, "session_id": None,
        "total_cost_usd": 0.01,
        "instr_status": [{"status": "done", "attempts": 1, "cost": "oops"}, "not-a-dict"],
    }))
    stub.set_plan([{"kind": "success", "cost": 0.02}])
    r = run_runner([str(pb)], tmp_path, _env(stub))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Traceback" not in r.stdout and "Traceback" not in r.stderr

    text = report_path(pb).read_text(encoding="utf-8")
    assert "**Outcome:** complete" in text
    assert "| 1 | prompt | - | done |" in text
    assert "| 2 | prompt | - | done |" in text
