# Run report — branch `improve/watch`

**Status:** clean working tree, full test suite green (108/108), nothing pushed or
merged.

## What was built

**`--watch` — read-only attach to a playbook's run** (`claude_runner.py`,
`tests/test_watch.py`, `README.md`)

A new flag that lets a second terminal observe a run (live, detached, or already
finished) without touching it. On start it prints one status line — playbook path,
phase (`not started`/`running`/`stopped`/`complete`, computed the same way
`playbook_mcp.core_status` already does: `run.pid` liveness + saved state), how many
instructions are done of the total, and cost so far. It then streams
`<playbook>.logs/runner.log` like `tail -f`, starting from the last 10 lines for
context, polling the file by position roughly twice a second — pure Python, no
external tools, works the same on POSIX and Windows. It exits on its own, printing a
final status line, once the run is no longer alive (or immediately, if no run was
active to begin with); Ctrl-C exits just as cleanly. It never writes to state, pid, or
log files — only reads them. Documented honestly, in both the code and the README,
that because an agent's own output only lands once its attempt finishes, `--watch`
shows the *runner's* log stream (phase changes, `DONE`/`FAILED` lines, usage-limit
waits) — not an agent's live keystrokes as it works.

Phase computation and pid handling reuse the existing `pid_alive()` and state
helpers (`load_state`) rather than duplicating that logic a third time (it already
exists once in the main run loop and once in `playbook_mcp.py`).

Covered by four tests: watching a completed run prints the summary and exits 0;
watching a playbook with no state/logs says so and exits 0; attaching to a live
detached run streams a `DONE` line for an instruction that finishes mid-watch and
then exits 0 on its own when the run completes; and a read-only guarantee test that
diffs state/log file mtimes and contents before and after a watch pass.

## Commits made this session (on `improve/watch`, none pushed)

- `6d1ff87` Add `--watch`: read-only attach to a playbook's run and stream its log
  (+ `tests/test_watch.py`, 4 tests)
- `d024d48` README: tighten the `--watch` paragraph to match the surrounding notes'
  voice

Full suite: **108 passed**, working tree clean (re-verified at the start of this
report).

## Open questions

- **`--watch` has no MCP-layer counterpart.** `playbook_mcp.py` exposes
  `start_playbook`/`playbook_status`/`playbook_logs`/`stop_playbook` but nothing that
  mirrors the CLI's new read-only streaming attach — `playbook_status`/`playbook_logs`
  already cover the same data as one-shot snapshots, but not the "stream as it
  happens" experience. Worth deciding if that's in scope or if snapshots are enough
  for the MCP surface.
- **Never smoke-tested against a real `claude` CLI process** — only against the
  stub harness. The stub's `DONE`/log-line shapes match the real adapter's, but a
  live run watched by a live `--watch` in a second terminal hasn't been eyeballed.
- **Only verified on Windows in this sandbox.** The polling loop is plain
  `open()`/`seek()`/`read()` with no OS-specific calls, so POSIX should behave the
  same, but that's an expectation, not something this session confirmed directly.
- **Dev-environment quirk, not a code issue (recurring):** this sandbox leaks
  `CLAUDE_RUNNER_DETACHED=1` into the ambient shell environment, which suppresses
  `runner.log` creation for a foreground run. `tests/test_watch.py` clears it
  explicitly (same pattern as `tests/test_report.py`) — worth knowing if `--watch`
  ever looks like it has nothing to stream when testing manually in a similar nested
  shell.
- **Ctrl-C's exit code (130) is a convention choice**, matching the existing
  `_bye()` signal handler elsewhere in the runner, but no test asserts it directly —
  sending SIGINT to a *watch* process (as opposed to a *run*) isn't exercised by the
  suite.
- **No version bump / CHANGELOG entry** for the new flag — left to whoever cuts the
  next release.
