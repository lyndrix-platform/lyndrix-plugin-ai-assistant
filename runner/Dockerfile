# syntax=docker/dockerfile:1
# lyndrix-ai-runner — Claude Code + the Lyndrix bridge (FastAPI) in one long-lived container.
# No credential is needed at build time; auth arrives at runtime via env or the /auth login flow.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.301
# Shared storage in Lyndrix is group 10000 (setgid dirs); keep the agent in that group.
ARG AGENT_UID=10000
ARG AGENT_GID=10000

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq openssh-client procps \
      python3 python3-venv python3-pip ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 # Mounted workspaces are owned by whoever cloned them on the host.
 && git config --system safe.directory '*'

# Claude Code CLI, pinned. The SDK wheel below bundles its own copy of the same
# version; we delete that one so exactly this binary is used (and ~270 MB saved).
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && npm cache clean --force \
 && claude --version

RUN groupadd -g ${AGENT_GID} agent \
 && useradd -m -u ${AGENT_UID} -g ${AGENT_GID} -s /bin/bash agent \
 && mkdir -p /workspace /opt/bridge /home/agent/.claude \
 && chown agent:agent /workspace /home/agent /home/agent/.claude

COPY requirements.txt requirements-dev.txt /opt/bridge/
RUN python3 -m venv /opt/bridge/venv \
 && /opt/bridge/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/bridge/venv/bin/pip install --no-cache-dir \
      -r /opt/bridge/requirements.txt -r /opt/bridge/requirements-dev.txt \
 && rm -f /opt/bridge/venv/lib/python3*/site-packages/claude_agent_sdk/_bundled/claude

COPY pyproject.toml entrypoint.sh /opt/bridge/
COPY bridge/ /opt/bridge/bridge/
RUN chmod 0755 /opt/bridge/entrypoint.sh \
 && chown -R agent:agent /opt/bridge/bridge /opt/bridge/pyproject.toml \
 && chown agent:agent /opt/bridge

ENV HOME=/home/agent \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    PATH=/opt/bridge/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONPATH=/opt/bridge \
    LYNDRIX_AI_WORKSPACE=/workspace \
    LYNDRIX_AI_BRIDGE_PORT=8765 \
    LYNDRIX_AI_CLAUDE_BIN=/usr/local/bin/claude \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

USER agent
WORKDIR /workspace
VOLUME ["/workspace"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS -H "X-Bridge-Token: ${LYNDRIX_AI_BRIDGE_TOKEN}" "http://127.0.0.1:${LYNDRIX_AI_BRIDGE_PORT:-8765}/health" >/dev/null || exit 1

ENTRYPOINT ["/opt/bridge/entrypoint.sh"]
