# claude-playbook (Claude Code plugin)

Bundles the `/playbook` command and the playbook MCP tools (handoff / scaffold / start /
status / logs / stop) into one installable plugin.

## Prereqs (one-time)
```bash
pip install mcp pyyaml
```

## Install
Load it for a session straight from disk:
```bash
claude --plugin-dir /path/to/claude-playbook
```
Or register + install persistently:
```text
/plugin marketplace add /path/to/claude-playbook
/plugin install claude-playbook
```

## Use
```text
/playbook new          # blank starter playbook to edit
/playbook handoff      # capture THIS session (about to hit the limit) into a playbook
/playbook start        # run it detached; waits out usage limits, continues
/playbook status       # phase, done/total, cost, recent log
/playbook logs 200     # longer log slice (optional filter)
/playbook stop         # stop; progress saved, resumable
```

### Rescue a session about to hit the wall
From inside the stuck session: **Esc** to interrupt → `/playbook handoff` (pins this
session) → **quit** the session → `/playbook start` from a fresh session (or
`agent-playbook playbook.yaml --detach`). It resumes your conversation after the reset and
pings you when done. Quitting first matters: two processes must not share one session.

The runner uses your Claude Code OAuth login (Max subscription), not an API key.

Works on Linux, macOS, and Windows: status checks and stop are safe on all three
(`stop` sends SIGTERM on POSIX, `taskkill` + a graceful `stop.request` file on Windows;
progress is saved either way and `/playbook start` resumes it).
