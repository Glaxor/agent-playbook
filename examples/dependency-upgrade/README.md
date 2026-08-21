# Dependency upgrade

Point this at a real Python project and let it upgrade dependencies overnight,
least-risky first, with a real test suite as the safety gate and an automatic
revert if a batch can't be made to pass.

## What it does

1. **`audit`** — runs `pip list --outdated` (and `pip-audit` if installed) and
   writes `UPGRADE-PLAN.md`: every outdated package sorted into three tiers —
   `patch` (bugfix bumps), `minor` (backwards-compatible features), `major`
   (potentially breaking) — least risky first. Nothing is installed yet.
2. **`batch-patch` / `batch-minor` / `batch-major`** — one batch per tier.
   Each batch upgrades only its tier's packages, then runs
   `verify: python -m pytest -q`. A failing suite is fed back to the *same*
   Claude session for up to `fix_attempts` self-repair rounds before the
   batch counts as failed. A batch that goes green commits its own changes,
   so a later batch's rollback never touches earlier, already-committed work.
   `protect: ["tests/**"]` stops a struggling agent from "fixing" a failing
   test by editing the test instead of the code.
3. **`rollback`** — any batch (or the audit step) that's still failing after
   its repair rounds jumps here via `on_fail: goto rollback`. It reverts only
   the *uncommitted* changes from the batch in progress and writes
   `UPGRADE-NOTES.md`: which batch failed, which package it was upgrading,
   the tail of the test output, and which tiers (if any) landed successfully
   first.
4. **Exactly one notification fires** — success if every batch went green,
   or failure (pointing at `UPGRADE-NOTES.md`) if rollback ran. Only one of
   the two `when:`-gated end states is ever true, so you never get both.

## Before you run it

- Edit `defaults.cwd` in `playbook.yaml` to point at your actual Python
  project checkout (currently a placeholder, `~/projects/my-python-app`).
- The project needs a `requirements.txt` or `pyproject.toml` with pinned
  versions and a `pytest` suite that's currently green — that suite *is* the
  safety net every batch is judged against.
- Set a notification backend (or leave `notify_backend: none`):
  ```bash
  export NTFY_TOPIC=dependency-upgrade-run    # or export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
  ```

## POSIX gate commands

Every `when:`/`verify:` line here (`test ! -f UPGRADE-PLAN.md`,
`git status --porcelain | grep -q .`, ...) is POSIX shell. The runner
executes these with `shell=True`, which on stock Windows is `cmd.exe` and
doesn't understand `test`/`grep`/pipes the way a POSIX shell does. Run this
playbook from git-bash, WSL, macOS, or Linux — or rewrite the gate commands
in PowerShell before using it as-is on stock Windows `cmd.exe`.

## Run it

```bash
# validate first — checks structure, labels, on_fail targets, and warns
# (as errors, with --strict) about any typo'd keys:
agent-playbook playbook.yaml --dry-run --strict

# then let it run overnight, surviving a closed terminal:
agent-playbook playbook.yaml --detach
```

Re-running is safe: the audit step, and each batch's own idempotency
instructions, mean a restart after a fix or a usage-limit wait picks up
where it left off rather than redoing finished work. Start over from
scratch with `--restart`.
