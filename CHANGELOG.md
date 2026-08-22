# Changelog

All notable changes to agent-playbook (published on PyPI as
[`llm-agent-playbook`](https://pypi.org/project/llm-agent-playbook/)).

Versions 0.5.0–0.7.0 were implemented by agent-playbook itself, running
playbooks against this repository unattended — each run gated by the full test
suite, guarded by its predecessor's safety features, and human-reviewed before
merge. The run logs live in `docs/RUN-REPORT-*.md`.

## [0.7.0] — 2026-08-22

- **`--watch`**: attach read-only to a running playbook — one-line status
  (phase, N/total instructions, cost so far), then a live `tail -f`-style
  stream of the runner log. Exits on its own when the run ends; Ctrl-C only
  detaches, never touches the run. Shows the runner's log stream, not the
  agent's live keystrokes (attempt output is written when an attempt ends).

## [0.6.0] — 2026-08-21

- **`agent-playbook init`**: one-command first run — writes a small starter
  `playbook.yaml` that is runnable as-is (haiku, ~2 minutes, no editing
  needed) and prints the exact next steps. Refuses to overwrite.
- **`examples/dependency-upgrade`**: a production-shaped playbook for safe
  overnight dependency upgrades — risk-tiered batches, test suite as the gate
  after each batch, self-healing repair rounds, commit-per-green-batch, and a
  rollback path that only ever reverts uncommitted work.
- README: quick-start at the top of Install; new Examples section.

## [0.5.0] — 2026-08-21

- **`protect:`** (per-instruction or in `defaults:`): files/globs the agent
  must not touch — typically the test suite it is supposed to satisfy. Files
  are hashed before the agent runs; any change, deletion, or new matching
  file is treated exactly like a failed `verify:`, fed back through
  `fix_attempts` with a restore instruction, then subject to `on_fail`.
  Exists because a self-healing loop must not be able to "fix" a failing
  test by editing the test.
- **Run reports**: every run now ends by writing `<playbook>.report.md` —
  outcome, duration, a per-instruction table (status / attempts / cost),
  total cost, and the last log lines on failure. Written on every ending:
  complete, hard failure, budget stop, or `stop.request`.
- **Unknown-key warnings**: keys the runner would silently ignore (usually
  typos: `on_fial`) now warn with a did-you-mean suggestion; `--strict`
  turns the warnings into errors for CI. `--dry-run` is documented as the
  playbook validator.
- `examples/stress-test`: a playbook exercising every feature at once.

## [0.4.1] — 2026-08-21

- Hardening: malformed playbooks (invalid YAML, wrong structure, bad types,
  missing `prompt_file`) now produce one-line errors instead of tracebacks;
  a missing `prompt_file` follows `on_fail` policy like any other failure.
- 16 robustness tests: corrupt state recovery, resume-after-failure session
  threading, `--restart`/`--from`, soft-failing notifications, non-ASCII
  prompts. One of them caught a real encoding bug that only reproduced on
  stock Windows CI runners.

## [0.4.0] — 2026-08-21

First PyPI release (as `llm-agent-playbook`; the commands remain
`agent-playbook` and the legacy `claude-runner`).

- **Control flow**: `on_fail: stop | continue | goto <label>` per instruction
  (verify failures included), `label:` targets, `when:` shell gates that skip
  an instruction, and a `limits.max_gotos` loop guard. Startup validation
  rejects duplicate or unknown labels.
- **Docker support**: image bundling the runner + Claude Code CLI, a CI
  build-and-test job, and a "Run with Docker" README section.
- README demo GIF: a real, unedited self-healing run.

## [0.3.0] — 2026-08-21 (git only, pre-PyPI)

- Safety tier: `timeout_min` with process-tree kill, `max_cost_usd` budget
  cap, `max_attempts` runaway guard, and `fix_attempts` self-healing verify
  (failure output fed back to the same session).
- Multi-agent foundation: Claude Code, Codex CLI, and Gemini CLI adapters
  with `fallback_agents` on usage limits; usage-window tracking
  (`--windows`); GitHub Actions CI across 3 OSes × Python 3.10/3.13.
