# Run report — branch `improve/guard-report`

## What was built

**1. `protect:` — a verify-tamper guard**
A new optional `protect:` key (list of glob paths, resolved relative to the
instruction's `cwd`; settable per-instruction or in `defaults:`) hashes every
matched file before an instruction's agent call. If any matched file is
changed, deleted, or a new file appears matching the glob, the attempt is
treated exactly like a failed `verify:` — logged, fed back through
`fix_attempts` with an explicit "restore these files" instruction, and
`on_fail` applies once attempts are exhausted. Exists because self-healing
feeds verify failures back to the same agent, so without a guard a struggling
agent can "fix" a failing test by editing the test instead of the code.

The tamper baseline is captured **once, at the start of the instruction**,
not re-taken before every retry/fix attempt. This is deliberate: comparing
attempt-to-attempt would flag a legitimate restore (fix round 2 correctly
undoing round 1's damage) as a fresh tamper, since it differs from the
just-tampered prior snapshot. Comparing against a fixed baseline lets a
genuine restore be recognized as clean.

**2. Run report**
Every run now writes `<playbook stem>.report.md` next to the state file,
overwriting any previous one, at the end of every run: complete, hard
failure, budget stop, or `stop.request`. Contents: outcome line (with
location for anything but a clean complete), start/end time and duration, a
per-instruction table (kind / label / status / attempts / cost), total
notional cost, goto-jump count if any, and the last 5 runner-log lines on
failure. Per-instruction status/attempts/cost is persisted in
`state["instr_status"]` so the report stays accurate across resumed runs.
Report generation is wrapped in `finish()`'s try/except so a report bug can
only log — it can never change the run's actual exit code — and the
`instr_status` bookkeeping itself defensively resets to a fresh list if the
state file is corrupted, so a stale/hand-edited `state.json` can't crash a
run before it even starts.

**3. README** — documented both features in place (Safety nets & self-healing
for `protect:`, State & logs for the report), including the "why" for
`protect:`.

## Commits made this session (on `improve/guard-report`, none pushed)

- `227efdb` Add `protect:` — a verify-tamper guard for files the agent must
  not touch (+ `tests/test_protect.py`, 8 tests; extended the stub harness
  with `write_files`/`delete_files` so a stub attempt can simulate an agent
  editing/deleting files)
- `6112356` Add a run report written at the end of every run
  (+ `tests/test_report.py`, 8 tests)
- `6e76c83` Explain why `protect:` exists in the README

Full suite: **100 passed**, working tree clean.

## Open questions

- **Signal exits don't get a report.** `SIGINT`/`SIGTERM` go through the
  `_bye()` handler, which saves state and calls `sys.exit(130)` directly —
  it doesn't route through `finish()`, so no report is written for a
  Ctrl-C'd run. Only `stop.request` (the documented graceful-stop path) does.
  Worth deciding if Ctrl-C should also produce a report, or if that's
  intentionally out of scope (signal handlers should stay minimal).
- **`attempts` in the report is a single combined counter** — it doesn't
  distinguish a transient-backoff retry, a chain fallback to another agent,
  and a `fix_attempts` round. Fine for "how much did this cost to grind
  through," but if someone wants to debug *why* an instruction took 6
  attempts, the report alone won't say. The per-attempt `.log` files under
  `<playbook>.logs/` still have that detail.
- **`protect`'s baseline-per-instruction semantics** (see above) are
  covered by a test (`test_protect_fix_attempt_restores_and_recovers`) but
  are a real design choice, not the only reasonable reading of "hash before
  each agent call" — worth a second pair of eyes before relying on it for
  anything security-sensitive (it's a guard against a *cooperative* but
  struggling agent, not an adversarial one: hashes are recomputed from
  scratch each run, not cryptographically pinned anywhere).
- **Dev-environment quirk, not a code issue:** this sandbox had
  `CLAUDE_RUNNER_DETACHED=1` leaking into the ambient shell environment
  (apparently from an outer detached agent-playbook run), which suppresses
  `runner.log` creation. The new report tests explicitly clear that env var
  before asserting on the log-tail section; worth knowing if report-tail
  behavior ever looks wrong when testing manually in a similar nested shell.
- **No version bump / CHANGELOG entry** for either feature — left to
  whoever cuts the next release to decide semver bump + PyPI publish.
