# claude_runner

Run an ordered **playbook** of AI coding-agent instructions. The runner walks the list
top to bottom: a `prompt` is executed as an agent prompt (Claude Code by default; Codex
CLI and Gemini CLI are also supported); a `notify` sends a message; end of list → it
stops.

If a running prompt hits the **usage limit**, the runner waits until the window resets
and then *continues the same prompt* (resuming the threaded session) the moment the
window reopens — no manual restart. With `fallback_agents`, it doesn't even wait: the
prompt is handed to your next subscription's agent, and only when *every* agent is
limited does it wait for the first reset. Every limit event is recorded — see
`claude-runner --windows`.

## Install (one-time)
```bash
pipx install .            # gives you a global `claude-runner` command
```
No pipx? `pip install --user .`, or just `chmod +x claude_runner.py && ./claude_runner.py …`

### Optional: `claude --playbook` shim
To launch via `claude --playbook <file>` instead of `claude-runner`, append the shell
function in `claude-playbook.sh` to your `~/.bashrc` (or `~/.zshrc`) and re-source it:
```bash
cat claude-playbook.sh >> ~/.bashrc && source ~/.bashrc
```
On Windows, add the function in `claude-playbook.ps1` to your PowerShell profile instead
(`notepad $PROFILE`, paste, then `. $PROFILE`).
Then `claude --playbook playbook.yaml --detach` works, and every other `claude …` call
passes straight through to the real binary. (`--playbook` must be the first argument.)

## Run
```bash
claude login                              # be authenticated (uses your Max plan)

claude --playbook                          # no file -> generate a starter playbook.yaml
claude-runner playbook.yaml               # run
claude-runner playbook.yaml --detach      # background, survives logout
claude-runner playbook.yaml --dry-run     # print the plan, run nothing
claude-runner playbook.yaml --restart     # ignore saved progress, start over
claude-runner playbook.yaml --from 3      # start at instruction #3 (1-based)
```
`--detach` prints the pid + tail/stop commands and logs to `playbook.logs/runner.log`.
Works on Linux, macOS, and Windows (on Windows the detached run survives closing the
terminal via `DETACHED_PROCESS`, not `setsid`).

**Stopping a run:** `kill <pid>` (POSIX) or `taskkill /PID <pid> /T /F` (Windows) — or,
on any OS, create a `stop.request` file in the run's `.logs` dir. The runner notices it
within a few seconds (even while waiting out a usage-limit window), saves state, and
exits; the next start resumes where it left off.

## Playbook format
```yaml
session: keep              # keep = one threaded Claude session across prompts; fresh = independent
continue_on_limit: true    # hit the usage limit? wait for reset, then continue the prompt
notify_on_finish: true     # ping when the whole playbook finishes
notify_backend: telegram   # telegram | ntfy | none
defaults:                  # applied to every prompt unless overridden
  cwd: ~/projects/verb-drill
  permission_mode: dontAsk
  allowed_tools: [Read, Write, Edit, "Bash(npm:*)"]
  max_turns: 50
limits:
  resume_poll_sec: 30      # after the first wait, poll this often -> resume ASAP after reset
  poll_interval_sec: 300   # fallback wait if reset time can't be parsed

instructions:              # ordered; prompt or notify
  - prompt: "Implement the scheduler per CLAUDE.md. Commit when tests pass."
    verify: "npm test"     # prompt counts as done only if this exits 0
  - notify: "scheduler done"
  - prompt: "Now add streak tracking."
  - notify: "all done — review the branch"
```
Each prompt is a full Claude Code prompt (same as typing into `claude`), with the tools
you allow. `prompt_file: ./x.md` keeps long prompts in their own file.

## Multiple agents (Claude, Codex, Gemini)

The runner can drive more than one CLI coding agent — useful when you hold several
subscriptions: while one agent's usage window is closed, another is usually open.

```yaml
defaults:
  agent: claude              # who runs prompts: claude | codex | gemini
  fallback_agents: [codex]   # agent hit its usage limit? hand the prompt to these
                             # instead of only waiting for the reset
  models:                    # optional per-agent model choices
    claude: sonnet
    codex: gpt-5-codex

instructions:
  - prompt: "Fix all failing tests. Idempotent: if green, do nothing."
    verify: "npm test"
  - prompt: "Write the CHANGELOG entry for this release."
    agent: gemini            # per-instruction override
```

Semantics worth knowing:
- Sessions thread **per agent** (`claude` via `--resume`, `codex` via `exec resume`;
  `gemini` runs stateless). Context does NOT transfer between agents — write prompts
  with fallbacks to be self-contained.
- Only a **usage limit** triggers fallback. Hard failures stop the playbook as usual;
  transient rate limits are retried on the same agent with backoff.
- When *every* agent in the chain is limited, the runner waits and retries the chain.
- `claude-runner --agents` shows which agent CLIs are installed. Binaries can be
  overridden with `CLAUDE_BIN`, `CODEX_BIN`, `GEMINI_BIN`.
- The codex and gemini adapters are **experimental**: their contracts are covered by
  stub tests; verify once against your installed CLI versions.

## Usage windows

Every observed usage-limit event (any agent) is recorded to
`~/.claude-runner/windows.json`. See when windows closed and when they reopen:
```bash
claude-runner --windows      # per agent: hits in the last 7 days, last hit, next known reset
```

## Notifications
Secrets never go in the playbook. Either export them as env vars:
```bash
# Telegram:
export TELEGRAM_BOT_TOKEN=...    export TELEGRAM_CHAT_ID=...
# or ntfy:
export NTFY_TOPIC=my-claude-runs   # then subscribe to the topic in the ntfy app
```
…or put them in a **`.env` file next to the playbook** (auto-loaded on every run;
real env vars always take precedence). Never commit the `.env` file.
```
# .env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```
`notify_on_failure: true` (default) also pings you if a prompt hard-fails, then stops.

## State & logs
- `<playbook>.state.json` — next instruction index, threaded session id, running cost.
  Re-running resumes there; Ctrl-C flushes safely; `--restart` wipes it.
- `<playbook>.logs/` — `runner.log` + raw `claude` output per prompt/attempt, plus
  `run.pid` (the single-runner lock: a second runner on the same playbook refuses to
  start, so two processes can never share one session).

## Things worth knowing
- **No `--bare`, no API key**, on purpose: keeps the run on your OAuth login so it bills
  against your Max subscription, not per-token API billing.
- **Write prompts to be idempotent.** Resuming after a limit can occasionally lose track
  of progress; an idempotent prompt + a `verify:` gate makes re-entry safe.
- **Prompts are passed to `claude` via stdin** (UTF-8), so long `prompt_file` prompts,
  quotes, and non-ASCII text survive on every OS.
- **`CLAUDE_BIN`** env var overrides which `claude` binary the runner invokes (useful
  for testing or non-standard installs).

## Tests
```bash
python -m pytest tests
```
The suite runs the real runner and MCP tools against a stub `claude` binary (via
`CLAUDE_BIN`) that simulates success / usage-limit / transient responses — no actual
usage is consumed.

## Use it as a Claude Code tool (MCP)
`playbook_mcp.py` exposes the runner as Claude Code tools, so you can say "scaffold a
playbook", "start playbook.yaml", "what's the status" inside a session.

```bash
pip install mcp pyyaml
# register it (run in a terminal, not inside Claude Code):
claude mcp add --scope user --transport stdio claude-playbook \
    -- python3 /ABSOLUTE/PATH/playbook_mcp.py
```
The server finds the runner via `claude-runner` on PATH, a sibling `claude_runner.py`,
or the `CLAUDE_RUNNER` env var.

Tools:
- `scaffold_playbook(path=".")` — write a starter playbook to edit (no overwrite).
- `start_playbook(path)` — launch it **detached** and return pid + logfile.
- `playbook_status(path)` — phase (running/complete/stopped), N done / total, cost, log tail.
- `stop_playbook(path)` — stop it; progress is saved and `start_playbook` resumes it.

`start_playbook` is fire-and-forget on purpose: a run can wait out usage-limit windows
for hours, so it detaches and survives both this server and the calling session instead
of blocking it (which would burn the session's own usage). Check back with
`playbook_status`.

## Resuming an existing (interactive) session
A long interactive prompt about to hit the wall? Hand the session to the runner so it
waits out the reset and finishes unattended.

```bash
claude-runner --list-sessions            # find the session id (newest first)
```
Then point a one-prompt playbook at it (CLI flag overrides the playbook field):
```bash
claude-runner continue.yaml --resume-session <id>    # or: --resume-session latest
```
```yaml
# continue.yaml
notify_backend: telegram
notify_on_finish: true
resume_session: latest        # or a specific id
defaults: { cwd: ~/projects/verb-drill }
instructions:
  - prompt: "Continue exactly where you left off and finish the remaining work."
```
`latest` picks the newest transcript, preferring the project dir matching `cwd`. Session
transcripts live at `~/.claude/projects/<project>/<session-id>.jsonl` — the filename is
the id, so `claude --resume` (interactive picker) shows the same ids.

## Hand off the session you're in (`/playbook handoff`)
If you're *inside* a session that's about to hit the wall, capture it in one step instead
of copying ids around. From the stuck session:

1. **Esc** to interrupt the running turn (the session is saved).
2. `/playbook handoff`  → writes a `playbook.yaml` pinned to *this* session's id, with a
   "continue where you left off" prompt. (CLI equivalent: `claude-runner --handoff`.)
3. **Quit** the interactive session (so two processes don't share one session).
4. `claude-runner playbook.yaml --detach`  — or `/playbook start` from a fresh session.

It waits out the reset, resumes your conversation with full context, finishes, and pings
you. Works because the session you're in is the newest transcript for the project, so
`handoff` resolves and pins its id automatically.
