"""Safety-net tests: per-prompt timeout, budget caps, attempt caps, and the
self-healing verify loop (v0.3 features)."""
from __future__ import annotations

import sys

from conftest import read_state, run_runner, write_playbook

# A verify command that fails on its first run and passes from the second on
# (counts runs in cnt.txt). Built without f-string escapes for Python 3.10.
_COUNTER_CODE = ("import os,sys; f='cnt.txt'; "
                 "n=int(open(f).read()) if os.path.exists(f) else 0; "
                 "open(f,'w').write(str(n+1)); sys.exit(0 if n>=1 else 1)")
VERIFY_FAIL_ONCE = '"%s" -c "%s"' % (sys.executable, _COUNTER_CODE)


# --------------------------------------------------------------------------- #
# per-prompt timeout
# --------------------------------------------------------------------------- #
def test_timeout_kills_and_retries_then_succeeds(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "timeout_min": 0.1}])   # 6s cap
    stub.claude.set_plan([{"kind": "hang", "sleep": 60}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env(), timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "timed out — retrying (1/1)" in r.stdout
    assert "PLAYBOOK COMPLETE" in r.stdout


def test_persistent_hang_fails_the_prompt(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "timeout_min": 0.1}])
    stub.claude.set_plan([{"kind": "hang", "sleep": 60},
                          {"kind": "hang", "sleep": 60}])
    r = run_runner([str(pb)], tmp_path, stub.env(), timeout=180)
    assert r.returncode == 1
    assert "no result after" in r.stdout          # timeout message surfaced
    assert read_state(pb).get("next_index", 0) == 0


# --------------------------------------------------------------------------- #
# self-healing verify (fix_attempts)
# --------------------------------------------------------------------------- #
def test_fix_attempt_feeds_verify_failure_back_and_passes(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "build it",
                          "verify": VERIFY_FAIL_ONCE, "fix_attempts": 2}])
    stub.claude.set_plan([{"kind": "success"}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verify failed — asking [claude] to fix it (fix 1/2)" in r.stdout
    calls = stub.claude.calls()
    assert len(calls) == 2
    assert "build it" in calls[0]["prompt"]
    assert "verification command" in calls[1]["prompt"]     # failure fed back
    assert (tmp_path / "cnt.txt").read_text() == "2"        # verify ran twice
    # the fix went to the SAME threaded session
    i = calls[1]["argv"].index("--resume")
    assert calls[1]["argv"][i + 1] == "stub-session-0001"


def test_fix_attempts_exhausted_fails(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "verify": "exit 1", "fix_attempts": 1}])
    stub.claude.set_plan([{"kind": "success"}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert len(stub.claude.calls()) == 2          # original + one fix attempt
    assert "verify command failed" in r.stdout


def test_no_fix_attempts_keeps_old_behavior(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "verify": "exit 1"}])
    stub.claude.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert len(stub.claude.calls()) == 1          # no fix round-trips


# --------------------------------------------------------------------------- #
# budget caps
# --------------------------------------------------------------------------- #
def test_budget_stops_between_prompts(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}],
                        max_cost_usd=0.015)
    stub.claude.set_plan([{"kind": "success", "cost": 0.01}] * 3)
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "BUDGET STOP" in r.stdout
    assert read_state(pb).get("next_index") == 2   # stopped before prompt #3
    assert len(stub.claude.calls()) == 2


def test_budget_stops_mid_prompt_fix_loop(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "verify": "exit 1", "fix_attempts": 10}],
                        max_cost_usd=0.005)
    stub.claude.set_plan([{"kind": "success", "cost": 0.01}] * 12)
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "budget exhausted" in r.stdout
    assert len(stub.claude.calls()) == 1           # stopped before the 2nd attempt


# --------------------------------------------------------------------------- #
# attempt cap (runaway guard)
# --------------------------------------------------------------------------- #
def test_max_attempts_stops_runaway_retry_loop(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "max_attempts": 2}])
    stub.claude.set_plan([{"kind": "usage"}] * 10)
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "max_attempts (2) reached" in r.stdout
    assert len(stub.claude.calls()) == 2
