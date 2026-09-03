# Isolated test harness for the generated coding-agent configs (gateway routing
# + OTEL telemetry). Runs on a machine that already has host-level settings,
# without touching them — Claude Code's config lives at the Linux enterprise path
# (/etc/claude-code/managed-settings.json) and Codex's at the dev user's
# $CODEX_HOME (/home/dev/.codex/config.toml) inside the container.
FROM node:22-bookworm-slim

# Toolchain: databricks CLI (auth + otelHeadersHelper), python3 (helpers),
# jq (the emit_hook_events.sh reporting hook builds its JSON payloads with it),
# socat (bridges the OAuth loopback callback to the host), gosu (drop to non-root).
# uv + ucode (below) provide MCP-service discovery/registration.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq python3 socat gosu unzip \
    && rm -rf /var/lib/apt/lists/*

# Databricks CLI (latest) -> /usr/local/bin/databricks
RUN curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh \
    && databricks --version

# Claude Code + Codex + DeepSeek Harness CLIs (all npm-distributed). NPM_REGISTRY
# forwards a non-default registry (e.g. a corporate mirror) from the builder's
# `npm config` — see the Makefile, which only passes it when set. Empty (the
# default) uses the public npm registry, so builds on machines without the mirror
# are unaffected. DeepSeek Harness (`dsh`) requires Node 22.19+, satisfied by the
# node:22 base. `command -v dsh` verifies the bin without booting the harness.
ARG NPM_REGISTRY=""
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install -g @anthropic-ai/claude-code @openai/codex @deepseek-ai/dsh \
    && claude --version && codex --version && command -v dsh

# uv (Astral) -> /usr/local/bin/uv, used to install ucode below.
RUN curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh \
    && uv --version

# ucode (Unity AI Gateway coding CLI): discovers Databricks MCP services and
# registers them into a tool's user-level config, bridging each through the
# bundled `ucode mcp-proxy` (no separate uvx uc-mcp-proxy needed). Requires
# Python 3.12+, which uv fetches and manages itself (bookworm's python3 is 3.11).
# Everything is installed into shared, world-readable /opt/uv with entry points
# on PATH via /usr/local/bin, so the non-root `dev` user can run `ucode`.
# UCODE_SOURCE is overridable (a pinned ref, a mirror, a private URL with token,
# or a local path) — mirrors the NPM_REGISTRY override pattern.
ARG UCODE_SOURCE="git+https://github.com/databricks/ucode"
# PYPI_INDEX: if the build host resolves PyPI through a proxy/mirror, the Makefile
# forwards it here (from uv's global config, mirroring NPM_REGISTRY) so uv can
# fetch ucode's build backend. Empty (default) uses public PyPI, so builds on
# unproxied machines are unaffected.
ARG PYPI_INDEX=""
ENV UV_TOOL_DIR=/opt/uv/tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN if [ -n "$PYPI_INDEX" ]; then export UV_DEFAULT_INDEX="$PYPI_INDEX"; fi \
    && uv tool install "$UCODE_SOURCE" \
    && chmod -R a+rX /opt/uv \
    && ucode --version

# Non-root user for interactive sessions (Claude Code + auth). Managed settings
# are written root-owned at container start; the shell runs as this user.
# UID 1001: the node base image already uses 1000 (the `node` user).
RUN useradd -m -u 1001 -s /bin/bash dev \
    && mkdir -p /home/dev/work \
    && chown -R dev:dev /home/dev
ENV HOME=/home/dev
WORKDIR /home/dev/work

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# install.sh: single placement authority for the MANAGED agent configs (Claude
# Code + Codex), called by entrypoint and by docker-reload (see Makefile). Baked
# into the image so the harness always exercises the real installer (plan P5 —
# the harness is the test).
COPY agent_setups/deploy/install.sh /usr/local/lib/unity-gateway/install.sh
RUN chmod +x /usr/local/lib/unity-gateway/install.sh

# install-dsh-local.sh: the user-scoped installer for DeepSeek Harness. DSH has no
# managed system path — its config lives in the dev user's $DSH_HOME (~/.dsh) — so
# the harness exercises the real per-user installer, run as `dev` by the
# entrypoint and docker-reload.
COPY agent_setups/deploy/install-dsh-local.sh /usr/local/lib/unity-gateway/install-dsh-local.sh
RUN chmod +x /usr/local/lib/unity-gateway/install-dsh-local.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
