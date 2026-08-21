"""Hostile-input and recovery robustness: malformed playbooks must produce
clear errors (never tracebacks), and interrupted/corrupted runs must recover."""
from __future__ import annotations

import json

from conftest import read_state, run_runner, write_playbook


def _clean_failure(r):
    """Non-zero exit, message on stderr, and no Python traceback anywhere."""
    assert r.returncode != 0
    assert "Traceback" not in r.stderr and "Traceback" not in r.stdout
    return r.stderr


# --------------------------------------------------------------------------- #
# Malformed playbooks -> clear startup errors
# --------------------------------------------------------------------------- #
def test_playbook_not_a_mapping(stub, tmp_path):
    pb = tmp_path / "pb.yaml"
    pb.write_text('"just a string"')
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "must be a YAML mapping" in err


def test_playbook_empty_file(stub, tmp_path):
    pb = tmp_path / "pb.yaml"
    pb.write_text("")
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "empty" in err


def test_playbook_invalid_yaml_syntax(stub, tmp_path):
    pb = tmp_path / "pb.yaml"
    pb.write_text("instructions: [unclosed\n")
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "not valid YAML" in err


def test_instructions_not_a_list(stub, tmp_path):
    pb = tmp_path / "pb.yaml"
    pb.write_text('instructions: "oops"\n')
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "'instructions' must be a list" in err


def test_instruction_bare_string(stub, tmp_path):
    pb = tmp_path / "pb.yaml"
    pb.write_text("instructions:\n  - just a string\n")
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "instruction #1" in err and "mapping" in err


def test_non_numeric_max_cost(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "x"}],
                        max_cost_usd="cheap")
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "max_cost_usd" in err


def test_non_numeric_limits_value(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "x"}],
                        limits={"max_gotos": "lots"})
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "limits.max_gotos" in err


def test_defaults_not_a_mapping(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "x"}],
                        defaults="oops")
    err = _clean_failure(run_runner([str(pb)], tmp_path, stub.env()))
    assert "'defaults' must be a mapping" in err


# --------------------------------------------------------------------------- #
# Runtime failures stay clean and follow on_fail
# --------------------------------------------------------------------------- #
def test_missing_prompt_file_fails_cleanly(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt_file": "./nope.md"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "Traceback" not in r.stderr and "Traceback" not in r.stdout
    assert "cannot read prompt_file" in r.stdout
    assert len(stub.calls()) == 0


def test_missing_prompt_file_respects_on_fail_continue(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt_file": "./nope.md", "on_fail": "continue"},
        {"prompt": "still runs"},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert [c["prompt"] for c in stub.calls()] == ["still runs"]


# --------------------------------------------------------------------------- #
# Recovery: corrupt state, resume, restart, --from
# --------------------------------------------------------------------------- #
def test_corrupt_state_file_starts_fresh(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "go"}])
    pb.with_suffix(".state.json").write_text("{ not json !!!")
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "starting fresh" in r.stdout
    assert read_state(pb)["next_index"] == 1


def test_failed_run_resumes_where_it_stopped(stub, tmp_path):
    stub.set_plan([{"kind": "success", "session_id": "sess-A"},
                   {"kind": "error"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "one"}, {"prompt": "two"},
    ])
    r1 = run_runner([str(pb)], tmp_path, stub.env())
    assert r1.returncode == 1
    assert read_state(pb)["next_index"] == 1          # stopped before #2

    stub.set_plan([{"kind": "success"}])
    r2 = run_runner([str(pb)], tmp_path, stub.env())
    assert r2.returncode == 0, r2.stdout + r2.stderr
    calls = stub.calls()                              # calls file accumulates
    assert [c["prompt"] for c in calls] == ["one", "two", "two"]
    # the resumed prompt threads the session from run 1
    assert any("sess-A" in " ".join(c["argv"]) for c in calls[2:])


def test_restart_flag_wipes_progress(stub, tmp_path):
    stub.set_plan([{"kind": "success"}, {"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "task"}])
    assert run_runner([str(pb)], tmp_path, stub.env()).returncode == 0
    r = run_runner([str(pb), "--restart"], tmp_path, stub.env())
    assert r.returncode == 0
    assert len(stub.calls()) == 2                     # ran again from scratch


def test_from_flag_skips_earlier_instructions(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"prompt": "one"}, {"prompt": "two"},
    ])
    r = run_runner([str(pb), "--from", "2"], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert [c["prompt"] for c in stub.calls()] == ["two"]


# --------------------------------------------------------------------------- #
# Environment robustness
# --------------------------------------------------------------------------- #
def test_notify_backend_without_credentials_does_not_crash(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "work"}, {"notify": "ping"}],
                        notify_backend="telegram", notify_on_finish=True)
    env = stub.env()
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)
    r = run_runner([str(pb)], tmp_path, env)
    assert r.returncode == 0, r.stdout + r.stderr     # notify fails soft
    assert "PLAYBOOK COMPLETE" in r.stdout


def test_unicode_prompt_survives_stdin(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    text = "Résumé überprüfen — 日本語のテスト ✓ €"
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": text}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert stub.calls()[0]["prompt"] == text


# --------------------------------------------------------------------------- #
# Unknown-key (typo) warnings
# --------------------------------------------------------------------------- #
def test_typo_key_warns_with_suggestion_but_runs(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "go", "on_fial": "continue"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr        # warn, don't block
    assert "unknown key 'on_fial'" in r.stderr
    assert "did you mean 'on_fail'" in r.stderr


def test_strict_makes_typo_fatal(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "go", "promt_file": "x.md"}])
    r = run_runner([str(pb), "--strict"], tmp_path, stub.env())
    assert r.returncode != 0
    assert "unknown key 'promt_file'" in r.stderr
    assert len(stub.calls()) == 0


def test_typos_in_all_sections_are_flagged(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "go"}],
                        sessoin="keep",
                        defaults={"cwd": str(tmp_path), "permision_mode": "dontAsk"},
                        limits={"poll_interval_sec": 1, "resume_poll_sec": 1,
                                "transient_base_sec": 0, "max_gotoz": 5})
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0
    assert "playbook: unknown key 'sessoin'" in r.stderr
    assert "defaults: unknown key 'permision_mode'" in r.stderr
    assert "limits: unknown key 'max_gotoz'" in r.stderr


def test_clean_playbook_has_no_warnings(stub, tmp_path):
    stub.set_plan([{"kind": "success"}])
    pb = write_playbook(tmp_path / "pb.yaml", [
        {"label": "a", "prompt": "go", "when": "exit 0", "on_fail": "goto a",
         "verify": "exit 0", "fix_attempts": 0},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "unknown key" not in r.stderr


def test_dry_run_also_warns(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "go", "labell": "x"}])
    r = run_runner([str(pb), "--dry-run"], tmp_path, stub.env())
    assert r.returncode == 0
    assert "unknown key 'labell'" in r.stderr
    assert "did you mean 'label'" in r.stderr
