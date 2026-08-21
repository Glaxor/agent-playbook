"""--watch: read-only attach to a run's progress + log stream."""
from __future__ import annotations

from pathlib import Path

from conftest import run_runner, start_runner, wait_for, write_playbook


# CLAUDE_RUNNER_DETACHED is set by --detach children (and can leak in from an
# ambient parent runner in some shells) to suppress the runner.log file; clear
# it explicitly wherever a test needs that file from a foreground run.
def _env(stub, **extra):
    return stub.env(CLAUDE_RUNNER_DETACHED="", **extra)


def _logs_dir(pb: Path) -> Path:
    return pb.parent / (pb.stem + ".logs")


def _pid_file(pb: Path) -> Path:
    return _logs_dir(pb) / "run.pid"


def _log_file(pb: Path) -> Path:
    return _logs_dir(pb) / "runner.log"


def _state_file(pb: Path) -> Path:
    return pb.with_suffix(".state.json")


def test_watch_completed_run_prints_summary_and_exits(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, _env(stub))
    assert r.returncode == 0, r.stdout + r.stderr

    w = run_runner([str(pb), "--watch"], tmp_path, _env(stub))
    assert w.returncode == 0, w.stdout + w.stderr
    assert "complete" in w.stdout
    assert "1/1" in w.stdout


def test_watch_no_run_says_so_and_exits(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}])
    assert not _state_file(pb).exists()
    assert not _logs_dir(pb).exists()

    w = run_runner([str(pb), "--watch"], tmp_path, _env(stub))
    assert w.returncode == 0, w.stdout + w.stderr
    assert "not started" in w.stdout
    assert "0/1" in w.stdout


def test_watch_streams_live_run_and_exits_on_its_own(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job1"}, {"prompt": "job2"}])
    # instruction #1 stays "in flight" for a few seconds so the watcher attaches
    # while the run is still going, then observes it finish on its own.
    stub.set_plan([{"kind": "hang", "sleep": 4}, {"kind": "success"}])

    r = run_runner([str(pb), "--detach"], tmp_path, _env(stub))
    assert r.returncode == 0, r.stdout + r.stderr
    pid_file = _pid_file(pb)
    assert wait_for(pid_file.exists, timeout=30)

    watch = start_runner([str(pb), "--watch"], tmp_path, _env(stub))
    try:
        out, _ = watch.communicate(timeout=90)
    finally:
        if watch.poll() is None:
            watch.kill()
            watch.wait(timeout=30)

    assert watch.returncode == 0, out
    assert "running" in out            # attached while the run was still going
    assert "DONE prompt #1" in out     # streamed a line that appeared mid-watch
    assert "complete" in out           # final status line, printed once it finished


def test_watch_never_writes_state_pid_or_log(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}])
    stub.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, _env(stub))
    assert r.returncode == 0, r.stdout + r.stderr

    state_path, log_path, pid_path = _state_file(pb), _log_file(pb), _pid_file(pb)
    assert state_path.exists() and log_path.exists()
    assert not pid_path.exists()        # cleaned up on normal exit

    before_state, before_state_mtime = state_path.read_text(), state_path.stat().st_mtime_ns
    before_log, before_log_mtime = log_path.read_text(), log_path.stat().st_mtime_ns

    w = run_runner([str(pb), "--watch"], tmp_path, _env(stub))
    assert w.returncode == 0, w.stdout + w.stderr

    assert state_path.read_text() == before_state
    assert state_path.stat().st_mtime_ns == before_state_mtime
    assert log_path.read_text() == before_log
    assert log_path.stat().st_mtime_ns == before_log_mtime
    assert not pid_path.exists()        # watch must not (re)create it
