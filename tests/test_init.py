"""Tests for `agent-playbook init` — the small, runnable starter scaffold."""
from __future__ import annotations

from conftest import run_runner


def test_init_creates_file_and_strict_validates(stub, tmp_path):
    r = run_runner(["init"], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    pb = tmp_path / "playbook.yaml"
    assert pb.exists()

    # --dry-run --strict is the playbook validator: it must accept the
    # starter as-is, with zero unknown-key warnings, so a brand-new user's
    # first run succeeds without editing anything.
    r2 = run_runner([str(pb), "--dry-run", "--strict"], tmp_path, stub.env())
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "warning" not in r2.stderr
    assert "Dry run complete" in r2.stdout


def test_init_refuses_to_overwrite(stub, tmp_path):
    r1 = run_runner(["init"], tmp_path, stub.env())
    assert r1.returncode == 0, r1.stdout + r1.stderr
    pb = tmp_path / "playbook.yaml"
    original = pb.read_text(encoding="utf-8")

    r2 = run_runner(["init"], tmp_path, stub.env())
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "already exists" in r2.stdout
    assert pb.read_text(encoding="utf-8") == original   # untouched


def test_init_prints_exact_next_steps(stub, tmp_path):
    r = run_runner(["init"], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [l.rstrip() for l in r.stdout.splitlines()]
    assert "  agent-playbook playbook.yaml --dry-run" in lines
    assert "  agent-playbook playbook.yaml" in lines
    # --dry-run must be told to the user before the real run
    dry_idx = lines.index("  agent-playbook playbook.yaml --dry-run")
    run_idx = lines.index("  agent-playbook playbook.yaml")
    assert dry_idx < run_idx
