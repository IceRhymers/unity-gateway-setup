# Isolated test harness for the generated coding-agent configs (gateway routing
# + OTEL telemetry). Runs on a machine that already has host-level settings,
# without touching them — Claude Code's config lives at the Linux enterprise path
# (/etc/claude-code/managed-settings.json) and Codex's at the dev user's
# $CODEX_HOME (/home/dev/.codex/config.toml) inside the container.
# BASE_DIGEST pins the manifest-list digest (multi-arch safe). Override with
# --build-arg BASE_DIGEST=sha256:... to test a newer base before committing the pin.
ARG BASE_DIGEST=sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
FROM node:22-bookworm-slim@${BASE_DIGEST}

# Toolchain: databricks CLI (auth + otelHeadersHelper), python3 (helpers),
# jq (the emit_hook_events.sh reporting hook builds its JSON payloads with it),
# socat (bridges the OAuth loopback callback to the host), gosu (drop to non-root).
# uv + ug (below) provide MCP-service discovery/registration.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq python3 socat gosu unzip \
    && rm -rf /var/lib/apt/lists/*

# Databricks CLI -> /usr/local/bin/databricks. Downloads the versioned zip and
# the SHA256SUMS file from the GitHub release, verifies the checksum with
# sha256sum -c, then unzips the binary. Fails the build on mismatch.
# See https://github.com/databricks/cli/releases.
ARG DATABRICKS_CLI_VERSION=1.14.1
RUN arch="$(dpkg --print-architecture)" \
    && zip="databricks_cli_${DATABRICKS_CLI_VERSION}_linux_${arch}.zip" \
    && sums="databricks_cli_${DATABRICKS_CLI_VERSION}_SHA256SUMS" \
    && base="https://github.com/databricks/cli/releases/download/v${DATABRICKS_CLI_VERSION}" \
    && curl -fsSL "${base}/${zip}" -o "/tmp/${zip}" \
    && curl -fsSL "${base}/${sums}" -o "/tmp/${sums}" \
    && grep " ${zip}\$" "/tmp/${sums}" > /tmp/check.sum \
    && (cd /tmp && sha256sum -c check.sum) \
    && unzip -o "/tmp/${zip}" databricks -d /usr/local/bin \
    && chmod +x /usr/local/bin/databricks \
    && rm "/tmp/${zip}" "/tmp/${sums}" /tmp/check.sum \
    && databricks --version | grep -q "${DATABRICKS_CLI_VERSION}"

# Claude Code + Codex + DeepSeek Harness + opencode CLIs (all npm-distributed).
# Claude Code: pkg @anthropic-ai/claude-code, bin claude.
# Codex: pkg @openai/codex, bin codex.
# DeepSeek Harness (dsh): pkg @deepseek-ai/dsh, bin dsh. Requires Node 22.19+,
#   satisfied by the node:22 base. `command -v dsh` verifies the bin without booting
#   the harness.
# opencode: pkg opencode-ai, bin opencode. Pinned to the latest-1 stable (1.x) channel.
# NPM_REGISTRY forwards a non-default registry (e.g. a corporate mirror); empty (the
# default) uses the public npm registry.
ARG NPM_REGISTRY=""
ARG CLAUDE_CODE_VERSION=2.1.259
ARG CODEX_VERSION=0.153.0
ARG DSH_VERSION=0.1.1-rc.2
ARG OPENCODE_VERSION=1.1.4
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} @openai/codex@${CODEX_VERSION} @deepseek-ai/dsh@${DSH_VERSION} opencode-ai@${OPENCODE_VERSION} \
    && claude --version && codex --version && command -v dsh && opencode --version | grep -q "${OPENCODE_VERSION}"

# uv (Astral) -> /usr/local/bin/uv, used to install ucode below. The version-pinned
# installer self-verifies embedded SHA256 checksums (primary integrity check);
# the version grep below is a fallback.
ARG UV_VERSION=0.12.9
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
      | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh \
    && uv --version | grep -q "${UV_VERSION}"

# ug (Unity AI Gateway coding CLI): discovers Databricks MCP services and
# registers them into a tool's user-level config, bridging each through the
# bundled `ug mcp-proxy` (no separate uvx uc-mcp-proxy needed). Requires
# Python 3.12+, which uv fetches and manages itself (bookworm's python3 is 3.11).
# Everything is installed into shared, world-readable /opt/uv with entry points
# on PATH via /usr/local/bin, so the non-root `dev` user can run `ug`.
# UCODE_SOURCE is pinned to a commit SHA by default (reproducible builds). Override
# with a mirror, a private URL with token, or a local path — mirrors the
# NPM_REGISTRY override pattern.
ARG UCODE_SOURCE="git+https://github.com/databricks/ucode@95999aa5188951662ac1ccc95e1c05599d601a32"
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
    && ug --version

# Non-root user for interactive sessions (Claude Code + auth). Managed settings
# are written root-owned at container start; the shell runs as this user.
# UID 1001: the node base image already uses 1000 (the `node` user).
RUN useradd -m -u 1001 -s /bin/bash dev \
    && mkdir -p /home/dev/work \
    && chown -R dev:dev /home/dev
ENV HOME=/home/dev
# opencode 1.1.4 predates automatic /etc/opencode directory loading (that
# feature was added after v1.1.4 in managed.ts). Point opencode at the staged
# managed config file explicitly. install.sh stages the file before exec gosu
# drops to dev, so the file is present when any opencode invocation runs.
# OPENCODE_CONFIG is a documented env var in opencode; it loads the named file
# as an additional config layer on top of the user config. If the file does not
# exist (no docker-config-opencode run), opencode's loadFile returns {} safely.
ENV OPENCODE_CONFIG=/etc/opencode/opencode.json
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
