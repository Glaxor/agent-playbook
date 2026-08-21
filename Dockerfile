# agent-playbook in a container: the Python runner plus the Claude Code CLI.
#   Build:  docker build -t agent-playbook .
#   Run:    docker run --rm -v ~/.claude:/root/.claude -v "$PWD":/work agent-playbook playbook.yaml
FROM python:3.13-slim

# Claude Code needs Node 18+; git and ca-certificates for agent work inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm git ca-certificates \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agent-playbook
COPY pyproject.toml claude_runner.py ./
RUN pip install --no-cache-dir .

# Mount the directory holding your playbook here; state and logs land next to
# the playbook on the host, so re-running the container resumes as usual.
WORKDIR /work
ENTRYPOINT ["agent-playbook"]
CMD ["--help"]
