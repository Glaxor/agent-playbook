"""End-to-end runner tests against the stub claude binary (no real usage burned)."""
from __future__ import annotations

import claude_runner

from conftest import read_state, run_runner, write_playbook


def _patch_env(stub, monkeypatch):
    monkeypatch.setenv("CLAUDE_RUNNER_HOME", str(stub.home))
    for s in (stub.claude, stub.codex, stub.gemini):
        for k, v in s.env_vars().items():
            monkeypatch.setenv(k, v)


# --------------------------------------------------------------------------- #
# claude adapter classification against the stub
# --------------------------------------------------------------------------- #
def test_claude_success_mentioning_rate_limits_is_not_a_limit(stub, tmp_path, monkeypatch):
    """A SUCCESSFUL result whose text talks about rate limiting must not be
    classified as rate-limited (regression: classify ran on success output)."""
    _patch_env(stub, monkeypatch)
    stub.set_plan([{"kind": "success",
                    "result": "Implemented API rate limiting; usage limit reached tests pass."}])
    res = claude_runner.ADAPTERS["claude"].run("add rate limiting", {}, None, str(tmp_path))
    assert res["ok"] is True
    assert res["limit"] == claude_runner.Limit.NONE
    assert res["session_id"] == "stub-session-0001"


def test_claude_usage_limit_detected(stub, tmp_path, monkeypatch):
    _patch_env(stub, monkeypatch)
    stub.set_plan([{"kind": "usage"}])
    res = claude_runner.ADAPTERS["claude"].run("do work", {}, None, str(tmp_path))
    assert res["ok"] is False
    assert res["limit"] == claude_runner.Limit.USAGE


def test_claude_transient_detected(stub, tmp_path, monkeypatch):
    _patch_env(stub, monkeypatch)
    stub.set_plan([{"kind": "transient"}])
    res = claude_runner.ADAPTERS["claude"].run("do work", {}, None, str(tmp_path))
    assert res["ok"] is False
    assert res["limit"] == claude_runner.Limit.TRANSIENT


def test_prompt_travels_via_stdin(stub, tmp_path, monkeypatch):
    """Long prompts with quotes/newlines must reach claude intact (stdin, not argv)."""
    _patch_env(stub, monkeypatch)
    stub.set_plan([{"kind": "success"}])
    nasty = 'line one\nline "two" with %PATH% and $HOME and a caret ^\n' + ("x" * 40000)
    res = claude_runner.ADAPTERS["claude"].run(nasty, {}, None, str(tmp_path))
    assert res["ok"] is True
    assert stub.calls()[0]["prompt"] == nasty


# --------------------------------------------------------------------------- #
# whole-playbook runs
# --------------------------------------------------------------------------- #
def test_scaffold_when_no_playbook_given(stub, tmp_path):
    r = run_runner([], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "playbook.yaml").exists()
    r2 = run_runner([], tmp_path, stub.env())
    assert r2.returncode == 0 and "already exists" in r2.stdout


def test_dry_run_executes_nothing(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "step one"}, {"notify": "hi"}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb), "--dry-run"], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Dry run complete" in r.stdout
    assert stub.calls() == []                       # claude never invoked
    assert not pb.with_suffix(".state.json").exists()


def test_happy_path_threads_one_session(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "step one"}, {"prompt": "step two"}, {"notify": "done"}])
    stub.set_plan([{"kind": "success", "session_id": "stub-session-0001"},
                   {"kind": "success", "session_id": "stub-session-0001"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PLAYBOOK COMPLETE" in r.stdout

    state = read_state(pb)
    assert state["next_index"] == 3
    assert state["session_id"] == "stub-session-0001"
    assert abs(state["total_cost_usd"] - 0.02) < 1e-6

    calls = stub.calls()
    assert len(calls) == 2
    assert "step one" in calls[0]["prompt"]
    assert "--resume" not in calls[0]["argv"]       # first prompt: new session
    i = calls[1]["argv"].index("--resume")          # second prompt: threaded
    assert calls[1]["argv"][i + 1] == "stub-session-0001"

    # re-running a finished playbook is a no-op
    r2 = run_runner([str(pb)], tmp_path, stub.env())
    assert r2.returncode == 0 and "Nothing to do" in r2.stdout


def test_usage_limit_waits_then_continues(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "long job"}])
    stub.set_plan([{"kind": "usage"}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "USAGE LIMIT" in r.stdout
    assert "PLAYBOOK COMPLETE" in r.stdout
    assert len(stub.calls()) == 2
    logs = pb.parent / "pb.logs"
    assert (logs / "prompt01.attempt1.log").exists()
    assert (logs / "prompt01.attempt2.log").exists()


def test_usage_limit_stops_when_continue_disabled(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}],
                        continue_on_limit=False)
    stub.set_plan([{"kind": "usage"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "continue_on_limit is disabled" in r.stdout
    assert read_state(pb).get("next_index", 0) == 0


def test_transient_backoff_then_success(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}])
    stub.set_plan([{"kind": "transient"}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "transient limit; backoff" in r.stdout
    assert len(stub.calls()) == 2


def test_transient_retries_exhausted(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}],
                        limits={"transient_base_sec": 0, "transient_max_retries": 2,
                                "poll_interval_sec": 1, "resume_poll_sec": 1})
    stub.set_plan([{"kind": "transient"}] * 3)
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "Exceeded transient rate-limit retries" in r.stdout


def test_hard_error_stops_playbook(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}, {"prompt": "never runs"}])
    stub.set_plan([{"kind": "error", "result": "something exploded"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "FAILED prompt #1" in r.stdout
    assert len(stub.calls()) == 1
    assert read_state(pb).get("next_index", 0) == 0    # resume re-runs prompt 1


def test_verify_gate_fails_prompt(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "verify": "exit 1"}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "verify" in r.stdout.lower()
    assert read_state(pb).get("next_index", 0) == 0


def test_env_file_next_to_playbook_is_loaded(stub, tmp_path):
    """A .env beside the playbook feeds env vars to the run (verify sees them)."""
    import sys as _sys
    check = (f'"{_sys.executable}" -c "import os,sys; '
             f"sys.exit(0 if os.environ.get('PB_ENV_TEST')=='works' else 1)\"")
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job", "verify": check}])
    (tmp_path / ".env").write_text(
        "# comment line\n\nexport PB_ENV_TEST='works'\nIGNORED_NO_EQUALS\n",
        encoding="utf-8")
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[env] loaded" in r.stdout


def test_env_file_never_overrides_real_env(stub, tmp_path):
    import sys as _sys
    check = (f'"{_sys.executable}" -c "import os,sys; '
             f"sys.exit(0 if os.environ.get('PB_ENV_TEST')=='real' else 1)\"")
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job", "verify": check}])
    (tmp_path / ".env").write_text("PB_ENV_TEST=fromfile\n", encoding="utf-8")
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env(PB_ENV_TEST="real"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_verify_gate_passes(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "verify": "exit 0"}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert read_state(pb).get("next_index") == 1
