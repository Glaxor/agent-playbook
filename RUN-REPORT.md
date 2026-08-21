# Run report: roadmap/overnight

**Branch:** `roadmap/overnight` (from `main`)
**Status:** clean working tree, full test suite green (104/104), nothing pushed or merged.

## What was built

1. **`agent-playbook init`** (`claude_runner.py`, `tests/test_init.py`)
   A new CLI entry point that writes a small, runnable-as-is `playbook.yaml`
   starter — one `haiku`-model prompt that writes `hello.txt` with a
   cross-platform `verify:` line, `notify_backend: none`. Refuses to
   overwrite an existing file and prints the exact next steps
   (`--dry-run`, then a real run). The existing no-argument scaffold (a
   placeholder prompt the user must edit) and `--handoff` are unchanged.
   Covered by three tests: file created + strict-validates clean, refusal
   to overwrite, and the exact printed next-steps text/order.

2. **`examples/dependency-upgrade/`** (`playbook.yaml`, `README.md`)
   A production-style playbook for upgrading a Python project's
   dependencies overnight: an `audit` step ranks outdated packages into
   patch/minor/major risk tiers, one batch step per tier upgrades and
   commits only if `python -m pytest -q` passes (with `fix_attempts`
   self-healing and `protect: ["tests/**"]` so a struggling agent can't
   "fix" a test by editing it), `on_fail: goto rollback` on every step,
   and a gated rollback that reverts uncommitted work and writes
   `UPGRADE-NOTES.md`. Exactly one of two `when:`-gated `notify:` steps
   fires depending on outcome. Validated with
   `agent-playbook playbook.yaml --dry-run --strict` — zero warnings.
   The README documents that its `when:`/`verify:` gates are POSIX shell
   and need adapting (or git-bash/WSL) on stock Windows `cmd.exe`.

3. **README.md**
   Three-line quick start (`pipx install`, `agent-playbook init`, run it)
   moved to the top of Install. Added an Examples section linking
   `examples/dependency-upgrade` and `examples/failover-demo`, one
   sentence each.

## Commits on this branch

```
ceebf4b README: quick-start at the top of Install, add an Examples section
4dd93b7 Add examples/dependency-upgrade: safe overnight Python dependency upgrades
b50ddc9 Add `agent-playbook init` for a runnable-in-2-minutes starter playbook
```

## Verification performed

- `python -m pytest tests` — 104 passed, 0 failed, both before and after
  this run's re-check.
- `agent-playbook examples/dependency-upgrade/playbook.yaml --dry-run --strict`
  — exit 0, zero unknown-key warnings (also asserted directly on stdout/stderr).
- Manual CLI smoke test of `init`: creates the file, second run refuses to
  overwrite and leaves the original byte-for-byte unchanged, `--dry-run
  --strict` on the generated starter is clean.
- `git status` — clean at the start and end of this session.

## Skipped / uncertain

- **The dependency-upgrade playbook has never been run end-to-end** against
  a real Python project with a live agent — only structurally validated via
  `--dry-run --strict`. The audit/batch/rollback prompts are well-specified
  but unexercised; first real run should be watched, not left fully
  unattended.
- **`model: haiku` in the `init` template** relies on the same short-alias
  convention the README already documents for `model: sonnet`, but this
  wasn't independently verified against an installed `claude` CLI in this
  session (no real Claude Code binary was invoked — only the test suite's
  stub).
- **`playbook_mcp.py`'s `scaffold_playbook` MCP tool was not updated** to
  offer the new runnable-starter behavior — it still only exposes the
  original placeholder-prompt scaffold, so the CLI and MCP surface are now
  slightly out of parity.
- **No version bump.** `pyproject.toml` is still `0.5.0` despite a new CLI
  subcommand; the repo has no `CHANGELOG` file/convention to update either,
  so this was left to the maintainer's judgment.
- POSIX-only gate commands in the dependency-upgrade example are a known,
  documented limitation, not a bug — but stock-Windows users will need to
  adapt them before running as-is.

## Suggested next steps

1. Dry-run `examples/dependency-upgrade/playbook.yaml` for real against a
   small Python project (or a disposable clone) to confirm the audit/batch/
   rollback prompts behave as intended with an actual agent and test suite.
2. Decide whether `playbook_mcp.py`'s scaffold tool should also offer the
   `init` starter (e.g. a `runnable: bool` param or a second tool), so the
   MCP and CLI surfaces stay in sync.
3. If this is release-worthy, bump `pyproject.toml`'s version and tag,
   consistent with the existing `0.5.0` release commit style
   (`94eb1af Release 0.5.0: ...`).
4. Consider a Windows/PowerShell variant (or a cross-platform Python
   `verify:`/`when:` helper) for teams that can't rely on git-bash/WSL,
   since this is the second example (after the stress-test) to lean on
   POSIX-only gate commands.
