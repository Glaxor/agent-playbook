# Failover demo

See the headline feature in 60 seconds: an agent hits its usage limit and the
runner hands the same prompt to the next agent — no waiting for a reset.

`fake-codex` is a two-line script that always answers "usage limit". Pointing
`CODEX_BIN` at it simulates an exhausted Codex subscription; your real Claude
login does the rescue (uses one tiny haiku prompt).

```powershell
# Windows (PowerShell) — from this directory:
$env:CODEX_BIN = "$PWD\fake-codex.bat"
agent-playbook playbook.yaml
```

```bash
# macOS/Linux — from this directory:
chmod +x fake-codex.sh
CODEX_BIN=./fake-codex.sh agent-playbook playbook.yaml
```

You should see the switch happen within a second:

```
running prompt #1 [codex] (new session) attempt 1
[codex] usage limit — switching to next agent
running prompt #1 [claude] (new session) attempt 2
DONE prompt #1 [claude]
```

…and `rescued.txt` appears with the line `saved by the fallback agent`.
Afterwards, `agent-playbook --windows` shows the recorded codex limit event.
Re-run from scratch with `--restart` (state lives in `playbook.state.json`).
