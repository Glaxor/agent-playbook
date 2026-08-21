#!/usr/bin/env python3
"""
claude_runner.py — run an ordered playbook of Claude Code instructions.

The playbook is a flat list. Each instruction is one of:
    - prompt:  run it as a Claude Code prompt (full agent, tools, etc.)
    - notify:  send a message via the configured backend (telegram | ntfy)
It walks the list top to bottom:
    prompt   -> execute it
    notify   -> send the notification
    (end)    -> stop

LIMITS
    If a running prompt hits the usage-limit wall, the runner waits until the window
    resets and then *continues the same prompt* (resuming the threaded session) the
    moment the window reopens — no manual restart. Transient server limits get a short
    backoff.

USAGE
    agent-playbook playbook.yaml                # run
    agent-playbook playbook.yaml --detach       # background, survives logout
    agent-playbook playbook.yaml --dry-run      # print the plan, run nothing
    agent-playbook playbook.yaml --restart      # ignore saved progress, start over
    agent-playbook playbook.yaml --from 3       # start at instruction #3 (1-based)

NOTES
    * Does NOT use --bare and passes no API key, so the run uses your OAuth login and
      bills against your Max subscription rather than per-token API billing.
    * Secrets come from env vars, never the playbook.

REQUIRES
    pip install pyyaml
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency. Run: pip install pyyaml")


# --------------------------------------------------------------------------- #
# Rate-limit classification
# --------------------------------------------------------------------------- #
USAGE_LIMIT_RE = re.compile(r"usage limit reached|limit will reset at", re.I)
TRANSIENT_RE = re.compile(
    r"temporarily limiting requests|not your usage limit|rate limit(?:ed)?", re.I
)
RESET_RE = re.compile(
    r"reset at\s+(?:approximately\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*"
    r"\(?([A-Za-z]+/[A-Za-z_]+)\)?",
    re.I,
)


class Limit:
    NONE = "none"
    USAGE = "usage"
    TRANSIENT = "transient"


def classify_limit(text: str) -> str:
    if not text:
        return Limit.NONE
    if USAGE_LIMIT_RE.search(text):
        return Limit.USAGE
    if TRANSIENT_RE.search(text):
        return Limit.TRANSIENT
    return Limit.NONE


def parse_reset_seconds(text: str, cap_sec: int) -> int | None:
    """Turn 'reset at 3pm (America/Santiago)' into seconds to sleep. None if unparseable."""
    m = RESET_RE.search(text or "")
    if not m:
        return None
    try:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").replace(".", "").lower()
        tz = ZoneInfo(m.group(4))
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        now = dt.datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        secs = int((target - now).total_seconds()) + 30  # small buffer past reset
        return max(0, min(secs, cap_sec))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Process helpers (portable across POSIX and Windows)
# --------------------------------------------------------------------------- #
def pid_alive(pid: int | None) -> bool:
    """True if a process with this pid is running. Never signals the process:
    os.kill(pid, 0) on Windows calls TerminateProcess and would KILL it."""
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


def load_env_file(path: Path) -> int:
    """Load KEY=VALUE lines from a .env file into os.environ. Existing (real)
    env vars always win. Supports comments, blank lines, an optional `export `
    prefix, and single/double quoted values. Returns how many vars were set."""
    if not path.exists():
        return 0
    n = 0
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
                n += 1
    except Exception as e:
        log(f"[env] could not read {path}: {e}")
    return n


class StopRequested(Exception):
    """A stop.request file appeared; save state and exit cleanly."""


_STOP_FILE: Path | None = None   # set in main(); gives Windows a graceful stop path


def check_stop() -> None:
    if _STOP_FILE is not None and _STOP_FILE.exists():
        raise StopRequested()


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
class Notifier:
    def __init__(self, backend: str, cfg: dict):
        self.backend = (backend or "none").lower()
        self.cfg = cfg

    def notify(self, text: str, title: str = "agent-playbook") -> bool:
        try:
            if self.backend == "telegram":
                return self._telegram(f"{title}\n{text}".strip())
            if self.backend == "ntfy":
                return self._ntfy(title, text or title)
            log(f"[notify:none] {text}")  # no backend -> just log it
            return True
        except Exception as e:
            log(f"[notify] {self.backend} failed: {e}")
            return False

    def _telegram(self, msg: str) -> bool:
        token = os.environ.get(self.cfg.get("telegram_bot_token_env", "TELEGRAM_BOT_TOKEN"))
        chat = os.environ.get(self.cfg.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID"))
        if not token or not chat:
            log("[notify] telegram TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping")
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15).read()
        return True

    def _ntfy(self, title: str, body: str) -> bool:
        topic = os.environ.get("NTFY_TOPIC") or self.cfg.get("ntfy_topic")
        if not topic:
            log("[notify] ntfy topic (NTFY_TOPIC) not set; skipping")
            return False
        base = self.cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{base}/{topic}", data=body.encode("utf-8"), headers={"Title": title[:200]}
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
_LOG_FH = None


def log(line: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    out = f"{stamp}  {line}"
    print(out, flush=True)
    if _LOG_FH:
        _LOG_FH.write(out + "\n")
        _LOG_FH.flush()


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def fresh_state() -> dict:
    return {"next_index": 0, "sessions": {}, "session_id": None, "total_cost_usd": 0.0}


def load_state(path: Path) -> dict:
    state = None
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log(f"[state] {path} unreadable; starting fresh")
    if not isinstance(state, dict):
        return fresh_state()
    # migrate pre-multi-agent state: session_id was Claude's threaded session
    state.setdefault("sessions", {})
    if state.get("session_id") and not state["sessions"].get("claude"):
        state["sessions"]["claude"] = state["session_id"]
    return state


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Session discovery — find ids of sessions created interactively (or anywhere)
# --------------------------------------------------------------------------- #
def _sessions_dir() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


def _all_session_files() -> list[Path]:
    base = _sessions_dir()
    if not base.exists():
        return []
    return sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def _cwd_slug(cwd: str | None) -> str:
    # Claude Code names a project dir after the abs cwd with separators replaced by '-'.
    path = os.path.abspath(os.path.expanduser(cwd or "."))
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def list_sessions(n: int = 10) -> int:
    files = _all_session_files()[:n]
    if not files:
        print(f"No sessions found under {_sessions_dir()}")
        return 0
    print(f"{'SESSION ID':36}  {'WHEN':19}  PROJECT / first message")
    for f in files:
        when = dt.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        first = ""
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                obj = json.loads(line)
                msg = obj.get("message", obj)
                content = msg.get("content") if isinstance(msg, dict) else None
                if obj.get("type") == "user" or (isinstance(msg, dict) and msg.get("role") == "user"):
                    if isinstance(content, str):
                        first = content
                    elif isinstance(content, list):
                        first = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    if first.strip():
                        break
        except Exception:
            pass
        print(f"{f.stem:36}  {when}  {f.parent.name[:30]}  {first.strip()[:50]}")
    return 0


def resolve_session_id(value: str, cwd: str | None) -> str | None:
    """Explicit id passes through; 'latest' finds the newest transcript (preferring
    the project dir matching cwd)."""
    if value and value.lower() != "latest":
        return value
    files = _all_session_files()
    if not files:
        return None
    slug = _cwd_slug(cwd)
    preferred = [f for f in files if f.parent.name == slug] or \
                [f for f in files if slug.endswith(f.parent.name.lstrip("-"))]
    return (preferred[0] if preferred else files[0]).stem


# --------------------------------------------------------------------------- #
# Starter playbook (generated when no playbook file is given)
# --------------------------------------------------------------------------- #
PLAYBOOK_TEMPLATE = """\
# playbook.yaml — edit the prompt(s), then run:  agent-playbook playbook.yaml
#
# The runner walks `instructions` top to bottom:
#   prompt: ...   run as a Claude Code prompt
#   notify: ...   send a message
#   (end)         stop

session: keep             # one threaded Claude session carried across prompts
continue_on_limit: true   # hit the usage limit? wait for the window to reset, then
                          # continue the same prompt — don't terminate.
notify_on_finish: true    # send a notification when the playbook finishes
notify_backend: none      # none | telegram | ntfy  (set this to actually receive pings)
# resume_session: latest # adopt an existing session (id or 'latest') and continue it
# Telegram: export TELEGRAM_BOT_TOKEN=...  export TELEGRAM_CHAT_ID=...
# ntfy:     export NTFY_TOPIC=...   (then subscribe to that topic in the ntfy app)

# max_cost_usd: 10        # hard budget cap for the whole playbook (0/absent = none)

defaults:
  cwd: .                  # working dir for prompts, e.g. ~/projects/myapp
  permission_mode: dontAsk
  allowed_tools: [Read, Write, Edit, "Bash(npm:*)", "Bash(git add:*)", "Bash(git commit:*)"]
  timeout_min: 180        # kill + retry an agent call that produces nothing for this long
  # fix_attempts: 2           # verify failed? feed the output back to the agent to fix
  # max_attempts: 25          # runaway guard: max attempts per prompt (0 = unlimited)
  # agent: claude             # claude | codex | gemini  (see: agent-playbook --agents)
  # fallback_agents: [codex]  # if the agent hits its usage limit, hand the prompt
  #                           # to these instead of only waiting for the reset

instructions:
  - prompt: "Describe your task here. Write it so that re-running is safe."
    # verify: "npm test"            # optional: 'done' only if this command exits 0
  # - notify: "checkpoint reached"  # add notify lines anywhere you want a ping
"""


HANDOFF_TEMPLATE = """\
# playbook.yaml — pinned to an existing Claude Code session.
# Edit the prompt if you like, then:  agent-playbook {name} --detach
# Tip: quit the interactive session first, so two processes don't share one session.

session: keep
continue_on_limit: true        # wait out the usage-limit reset, then continue
notify_on_finish: true
notify_backend: none           # set telegram/ntfy + env vars to get the finish ping

resume_session: {sid}          # captured session — its conversation/context is reused

defaults:
  cwd: {cwd}
  permission_mode: dontAsk
  allowed_tools: [Read, Write, Edit, "Bash(npm:*)", "Bash(git add:*)", "Bash(git commit:*)"]

instructions:
  - prompt: "Continue exactly where you left off and complete the remaining work. Do not redo anything already finished."
"""


def handoff_scaffold(target: Path, cwd: str) -> int:
    if target.exists():
        print(f"{target} already exists — not overwriting.")
        return 0
    sid = resolve_session_id("latest", cwd)
    if not sid:
        print("No existing session found to hand off. Are you inside a Claude Code project?")
        return 1
    target.write_text(HANDOFF_TEMPLATE.format(sid=sid, cwd=cwd, name=target.name), encoding="utf-8")
    print(f"Created {target}, pinned to session {sid}")
    print("Quit this session, then run:")
    print(f"  agent-playbook {target} --detach")
    return 0


def scaffold_playbook(target: Path) -> int:
    if target.exists():
        print(f"{target} already exists — not overwriting.")
        print(f"Edit it, then run:  agent-playbook {target}")
        return 0
    target.write_text(PLAYBOOK_TEMPLATE, encoding="utf-8")
    print(f"Created {target}")
    print("Edit the `instructions` (and notify_backend if you want pings), then run:")
    print(f"  agent-playbook {target}")
    return 0


# --------------------------------------------------------------------------- #
# Agent adapters — one per CLI coding agent the runner can drive.
#
# Each adapter knows: how to invoke its agent headlessly, how to thread a
# session across prompts (when the agent supports it), how to parse the final
# result, and what its vendor's usage-limit / rate-limit messages look like.
# The playbook picks agents with `agent:` and `fallback_agents:` per
# instruction (or in defaults).
# --------------------------------------------------------------------------- #
class BaseAdapter:
    name = "base"
    bin_env = ""             # env var that overrides the binary path
    default_bin = ""
    supports_resume = True   # False -> each prompt is stateless for this agent
    usage_re = USAGE_LIMIT_RE
    transient_re = TRANSIENT_RE

    def bin(self) -> str:
        """Resolve via PATH so Windows .cmd/.ps1 shims work with subprocess."""
        exe = os.path.expanduser(os.environ.get(self.bin_env) or self.default_bin)
        return shutil.which(exe) or exe

    def available(self) -> str | None:
        exe = os.path.expanduser(os.environ.get(self.bin_env) or self.default_bin)
        return shutil.which(exe)

    def model_for(self, opts: dict) -> str | None:
        """`models: {claude: .., codex: ..}` wins; legacy `model:` applies only
        to the instruction's primary agent."""
        models = opts.get("models") or {}
        if models.get(self.name):
            return str(models[self.name])
        if opts.get("model") and str(opts.get("agent") or "claude") == self.name:
            return str(opts["model"])
        return None

    def build(self, opts: dict, session_id: str | None) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str) -> dict:
        raise NotImplementedError

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Kill the agent call INCLUDING its children. proc.kill() alone only
        hits the direct child — a .cmd/.bat shim's real process (node, python)
        would survive and keep the pipes open forever."""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
            else:
                os.killpg(proc.pid, signal.SIGKILL)   # start_new_session set below
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    def run(self, prompt: str, opts: dict, session_id: str | None, cwd: str | None) -> dict:
        cmd = self.build(opts, session_id if self.supports_resume else None)
        # A hung agent call must never block an unattended run forever.
        timeout_min = float(opts.get("timeout_min", 180) or 0)
        timeout_sec = timeout_min * 60 if timeout_min > 0 else None
        kwargs: dict = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True   # own process group -> killable as a tree
        try:
            # Prompt travels via stdin (no ~32k Windows argv limit, no shell-shim
            # quoting). utf-8 explicitly: Windows text pipes default to the
            # legacy codepage, which mangles non-ASCII text.
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace",
                                    **kwargs)
        except FileNotFoundError:
            return {"ok": False, "limit": Limit.NONE, "agent": self.name,
                    "text": f"{self.name}: binary not found ({cmd[0]}). "
                            f"Install it or set {self.bin_env}.",
                    "session_id": None, "cost": 0.0, "num_turns": None,
                    "exit_code": 127, "raw": f"{self.name}: binary not found ({cmd[0]})"}
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            try:
                proc.communicate(timeout=15)     # reap; pipes close once the tree is dead
            except Exception:
                pass
            msg = f"{self.name}: no result after {timeout_min:g} min (timeout_min) — killed."
            return {"ok": False, "limit": Limit.NONE, "agent": self.name,
                    "timed_out": True, "text": msg, "session_id": None,
                    "cost": 0.0, "num_turns": None, "exit_code": -1, "raw": msg}
        stdout, stderr = stdout or "", stderr or ""
        parsed = self.parse(stdout, stderr)
        combined = "\n".join([parsed.get("text") or "", stdout, stderr])
        failed = (proc.returncode != 0) or bool(parsed.get("is_error"))
        # Only a FAILED run can be rate-limited. A successful result whose text
        # merely mentions rate limits (e.g. the task is about rate limiting) is fine.
        limit = Limit.NONE
        if failed:
            if self.usage_re.search(combined):
                limit = Limit.USAGE
            elif self.transient_re.search(combined):
                limit = Limit.TRANSIENT
        return {"ok": not failed, "limit": limit, "agent": self.name,
                "text": (parsed.get("text") or stderr.strip() or stdout.strip()),
                "session_id": parsed.get("session_id"),
                "cost": float(parsed.get("cost") or 0.0),
                "num_turns": parsed.get("num_turns"),
                "exit_code": proc.returncode, "raw": combined}


class ClaudeAdapter(BaseAdapter):
    """Claude Code CLI: headless -p mode, threaded sessions via --resume."""
    name = "claude"
    bin_env = "CLAUDE_BIN"
    default_bin = "claude"
    usage_re = USAGE_LIMIT_RE
    transient_re = TRANSIENT_RE

    def build(self, opts, session_id):
        cmd = [self.bin(), "-p", "--output-format", "json"]
        if session_id:
            cmd += ["--resume", session_id]   # continue the threaded conversation
        if opts.get("permission_mode"):
            cmd += ["--permission-mode", str(opts["permission_mode"])]
        if opts.get("allowed_tools"):
            cmd += ["--allowedTools", ",".join(opts["allowed_tools"])]
        if opts.get("max_turns"):
            cmd += ["--max-turns", str(opts["max_turns"])]
        model = self.model_for(opts)
        if model:
            cmd += ["--model", model]
        return cmd

    def parse(self, stdout, stderr):
        try:
            p = json.loads(stdout.strip())
            if isinstance(p, dict):
                return {"text": str(p.get("result", "")),
                        "session_id": p.get("session_id"),
                        "cost": p.get("total_cost_usd") or 0.0,
                        "num_turns": p.get("num_turns"),
                        "is_error": bool(p.get("is_error")) or p.get("subtype") == "error"}
        except Exception:
            pass
        return {}


class CodexAdapter(BaseAdapter):
    """OpenAI Codex CLI: `codex exec` headless mode with --json events;
    threads continue via `codex exec resume <id>`. (Experimental: contract
    covered by stub tests; verify against your installed codex version.)"""
    name = "codex"
    bin_env = "CODEX_BIN"
    default_bin = "codex"
    usage_re = re.compile(r"hit your usage limit|usage limit|weekly limit", re.I)
    transient_re = re.compile(r"429|too many requests|rate limit(?:ed)?|temporarily", re.I)

    def build(self, opts, session_id):
        cmd = [self.bin(), "exec"]
        if session_id:
            cmd += ["resume", session_id]
        cmd += ["--json", "--skip-git-repo-check"]
        if opts.get("codex_sandbox"):
            cmd += ["--sandbox", str(opts["codex_sandbox"])]
        else:
            cmd += ["--full-auto"]
        model = self.model_for(opts)
        if model:
            cmd += ["--model", model]
        cmd += ["-"]   # read the prompt from stdin
        return cmd

    def parse(self, stdout, stderr):
        text, sid, turns = None, None, 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            sid = obj.get("thread_id") or obj.get("session_id") or sid
            item = obj.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text") or text
                turns += 1
            msg = obj.get("msg") or {}   # older codex event shape
            if isinstance(msg, dict):
                if msg.get("type") == "agent_message":
                    text = msg.get("message") or text
                    turns += 1
                if msg.get("type") == "session_configured":
                    sid = msg.get("session_id") or sid
        return {"text": text, "session_id": sid, "num_turns": turns or None}


class GeminiAdapter(BaseAdapter):
    """Google Gemini CLI: headless via stdin prompt. Prefers --output-format
    json (newer CLIs); older CLIs without that flag get plain-text mode
    automatically. No reliable headless resume -> prompts run stateless (write
    them self-contained)."""
    name = "gemini"
    bin_env = "GEMINI_BIN"
    default_bin = "gemini"
    supports_resume = False
    usage_re = re.compile(r"quota exceeded|resource.?exhausted|daily limit", re.I)
    transient_re = re.compile(r"429|rate limit(?:ed)?|too many requests|overloaded|503|unavailable", re.I)

    def __init__(self):
        self._json_ok = True    # flips off for old CLIs lacking --output-format

    def build(self, opts, session_id):
        cmd = [self.bin()]
        if self._json_ok:
            cmd += ["--output-format", "json"]
        cmd += ["--yolo"]
        model = self.model_for(opts)
        if model:
            cmd += ["-m", model]
        return cmd

    def parse(self, stdout, stderr):
        try:
            p = json.loads(stdout.strip())
            if isinstance(p, dict):
                err = p.get("error")
                return {"text": str(p.get("response") or (err or {}).get("message", "")),
                        "is_error": bool(err), "session_id": None}
        except Exception:
            pass
        # plain-text mode (old CLIs): stdout IS the response
        return {"text": stdout.strip(), "session_id": None}

    def run(self, prompt, opts, session_id, cwd):
        res = super().run(prompt, opts, session_id, cwd)
        if (not res["ok"] and self._json_ok
                and re.search(r"unknown argument", res["raw"], re.I)):
            self._json_ok = False
            log("   [gemini] CLI lacks --output-format json; retrying in plain-text mode")
            res = super().run(prompt, opts, session_id, cwd)
        return res


ADAPTERS: dict[str, BaseAdapter] = {
    a.name: a for a in (ClaudeAdapter(), CodexAdapter(), GeminiAdapter())
}


def agent_chain(opts: dict) -> list[str]:
    """[primary, *fallbacks] — the order agents are tried for one instruction."""
    chain = [str(opts.get("agent") or "claude")]
    for a in opts.get("fallback_agents") or []:
        if str(a) not in chain:
            chain.append(str(a))
    return chain


# --------------------------------------------------------------------------- #
# Usage-window tracking — every observed usage-limit event is recorded so
# `--windows` can show when each agent's window closes/reopens.
# --------------------------------------------------------------------------- #
def runner_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLAUDE_RUNNER_HOME")
                                   or "~/.agent-playbook"))


def record_window_event(agent: str, raw: str, cap_sec: int) -> None:
    try:
        path = runner_home() / "windows.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        events = []
        if path.exists():
            try:
                events = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                events = []
        now = dt.datetime.now().astimezone()
        secs = parse_reset_seconds(raw, cap_sec)
        events.append({
            "agent": agent,
            "hit_at": now.isoformat(timespec="seconds"),
            "reset_at": ((now + dt.timedelta(seconds=secs)).isoformat(timespec="seconds")
                         if secs else None),
        })
        path.write_text(json.dumps(events[-200:], indent=2), encoding="utf-8")
    except Exception as e:
        log(f"[windows] could not record event: {e}")


def show_windows() -> int:
    path = runner_home() / "windows.json"
    events = []
    if path.exists():
        try:
            events = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not events:
        print("No usage-limit events recorded yet. They are logged automatically "
              "whenever a run hits an agent's usage limit.")
        return 0

    def ts(e: dict, key: str):
        try:
            return dt.datetime.fromisoformat(e.get(key) or "")
        except Exception:
            return None

    now = dt.datetime.now().astimezone()
    week_ago = now - dt.timedelta(days=7)
    print(f"{'AGENT':8}  {'HITS(7d)':>8}  {'LAST HIT':17}  NEXT KNOWN RESET")
    for agent in sorted({e.get("agent", "?") for e in events}):
        evs = [e for e in events if e.get("agent") == agent]
        hits7 = [e for e in evs if (t := ts(e, "hit_at")) and t > week_ago]
        last = ts(evs[-1], "hit_at")
        resets = [t for e in evs if (t := ts(e, "reset_at")) and t > now]
        nxt = min(resets) if resets else None
        print(f"{agent:8}  {len(hits7):>8}  "
              f"{last.strftime('%Y-%m-%d %H:%M') if last else '-':17}  "
              f"{nxt.strftime('%Y-%m-%d %H:%M') if nxt else 'unknown'}")
    return 0


def show_agents() -> int:
    print(f"{'AGENT':8}  {'STATUS':11}  BINARY")
    for name in sorted(ADAPTERS):
        a = ADAPTERS[name]
        found = a.available()
        note = "" if a.supports_resume else "   (stateless: no session resume)"
        if found:
            print(f"{name:8}  {'installed':11}  {found}{note}")
        else:
            print(f"{name:8}  {'not found':11}  install '{a.default_bin}' or set {a.bin_env}{note}")
    return 0


def run_verify(verify_cmd: str, cwd: str | None) -> tuple[bool, str]:
    """Run the verify command. Returns (passed, output tail) — the tail is fed
    back to the agent when fix_attempts is enabled."""
    log(f"   verify: {verify_cmd}")
    p = subprocess.run(verify_cmd, cwd=cwd, shell=True, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    tail_lines = (p.stdout + p.stderr).strip().splitlines()
    tail = "\n".join(tail_lines[-40:])
    if p.returncode != 0:
        log("   verify FAILED:\n     " + "\n     ".join(tail_lines[-4:]))
    return p.returncode == 0, tail


def sleep_with_heartbeat(seconds: int, why: str) -> None:
    end = time.time() + seconds
    next_beat = 0.0
    while True:
        check_stop()                       # stop.request works even mid-wait
        remaining = end - time.time()
        if remaining <= 0:
            return
        if time.time() >= next_beat:       # heartbeat each minute
            log(f"   waiting ({why}) ~{int(remaining) // 60}m{int(remaining) % 60:02d}s left…")
            next_beat = time.time() + 60
        time.sleep(min(5.0, max(0.1, remaining)))  # short naps so stop stays responsive


# --------------------------------------------------------------------------- #
# Running one prompt instruction (rides out limits, continues after reset).
# With fallback_agents, a usage-limited agent hands the prompt to the next
# agent in the chain; only when EVERY agent is limited does the runner wait.
# `sessions` maps agent name -> its threaded session id, and is mutated.
# --------------------------------------------------------------------------- #
def run_prompt(prompt: str, opts: dict, limits: dict, sessions: dict,
               logs_dir: Path, idx: int, continue_on_limit: bool = True,
               budget_usd: float | None = None) -> dict:
    cwd = os.path.expanduser(opts["cwd"]) if opts.get("cwd") else None
    chain = agent_chain(opts)

    poll = int(limits.get("poll_interval_sec", 300))
    resume_poll = int(limits.get("resume_poll_sec", 30))
    t_base = int(limits.get("transient_base_sec", 15))
    t_max = int(limits.get("transient_max_sec", 300))
    t_retries = int(limits.get("transient_max_retries", 6))
    usage_cap = int(limits.get("usage_max_wait_sec", 86400))
    timeout_retries = int(limits.get("timeout_max_retries", 1))
    max_attempts = int(opts.get("max_attempts", 0) or 0)
    fix_attempts = int(opts.get("fix_attempts", 0) or 0)

    attempt = usage_waits = fixes = 0
    spent = 0.0                              # cost accrued across ALL attempts
    transient_tries = {name: 0 for name in chain}
    timeout_tries = {name: 0 for name in chain}

    def done(res: dict) -> dict:
        res["cost"] = round(spent, 4)
        return res

    while True:
        primary_limit_raw = ""
        res = None
        for name in chain:
            adapter = ADAPTERS[name]
            while True:                       # transient/timeout retries for this agent
                check_stop()
                if max_attempts and attempt >= max_attempts:
                    return done({"ok": False, "limit": Limit.NONE, "agent": name,
                                 "text": f"max_attempts ({max_attempts}) reached for "
                                         f"prompt #{idx}; stopping this playbook.",
                                 "session_id": sessions.get(name), "cost": 0.0,
                                 "num_turns": None, "exit_code": None, "raw": ""})
                if budget_usd is not None and spent >= budget_usd:
                    return done({"ok": False, "limit": Limit.NONE, "agent": name,
                                 "text": f"max_cost_usd budget exhausted "
                                         f"(${spent:.4f} spent on prompt #{idx}); stopping.",
                                 "session_id": sessions.get(name), "cost": 0.0,
                                 "num_turns": None, "exit_code": None, "raw": ""})
                attempt += 1
                sid = sessions.get(name)
                tag = (f"[{name}] " if len(chain) > 1 or name != "claude" else "") + \
                      (f"(resume {sid[:8]})" if sid else "(new session)")
                log(f"   running prompt #{idx} {tag} attempt {attempt}")
                res = adapter.run(prompt, opts, sid, cwd)
                (logs_dir / f"prompt{idx:02d}.attempt{attempt}.log").write_text(
                    res["raw"], encoding="utf-8")
                spent += float(res.get("cost") or 0.0)
                if res.get("session_id"):
                    sessions[name] = res["session_id"]
                if res.get("timed_out"):
                    timeout_tries[name] += 1
                    if timeout_tries[name] > timeout_retries:
                        return done(res)      # persistent hang -> fail the prompt
                    log(f"   [{name}] timed out — retrying "
                        f"({timeout_tries[name]}/{timeout_retries})")
                    continue
                if res["limit"] == Limit.TRANSIENT:
                    transient_tries[name] += 1
                    if transient_tries[name] > t_retries:
                        res["ok"] = False
                        res["text"] = "Exceeded transient rate-limit retries.\n" + res["text"]
                        return done(res)
                    back = min(t_max, t_base * (2 ** (transient_tries[name] - 1)))
                    log(f"   transient limit; backoff {back}s "
                        f"({transient_tries[name]}/{t_retries})")
                    time.sleep(back)
                    continue
                break

            if res["limit"] == Limit.USAGE:
                record_window_event(name, res["raw"], usage_cap)
                if name == chain[0]:
                    primary_limit_raw = res["raw"]
                if name != chain[-1]:
                    log(f"   [{name}] usage limit — switching to next agent")
                continue                      # try the next agent in the chain

            if res["ok"] and opts.get("verify"):
                v_ok, v_tail = run_verify(opts["verify"], cwd)
                if not v_ok:
                    if fixes < fix_attempts:
                        # Self-healing: feed the failure back to the SAME agent
                        # (its session has the context) and let it fix the problem.
                        fixes += 1
                        log(f"   verify failed — asking [{name}] to fix it "
                            f"(fix {fixes}/{fix_attempts})")
                        prompt = (
                            "The work is not done yet. The verification command\n"
                            f"    {opts['verify']}\n"
                            "failed with this output:\n"
                            f"```\n{v_tail}\n```\n"
                            "Diagnose and fix the problem so the command passes. "
                            "Do not break existing functionality. Do not weaken or "
                            "delete the verification itself."
                        )
                        chain = [name]
                        break                 # restart the outer loop with the fix prompt
                    res["ok"] = False
                    res["text"] = "verify command failed.\n" + res["text"]
            return done(res)                  # success or hard failure
        else:
            # for-loop finished without break: every agent is usage-limited
            if not continue_on_limit:
                res["ok"] = False
                res["text"] = ("Usage limit hit and continue_on_limit is disabled.\n"
                               + res["text"])
                return done(res)
            usage_waits += 1
            # First wait: until the announced reset. Afterwards: short polls so
            # we resume the instant a window actually reopens.
            wait = (parse_reset_seconds(primary_limit_raw, usage_cap) or poll) \
                if usage_waits == 1 else resume_poll
            who = chain[0] if len(chain) == 1 else f"all agents: {', '.join(chain)}"
            log(f"   USAGE LIMIT ({who}). Waiting ~{wait // 60}m then continuing this prompt.")
            sleep_with_heartbeat(wait, "usage window")
            continue
        continue                              # reached only via the fix-attempt break


# --------------------------------------------------------------------------- #
# Playbook helpers
# --------------------------------------------------------------------------- #
def resolve_prompt_text(instr: dict, resolve_file: bool = True) -> str:
    if instr.get("prompt"):
        return instr["prompt"]
    if instr.get("prompt_file"):
        if resolve_file:
            return Path(os.path.expanduser(instr["prompt_file"])).read_text(encoding="utf-8")
        return f"<prompt_file: {instr['prompt_file']}>"
    return ""


def instr_kind(instr: dict) -> str:
    if instr.get("prompt") or instr.get("prompt_file"):
        return "prompt"
    if "notify" in instr:
        return "notify"
    return "unknown"


# Legal keys per section — used only to WARN about unknown (likely typo'd)
# keys, never to reject them, so future fields stay forward-compatible.
_OPTS_KEYS = {"cwd", "allowed_tools", "permission_mode", "max_turns", "model",
              "models", "verify", "agent", "fallback_agents", "codex_sandbox",
              "timeout_min", "fix_attempts", "max_attempts"}
_TOP_KEYS = {"session", "continue_on_limit", "notify_on_finish",
             "notify_on_failure", "notify_backend", "notify", "defaults",
             "limits", "instructions", "max_cost_usd", "resume_session"}
_INSTR_KEYS = _OPTS_KEYS | {"prompt", "prompt_file", "notify", "label",
                            "when", "on_fail"}
_LIMITS_KEYS = {"poll_interval_sec", "resume_poll_sec", "transient_base_sec",
                "transient_max_sec", "transient_max_retries",
                "usage_max_wait_sec", "timeout_max_retries", "max_gotos"}
_NOTIFY_KEYS = {"telegram_bot_token_env", "telegram_chat_id_env",
                "ntfy_topic", "ntfy_server"}


def unknown_key_warnings(pb: dict) -> list[str]:
    """Warnings for keys the runner will silently ignore (usually typos),
    with a did-you-mean suggestion where one is close enough."""
    import difflib
    warnings: list[str] = []

    def check(mapping: dict, legal: set, where: str) -> None:
        for k in mapping:
            if k not in legal:
                close = difflib.get_close_matches(str(k), legal, n=1)
                hint = f" — did you mean '{close[0]}'?" if close else ""
                warnings.append(f"{where}: unknown key '{k}' (ignored){hint}")

    check(pb, _TOP_KEYS, "playbook")
    check(pb.get("defaults") or {}, _OPTS_KEYS, "defaults")
    check(pb.get("limits") or {}, _LIMITS_KEYS, "limits")
    check(pb.get("notify") or {}, _NOTIFY_KEYS, "notify")
    for n, instr in enumerate(pb.get("instructions") or [], 1):
        check(instr, _INSTR_KEYS, f"instruction #{n}")
    return warnings


def load_playbook(pb_path: Path) -> dict:
    """Read and structurally validate the playbook. Exits with a clear message
    on malformed input instead of surfacing a traceback."""
    try:
        pb = yaml.safe_load(pb_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        sys.exit(f"{pb_path}: not valid YAML — {e}")
    if pb is None:
        sys.exit(f"{pb_path}: playbook is empty.")
    if not isinstance(pb, dict):
        sys.exit(f"{pb_path}: playbook must be a YAML mapping with top-level "
                 f"keys like 'instructions:' (got {type(pb).__name__}).")
    instructions = pb.get("instructions", [])
    if not isinstance(instructions, list):
        sys.exit(f"{pb_path}: 'instructions' must be a list.")
    for n, instr in enumerate(instructions, 1):
        if not isinstance(instr, dict):
            sys.exit(f"{pb_path}: instruction #{n} must be a mapping like "
                     f"'- prompt: ...' or '- notify: ...' (got: {instr!r}).")
    for key in ("defaults", "limits", "notify"):
        if pb.get(key) is not None and not isinstance(pb[key], dict):
            sys.exit(f"{pb_path}: '{key}' must be a mapping.")
    try:
        float(pb.get("max_cost_usd", 0) or 0)
    except (TypeError, ValueError):
        sys.exit(f"{pb_path}: 'max_cost_usd' must be a number "
                 f"(got: {pb.get('max_cost_usd')!r}).")
    for k, v in (pb.get("limits") or {}).items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            sys.exit(f"{pb_path}: limits.{k} must be a number (got: {v!r}).")
    return pb


def parse_on_fail(instr: dict) -> tuple[str, str | None]:
    """Parse an instruction's on_fail into (policy, goto_label).
    Policies: stop (default) | continue | goto. Unrecognized -> ('invalid', None)."""
    raw = str(instr.get("on_fail", "stop")).strip()
    if raw in ("stop", "continue"):
        return raw, None
    if raw.startswith("goto"):
        label = raw[len("goto"):].strip(" :")
        if label:
            return "goto", label
    return "invalid", None


def build_labels(instructions: list) -> dict[str, int]:
    """Map label -> 0-based instruction index; exits on duplicates."""
    labels: dict[str, int] = {}
    for n, instr in enumerate(instructions, 1):
        lbl = instr.get("label")
        if lbl is None:
            continue
        lbl = str(lbl)
        if lbl in labels:
            sys.exit(f"Duplicate label '{lbl}' "
                     f"(instructions #{labels[lbl] + 1} and #{n}).")
        labels[lbl] = n - 1
    return labels


def merged_opts(instr: dict, defaults: dict) -> dict:
    opts = dict(defaults)
    for k in ("cwd", "allowed_tools", "permission_mode", "max_turns", "model",
              "models", "verify", "agent", "fallback_agents", "codex_sandbox",
              "timeout_min", "fix_attempts", "max_attempts"):
        if k in instr:
            opts[k] = instr[k]
    opts.setdefault("permission_mode", "dontAsk")
    return opts


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    global _LOG_FH, _STOP_FILE

    # Consistent UTF-8 output on every platform: piped stdout on stock Windows
    # defaults to the legacy codepage, which mangles the log's em-dashes etc.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Run an ordered Claude Code playbook.")
    ap.add_argument("playbook", nargs="?",
                    help="playbook file; if omitted, a starter playbook.yaml is generated")
    ap.add_argument("--state", help="state file (default: <playbook>.state.json)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    ap.add_argument("--strict", action="store_true",
                    help="treat unknown-key warnings as errors (for CI)")
    ap.add_argument("--from", dest="start_from", type=int,
                    help="start at instruction N (1-based)")
    ap.add_argument("--restart", action="store_true", help="ignore saved progress")
    ap.add_argument("--resume-session", dest="resume_session", metavar="ID",
                    help="seed the run with an existing session id (or 'latest') so the "
                         "first prompt continues that conversation")
    ap.add_argument("--list-sessions", dest="list_sessions", nargs="?", type=int,
                    const=10, help="list recent session ids and exit")
    ap.add_argument("--handoff", action="store_true",
                    help="capture the current session into a ready-to-run playbook and exit")
    ap.add_argument("--detach", action="store_true",
                    help="run in the background, survive logout, log to the run's logfile")
    ap.add_argument("--agents", action="store_true",
                    help="list supported agents and whether their CLI is installed")
    ap.add_argument("--windows", action="store_true",
                    help="show recorded usage-limit windows per agent and exit")
    args = ap.parse_args()

    if args.agents:
        return show_agents()

    if args.windows:
        return show_windows()

    if args.list_sessions is not None:
        return list_sessions(args.list_sessions)

    if args.handoff:
        target = Path(os.path.expanduser(args.playbook)) if args.playbook else Path("playbook.yaml")
        return handoff_scaffold(target, os.getcwd())

    # No playbook given -> generate a starter one the user can edit.
    if not args.playbook:
        return scaffold_playbook(Path("playbook.yaml"))

    pb_path = Path(os.path.expanduser(args.playbook))

    # Secrets: auto-load a .env file sitting next to the playbook. Real env
    # vars keep precedence; the detached child re-loads it itself.
    loaded_env = load_env_file(pb_path.resolve().parent / ".env")
    if loaded_env:
        log(f"[env] loaded {loaded_env} var(s) from {pb_path.resolve().parent / '.env'}")

    # Background launch: re-exec detached, then exit. No nohup/& needed.
    if args.detach:
        ld = pb_path.parent / (pb_path.stem + ".logs")
        ld.mkdir(exist_ok=True)
        logfile = ld / "runner.log"
        argv = [a for a in sys.argv[1:] if a != "--detach"]
        env = {**os.environ, "CLAUDE_RUNNER_DETACHED": "1"}
        kwargs: dict = {}
        if os.name == "nt":
            # start_new_session is silently ignored on Windows; these flags are
            # what actually detaches the child from this console/session.
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        with open(logfile, "a") as out:
            proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), *argv],
                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, **kwargs,
            )
        print(f"started in background  pid={proc.pid}")
        if os.name == "nt":
            print(f"  tail:  Get-Content -Wait \"{logfile}\"")
            print(f"  stop:  taskkill /PID {proc.pid} /T /F   (or create {ld / 'stop.request'})")
        else:
            print(f"  tail:  tail -f {logfile}")
            print(f"  stop:  kill {proc.pid}")
        return 0

    pb = load_playbook(pb_path)
    key_warnings = unknown_key_warnings(pb)
    for w in key_warnings:
        print(f"warning: {w}", file=sys.stderr)
    if key_warnings and args.strict:
        sys.exit(f"{len(key_warnings)} unknown key(s) — failing because of --strict.")
    instructions = pb.get("instructions", [])
    defaults = pb.get("defaults", {})
    limits = pb.get("limits", {})
    session_mode = (pb.get("session", "keep")).lower()   # keep | fresh
    continue_on_limit = bool(pb.get("continue_on_limit", True))
    max_cost_usd = float(pb.get("max_cost_usd", 0) or 0)   # 0 = no budget cap
    notify_on_failure = pb.get("notify_on_failure", True)
    notify_on_finish = bool(pb.get("notify_on_finish", False))
    notifier = Notifier(pb.get("notify_backend", "none"), pb.get("notify", {}))

    if not instructions:
        sys.exit("Playbook has no 'instructions'.")

    # Fail fast on agents the playbook references but this runner doesn't know.
    for n, instr in enumerate(instructions, 1):
        if instr_kind(instr) != "prompt":
            continue
        for name in agent_chain(merged_opts(instr, defaults)):
            if name not in ADAPTERS:
                sys.exit(f"Instruction #{n}: unknown agent '{name}'. "
                         f"Available: {', '.join(sorted(ADAPTERS))} "
                         f"(see: agent-playbook --agents)")

    # Control flow: resolve labels and fail fast on bad on_fail values.
    labels = build_labels(instructions)
    for n, instr in enumerate(instructions, 1):
        if "on_fail" not in instr:
            continue
        policy, target = parse_on_fail(instr)
        if policy == "invalid" or (policy == "goto" and target not in labels):
            sys.exit(f"Instruction #{n}: on_fail must be 'stop', 'continue', or "
                     f"'goto <label>' naming an existing label "
                     f"(got: {instr['on_fail']!r}).")
    max_gotos = int(limits.get("max_gotos", 20))

    state_path = Path(args.state) if args.state else pb_path.with_suffix(".state.json")
    state = fresh_state() if args.restart else load_state(state_path)
    if args.start_from is not None:
        state["next_index"] = max(0, args.start_from - 1)

    # Adopt an existing (e.g. interactive) Claude session so the first prompt
    # continues it. CLI flag overrides the playbook's `resume_session` field.
    # Only seed if we don't already have a session in progress.
    resume_req = args.resume_session or pb.get("resume_session")
    if resume_req and not state["sessions"].get("claude"):
        sid = resolve_session_id(str(resume_req), defaults.get("cwd"))
        if sid:
            state["sessions"]["claude"] = sid
            print(f"resuming existing session {sid}")
        else:
            print(f"warning: could not resolve session '{resume_req}'; starting fresh")

    logs_dir = pb_path.parent / (pb_path.stem + ".logs")
    _STOP_FILE = logs_dir / "stop.request"
    pid_file = logs_dir / "run.pid"
    if not args.dry_run:                 # dry-run executes nothing, creates nothing
        logs_dir.mkdir(exist_ok=True)
        if not os.environ.get("CLAUDE_RUNNER_DETACHED"):
            _LOG_FH = open(logs_dir / "runner.log", "a", encoding="utf-8")
        # Single-runner lock: two runners sharing one playbook would also share
        # one Claude session, which corrupts both.
        old_pid = None
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            pass
        if old_pid and old_pid != os.getpid() and pid_alive(old_pid):
            log(f"Another runner (pid {old_pid}) already has this playbook. "
                f"Stop it first, or wait for it to finish.")
            return 1
        pid_file.write_text(str(os.getpid()))

        def _cleanup_pid_file():
            try:
                if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_file.unlink()
            except Exception:
                pass
        atexit.register(_cleanup_pid_file)
        _STOP_FILE.unlink(missing_ok=True)   # stale stop request from a past run

    def _bye(signum, frame):
        log(f"[signal {signum}] flushing state and exiting.")
        save_state(state_path, state)
        sys.exit(130)
    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)

    if args.dry_run:
        log(f"Playbook: {pb_path}  ({len(instructions)} instructions)  session={session_mode}")
        for i, instr in enumerate(instructions, 1):
            kind = instr_kind(instr)
            flow = ""
            if instr.get("label"):
                flow += f" [label: {instr['label']}]"
            if instr.get("when"):
                flow += f" [when: {str(instr['when'])[:30]}]"
            if "on_fail" in instr:
                flow += f" [on_fail: {instr['on_fail']}]"
            if kind == "prompt":
                snip = resolve_prompt_text(instr, resolve_file=False).replace("\n", " ")[:70]
                chain = agent_chain(merged_opts(instr, defaults))
                note = "" if chain == ["claude"] else f" [{' -> '.join(chain)}]"
                log(f"  {i:>2}. prompt{note}{flow}  — {snip}")
            elif kind == "notify":
                log(f"  {i:>2}. notify{flow}  — {str(instr['notify'])[:70]}")
            else:
                log(f"  {i:>2}. UNKNOWN — {instr}")
        log("Dry run complete. Nothing was executed.")
        return 0

    start = state.get("next_index", 0)
    if start >= len(instructions):
        log(f"Nothing to do — all {len(instructions)} instructions already complete. "
            f"(use --restart to run again)")
        return 0
    log(f"Playbook: {pb_path}  starting at instruction #{start + 1} of {len(instructions)}")

    i = start
    gotos = 0            # per-run jump budget (limits.max_gotos) — loop guard
    try:
        while i < len(instructions):
            check_stop()
            instr = instructions[i]
            kind = instr_kind(instr)
            idx1 = i + 1

            gate = instr.get("when")
            if gate:
                gate_cwd = os.path.expanduser(
                    str(instr.get("cwd") or defaults.get("cwd") or ".")) or None
                gp = subprocess.run(str(gate), cwd=gate_cwd, shell=True,
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
                if gp.returncode != 0:
                    log(f"== #{idx1} when: {gate}  (exit={gp.returncode}) — skipped")
                    i += 1
                    state["next_index"] = i
                    save_state(state_path, state)
                    continue
                log(f"== #{idx1} when: {gate}  (passed)")

            if kind == "notify":
                text = str(instr["notify"])
                log(f"== #{idx1} notify: {text}")
                notifier.notify(text)

            elif kind == "prompt":
                spent_total = float(state.get("total_cost_usd", 0.0))
                if max_cost_usd and spent_total >= max_cost_usd:
                    msg = (f"max_cost_usd budget reached (${spent_total:.4f} of "
                           f"${max_cost_usd:.2f}) — stopping before prompt #{idx1}. "
                           f"Raise the budget and re-run to resume.")
                    log(f"   BUDGET STOP: {msg}")
                    if notify_on_failure:
                        notifier.notify(msg, title="agent-playbook BUDGET")
                    save_state(state_path, state)
                    return 1
                log(f"== #{idx1} prompt")
                opts = merged_opts(instr, defaults)
                sessions = state["sessions"] if session_mode == "keep" else {}
                try:
                    prompt_text = resolve_prompt_text(instr)
                except OSError as e:
                    # Missing/unreadable prompt_file is a normal prompt failure:
                    # on_fail policy applies, no traceback.
                    res = {"ok": False, "agent": None, "cost": 0.0,
                           "num_turns": None, "exit_code": None,
                           "text": f"cannot read prompt_file "
                                   f"{instr.get('prompt_file')!r}: {e}"}
                else:
                    res = run_prompt(prompt_text, opts, limits, sessions,
                                     logs_dir, idx1, continue_on_limit=continue_on_limit,
                                     budget_usd=(max_cost_usd - spent_total)
                                     if max_cost_usd else None)

                state["total_cost_usd"] = round(
                    state.get("total_cost_usd", 0.0) + (res.get("cost") or 0.0), 4)
                if session_mode == "keep":
                    # legacy mirror of Claude's threaded session for older tooling
                    state["session_id"] = state["sessions"].get("claude")

                if not res["ok"]:
                    err = (res.get("text") or "")[:300]
                    log(f"   FAILED prompt #{idx1} (exit={res.get('exit_code')}): {err}")
                    policy, target = parse_on_fail(instr)
                    if policy == "continue":
                        log("   on_fail: continue — moving on.")
                    elif policy == "goto":
                        gotos += 1
                        if gotos > max_gotos:
                            msg = (f"goto limit reached ({max_gotos} jumps) at prompt "
                                   f"#{idx1} — stopping to avoid a loop "
                                   f"(raise limits.max_gotos to allow more).")
                            log(f"   {msg}")
                            if notify_on_failure:
                                notifier.notify(msg, title="agent-playbook FAILED")
                            save_state(state_path, state)
                            return 1
                        log(f"   on_fail: goto '{target}' (#{labels[target] + 1}) — "
                            f"jump {gotos}/{max_gotos}.")
                        i = labels[target]
                        state["next_index"] = i
                        save_state(state_path, state)
                        continue
                    else:
                        if notify_on_failure:
                            notifier.notify(f"prompt #{idx1} failed:\n{err}",
                                            title="agent-playbook FAILED")
                        save_state(state_path, state)
                        log("Stopping on failure.")
                        return 1
                else:
                    log(f"   DONE prompt #{idx1} [{res.get('agent')}]  "
                        f"turns={res.get('num_turns')} cost=${res.get('cost', 0):.4f}")
            else:
                log(f"== #{idx1} UNKNOWN instruction skipped: {instr}")

            i += 1
            state["next_index"] = i
            save_state(state_path, state)
    except StopRequested:
        _STOP_FILE.unlink(missing_ok=True)
        save_state(state_path, state)
        log("[stop] stop requested — state saved; run again to resume.")
        return 130

    log(f"== PLAYBOOK COMPLETE — {len(instructions)} instructions. "
        f"Total ${state['total_cost_usd']:.4f}")
    if notify_on_finish:
        notifier.notify(
            f"playbook complete — {len(instructions)} instructions, "
            f"${state['total_cost_usd']:.4f}", title="agent-playbook done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
