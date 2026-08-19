# claude --playbook shim
# ---------------------------------------------------------------------------
# Adds a `claude --playbook <file> [flags]` subcommand that runs claude_runner,
# while every other `claude …` call passes straight through to the real binary.
#
# Install: append this to ~/.bashrc (or ~/.zshrc), then `source ~/.bashrc`.
#   curl/paste this file, or:  cat claude-playbook.sh >> ~/.bashrc
#
# Requires `claude-runner` on PATH (pipx install .). If you didn't install it,
# set RUNNER below to the script path, e.g.:
#   RUNNER=(python3 "$HOME/tools/claude_runner.py")
# ---------------------------------------------------------------------------
claude() {
    if [ "$1" = "--playbook" ]; then
        shift
        local RUNNER=(command claude-runner)
        # Fallback if claude-runner isn't installed:
        # local RUNNER=(python3 "$HOME/tools/claude_runner.py")
        "${RUNNER[@]}" "$@"
    else
        command claude "$@"
    fi
}
