#!/usr/bin/env bash
# Container entrypoint: prepare the agent HOME for Claude Code, then run the bridge.
# All logic lives in Python (bridge.prepare_home / bridge.__main__); this file only wires them.
set -euo pipefail

# A runtime `--user` override may leave HOME unwritable; Claude Code must be able
# to write its config, so fall back to a private HOME in that case.
if [ ! -w "${HOME:-/home/agent}" ]; then
  export HOME=/tmp/agent-home
  export CLAUDE_CONFIG_DIR="${HOME}/.claude"
  mkdir -p "${CLAUDE_CONFIG_DIR}"
fi

/opt/bridge/venv/bin/python -m bridge.prepare_home
exec /opt/bridge/venv/bin/python -m bridge
