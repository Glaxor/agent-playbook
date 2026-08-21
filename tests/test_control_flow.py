"""Control flow: on_fail (stop/continue/goto), label, and when gates."""
from __future__ import annotations

from conftest import read_state, run_runner, write_playbook

FAST_LIMITS = {"poll_interval_sec": 1, "resume_poll_sec": 1, "transient_base_sec": 0}


def test_on_fail_continue_moves_on(stub, tmp_path):
    stub.set_plan([{"kind": "error"}, {"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "one", "on_fail": "continue"},
        {"prompt": "two"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "on_fail: continue" in r.stdout
    assert len(stub.calls()) == 2
    assert read_state(pb)["next_index"] == 2


def test_on_fail_goto_jumps_to_label(stub, tmp_path):
    stub.set_plan([{"kind": "error"}, {"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "build", "on_fail": "goto cleanup"},
        {"prompt": "never runs"},
        {"label": "cleanup", "prompt": "clean up"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    calls = stub.calls()
    assert [c["prompt"] for c in calls] == ["build", "clean up"]


def test_goto_loop_guard_stops(stub, tmp_path):
    stub.set_plan([{"kind": "error"}] * 10)
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"label": "top", "prompt": "always fails", "on_fail": "goto top"},
    ], limits={**FAST_LIMITS, "max_gotos": 3})
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "goto limit reached" in r.stdout
    assert len(stub.calls()) == 4          # first try + 3 allowed jumps


def test_default_on_fail_still_stops(stub, tmp_path):
    stub.set_plan([{"kind": "error"}, {"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "one"},
        {"prompt": "two"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "Stopping on failure." in r.stdout
    assert len(stub.calls()) == 1


def test_verify_failure_respects_on_fail_continue(stub, tmp_path):
    stub.set_plan([{"kind": "success"}, {"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "one", "verify": "exit 1", "fix_attempts": 0,
         "on_fail": "continue"},
        {"prompt": "two"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(stub.calls()) == 2


def test_when_gate_skips_and_passes(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "skipped", "when": "exit 1"},
        {"prompt": "runs", "when": "exit 0"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    calls = stub.calls()
    assert [c["prompt"] for c in calls] == ["runs"]
    assert "skipped" in r.stdout


def test_when_gate_on_notify(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"notify": "never sent", "when": "exit 1"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped" in r.stdout
    assert "notify: never sent" not in r.stdout


def test_unknown_goto_label_fails_fast(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "x", "on_fail": "goto nowhere"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode != 0
    assert "on_fail" in r.stderr
    assert len(stub.calls()) == 0          # validation runs before any agent call


def test_duplicate_label_fails_fast(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"label": "a", "prompt": "one"},
        {"label": "a", "prompt": "two"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode != 0
    assert "Duplicate label" in r.stderr
    assert len(stub.calls()) == 0


def test_dry_run_shows_flow_annotations(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"label": "top", "prompt": "one", "on_fail": "goto top", "when": "exit 0"},
    ])
    r = run_runner([str(pb), "--dry-run"], tmp_path, stub.env())
    assert r.returncode == 0
    assert "[label: top]" in r.stdout
    assert "[on_fail: goto top]" in r.stdout
    assert "[when: exit 0]" in r.stdout
    assert len(stub.calls()) == 0
