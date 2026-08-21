"""Tamper-guard tests: `protect:` hashes matched files before an agent call and
compares them again before verify runs — any change/delete/creation is treated
exactly like a failed verify."""
from __future__ import annotations

from conftest import read_state, run_runner, write_playbook


def test_protect_untouched_file_passes(stub, tmp_path):
    (tmp_path / "secret.txt").write_text("original", encoding="utf-8")
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "protect": ["secret.txt"]}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PLAYBOOK COMPLETE" in r.stdout
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "original"


def test_protect_modified_file_fails_like_verify(stub, tmp_path):
    (tmp_path / "secret.txt").write_text("original", encoding="utf-8")
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "protect": ["secret.txt"]}])
    stub.set_plan([{"kind": "success", "write_files": {"secret.txt": "tampered"}}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "protected files tampered: secret.txt" in r.stdout
    assert "protected files were modified" in r.stdout
    assert read_state(pb).get("next_index", 0) == 0


def test_protect_deleted_file_fails(stub, tmp_path):
    (tmp_path / "secret.txt").write_text("original", encoding="utf-8")
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "protect": ["secret.txt"]}])
    stub.set_plan([{"kind": "success", "delete_files": ["secret.txt"]}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "protected files tampered: secret.txt" in r.stdout


def test_protect_new_matched_file_fails(stub, tmp_path):
    """A file newly created that matches the glob counts as tampering too."""
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "protect": ["*.lock"]}])
    stub.set_plan([{"kind": "success", "write_files": {"unexpected.lock": "sneaky"}}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "unexpected.lock" in r.stdout


def test_protect_glob_matches_multiple_files(stub, tmp_path):
    (tmp_path / "a.cfg").write_text("a", encoding="utf-8")
    (tmp_path / "b.cfg").write_text("b", encoding="utf-8")
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "protect": ["*.cfg"]}])
    stub.set_plan([{"kind": "success", "write_files": {"b.cfg": "changed"}}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "protected files tampered: b.cfg" in r.stdout
    assert "a.cfg" not in r.stdout.split("protected files tampered:")[1].split("\n")[0]


def test_protect_defaults_apply_to_all_instructions(stub, tmp_path):
    (tmp_path / "secret.txt").write_text("original", encoding="utf-8")
    pb = write_playbook(
        tmp_path / "pb.yaml",
        [{"prompt": "one"}, {"prompt": "two"}],
        defaults={"cwd": str(tmp_path), "permission_mode": "dontAsk",
                  "protect": ["secret.txt"]},
    )
    stub.set_plan([{"kind": "success"},
                   {"kind": "success", "write_files": {"secret.txt": "tampered"}}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 1
    assert "FAILED prompt #2" in r.stdout
    assert "protected files tampered: secret.txt" in r.stdout
    assert len(stub.calls()) == 2                # first prompt ran clean, second tampered


def test_protect_fix_attempt_restores_and_recovers(stub, tmp_path):
    """A fix round that actually restores the file is recognized as clean —
    the baseline is fixed at the start of the instruction, not re-taken per
    attempt, so restoring doesn't look like a fresh tamper."""
    (tmp_path / "secret.txt").write_text("original", encoding="utf-8")
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "protect": ["secret.txt"],
                          "fix_attempts": 1}])
    stub.claude.set_plan([
        {"kind": "success", "write_files": {"secret.txt": "tampered"}},
        {"kind": "success", "write_files": {"secret.txt": "original"}},
    ])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "protected files tampered — asking [claude] to fix it (fix 1/1)" in r.stdout
    calls = stub.claude.calls()
    assert len(calls) == 2
    assert "restore" in calls[1]["prompt"].lower()
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "original"


def test_protect_not_flagged_as_unknown_key(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "protect": ["secret.txt"]}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb), "--strict"], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "unknown key" not in r.stderr
