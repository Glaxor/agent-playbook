# claude --playbook shim (PowerShell)
# ---------------------------------------------------------------------------
# Adds a `claude --playbook <file> [flags]` subcommand that runs claude_runner,
# while every other `claude ...` call passes straight through to the real binary.
#
# Install: add this file's contents to your PowerShell profile, then reload:
#   notepad $PROFILE          # paste this in (create the file if it doesn't exist)
#   . $PROFILE
#
# Requires `claude-runner` on PATH (pipx install .). If you didn't install it,
# replace the claude-runner line with:
#   python "$HOME\tools\claude_runner.py" @rest
# ---------------------------------------------------------------------------
function claude {
    if ($args.Count -ge 1 -and $args[0] -eq '--playbook') {
        $rest = @($args | Select-Object -Skip 1)
        claude-runner @rest
    } else {
        # -CommandType Application skips this function itself, so no recursion.
        $real = (Get-Command claude -CommandType Application | Select-Object -First 1).Source
        & $real @args
    }
}
