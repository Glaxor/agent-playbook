#!/usr/bin/env python3
"""
playbook_mcp.py — expose claude_runner as Claude Code tools (local stdio MCP server).

Tools:
    scaffold_playbook(path=".")   -> write a starter playbook the user can edit
    start_playbook(path)          -> launch the runner DETACHED, return pid + logfile
    playbook_status(path)         -> progress: which instruction, running?, cost, log tail
    stop_playbook(path)           -> stop a running playbook

Design note: a playbook can wait out usage-limit windows for hours, so start_playbook
is fire-and-forget — it launches a detached run (its own process group, survives this
server and the calling session) and returns immediately. Use playbook_status to check
on it. This keeps the calling Claude session from blocking (and burning its own usage)
while the run waits.

REGISTER (run in a terminal, not inside Claude Code):
    claude mcp add --scope user --transport stdio claude-playbook \
        -- python3 /ABSOLUTE/PATH/playbook_mcp.py
Then in any Claude Code session: "scaffold a playbook", "start playbook.yaml",
"what's the status of playbook.yaml".

REQUIRES
    pip install mcp pyyaml
    claude_runner.py next to this file, or `claude-runner` on PATH, or set
    CLAUDE_RUNNER env var to the runner's path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


# --------------------------------------------------------------------------- #
# Locating the runner
# --------------------------------------------------------------------------- #
def runner_cmd() -> list[str]:
    """How to invoke the runner: explicit env, installed command, or sibling script."""
    if os.environ.get("CLAUDE_RUNNER"):
        p = os.path.expanduser(os.environ["CLAUDE_RUNNER"])
        return [sys.executable, p] if p.endswith(".py") else [p]
    for exe in ("agent-playbook", "claude-runner"):
        if shutil.which(exe):
            return [exe]
    sibling = Path(__file__).resolve().parent / "claude_runner.py"
    if sibling.exists():
        return [sys.executable, str(sibling)]
    raise FileNotFoundError(
        "Can't find the runner. Install `agent-playbook` (pipx install .), put "
        "claude_runner.py next to playbook_mcp.py, or set CLAUDE_RUNNER."
    )


def _paths(playbook: str) -> tuple[Path, Path, Path, Path]:
    pb = Path(os.path.expanduser(playbook or "playbook.yaml")).resolve()
    logs = pb.parent / (pb.stem + ".logs")
    return pb, pb.with_suffix(".state.json"), logs, logs / "run.pid"


def _alive(pid: int) -> bool:
    """True if the pid is running. Must never signal the process: os.kill(pid, 0)
    on Windows calls TerminateProcess — the old code KILLED the runner on status
    checks."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # process exists but isn't ours
    except Exception:
        return False


def _read_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _count_instructions(pb: Path) -> int | None:
    if not (yaml and pb.exists()):
        return None
    try:
        data = yaml.safe_load(pb.read_text(encoding="utf-8")) or {}
        return len(data.get("instructions", []))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Core operations (plain functions so they're testable without the mcp package)
# --------------------------------------------------------------------------- #
def core_scaffold(path: str = ".") -> dict:
    target = Path(os.path.expanduser(path or "."))
    if target.is_dir() or path in ("", "."):
        target = target / "playbook.yaml"
    if target.exists():
        return {"ok": True, "created": False, "path": str(target),
                "message": f"{target} already exists; edit it, don't overwrite."}
    cmd = runner_cmd()
    # Runner scaffolds when given no playbook; it writes ./playbook.yaml in its cwd.
    # stdin=DEVNULL: the child must not inherit this server's stdin (the MCP stdio
    # pipe) — a pending read on that shared handle deadlocks the child's Python
    # startup on Windows (sys.stdin init seeks the handle, sync pipe I/O serializes).
    subprocess.run(cmd, cwd=str(target.parent), capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    ok = target.exists()
    if not ok:
        return {"ok": False, "created": False, "path": str(target),
                "error": f"Runner did not create {target}."}
    return {"ok": True, "created": True, "path": str(target),
            "message": f"Created {target}. Edit the instructions, then start_playbook."}


def core_handoff(path: str = ".") -> dict:
    target = Path(os.path.expanduser(path or ".")).resolve()
    if target.is_dir() or path in ("", "."):
        target = target / "playbook.yaml"
    if target.exists():
        return {"ok": True, "created": False, "path": str(target),
                "message": f"{target} already exists; not overwriting."}
    res = subprocess.run([*runner_cmd(), str(target), "--handoff"],
                         cwd=str(target.parent), capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
    out = (res.stdout or "") + (res.stderr or "")
    if not target.exists():
        return {"ok": False, "error": out.strip() or "handoff failed"}
    m = re.search(r"session (\S+)", out)
    return {"ok": True, "created": True, "path": str(target),
            "session_id": m.group(1) if m else None,
            "message": "Captured the current session. Quit this session, then "
                       "start_playbook (or `agent-playbook <path> --detach`). Don't run "
                       "it while this interactive session is still open — they'd share "
                       "one session."}


def core_start(path: str) -> dict:
    pb, state, logs, pidfile = _paths(path)
    if not pb.exists():
        return {"ok": False, "error": f"No such playbook: {pb}. Run scaffold_playbook first."}

    # already running?
    existing = _read_pid(pidfile)
    if existing and _alive(existing):
        return {"ok": False, "error": f"Already running (pid {existing}).",
                "pid": existing, "logfile": str(logs / "runner.log")}
    if existing:
        # Stale pidfile from a dead run. The runner's single-runner lock ignores
        # dead pids, but clear it so the poll below can't report the old pid.
        try:
            pidfile.unlink()
        except OSError:
            pass

    cmd = [*runner_cmd(), str(pb), "--detach"]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
    out = (res.stdout or "") + (res.stderr or "")
    m = re.search(r"pid=(\d+)", out)
    if not m:
        return {"ok": False, "error": "Runner did not report a pid.", "output": out.strip()}
    pid = int(m.group(1))
    # The pidfile belongs to the runner: it writes its own pid into run.pid on
    # startup. Writing the printed pid here would trip the runner's single-runner
    # lock against itself — under a pipx/venv redirector the printed pid is a
    # wrapper process, not the runner, and it stays alive while the runner runs.
    # Poll briefly so we can report the runner's real pid when it lands.
    for _ in range(20):
        real = _read_pid(pidfile)
        if real:
            pid = real
            break
        time.sleep(0.15)
    return {"ok": True, "pid": pid, "playbook": str(pb),
            "logfile": str(logs / "runner.log"),
            "message": f"Started in background (pid {pid}). Use playbook_status to check."}


def core_status(path: str, tail: int = 8) -> dict:
    pb, state, logs, pidfile = _paths(path)
    if not pb.exists():
        return {"ok": False, "error": f"No such playbook: {pb}"}

    total = _count_instructions(pb)
    st = {}
    if state.exists():
        try:
            st = json.loads(state.read_text(encoding="utf-8"))
        except Exception:
            pass
    next_index = st.get("next_index", 0)

    pid = _read_pid(pidfile)
    running = bool(pid and _alive(pid))

    if total is not None and total > 0 and next_index >= total:
        phase, running, pid = "complete", False, None
    elif running:
        phase = "running"
    elif next_index > 0:
        phase = "stopped"      # progressed then not running
    else:
        phase = "not started"

    logfile = logs / "runner.log"
    last_lines = []
    if logfile.exists():
        last_lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, tail):]

    return {
        "ok": True, "phase": phase, "running": running, "pid": pid if running else None,
        "done_instructions": next_index, "total_instructions": total,
        "cost_usd": st.get("total_cost_usd", 0.0),
        "session_id": st.get("session_id"),
        "sessions": st.get("sessions", {}),
        "log_tail": last_lines, "logfile": str(logfile),
    }


def core_logs(path: str, lines: int = 100, contains: str | None = None) -> dict:
    pb, state, logs, pidfile = _paths(path)
    logfile = logs / "runner.log"
    if not logfile.exists():
        return {"ok": False, "error": f"No log yet at {logfile}"}
    rows = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
    if contains:
        rows = [r for r in rows if contains.lower() in r.lower()]
    tail = rows[-max(1, lines):]
    return {"ok": True, "logfile": str(logfile), "returned": len(tail),
            "matched_total": len(rows), "lines": tail}


def core_stop(path: str) -> dict:
    pb, state, logs, pidfile = _paths(path)
    pid = _read_pid(pidfile)
    if not pid:
        return {"ok": False, "error": "No recorded pid; nothing to stop."}
    if not _alive(pid):
        return {"ok": True, "stopped": False, "message": f"pid {pid} is not running."}
    try:
        # Graceful path first: the runner watches for this file while it waits/polls
        # (the runner clears any stale one at startup).
        try:
            (logs / "stop.request").write_text("stop", encoding="utf-8")
        except Exception:
            pass
        if os.name == "nt":
            # No SIGTERM delivery on Windows; kill the tree (runner + its claude
            # child). State is saved at instruction boundaries, so resume is safe.
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGTERM)   # runner flushes state on SIGTERM
        return {"ok": True, "stopped": True, "pid": pid,
                "message": f"Sent stop to pid {pid}. Progress is saved; "
                           f"start_playbook resumes where it left off."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# MCP layer (only wired up when the mcp package is present)
# --------------------------------------------------------------------------- #
def build_server():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("claude-playbook")

    @mcp.tool()
    def scaffold_playbook(path: str = ".") -> dict:
        """Create a blank starter playbook.yaml the user can edit. `path` may be a
        directory or a filename; defaults to ./playbook.yaml. Won't overwrite."""
        return core_scaffold(path)

    @mcp.tool()
    def handoff_session(path: str = ".") -> dict:
        """Capture the CURRENT Claude Code session into a ready-to-run playbook pinned to
        its session id, with a 'continue where you left off' prompt. Use this from inside a
        session that's about to hit the usage limit, so the runner can finish it unattended.
        After it returns: quit this session, then start_playbook."""
        return core_handoff(path)

    @mcp.tool()
    def start_playbook(path: str) -> dict:
        """Start a playbook running in the background (detached, survives this session).
        Returns the pid and logfile. Use playbook_status to monitor it. The run waits out
        usage-limit windows and continues automatically."""
        return core_start(path)

    @mcp.tool()
    def playbook_status(path: str, tail: int = 8) -> dict:
        """Report a playbook's progress: phase (running/complete/stopped), how many
        instructions are done out of the total, running cost, and the last log lines."""
        return core_status(path, tail)

    @mcp.tool()
    def stop_playbook(path: str) -> dict:
        """Stop a running playbook. Progress is saved; start_playbook resumes it."""
        return core_stop(path)

    @mcp.tool()
    def playbook_logs(path: str, lines: int = 100, contains: str = "") -> dict:
        """Return a longer slice of a playbook's log on demand. `lines` caps how many
        trailing lines to return; `contains` filters to matching lines (case-insensitive)."""
        return core_logs(path, lines, contains or None)

    return mcp


if __name__ == "__main__":
    build_server().run()   # stdio transport by default
