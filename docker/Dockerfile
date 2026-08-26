# Isolated test harness for the generated Claude Code managed-settings.json
# (routing + OTEL telemetry). Runs on a machine that already has host-level
# managed settings, without touching them — the config lives at the Linux
# enterprise path (/etc/claude-code/managed-settings.json) inside the container.
FROM node:22-bookworm-slim

# Toolchain: databricks CLI (auth + otelHeadersHelper), python3 (helpers),
# socat (bridges the OAuth loopback callback to the host), gosu (drop to non-root).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 socat gosu unzip \
    && rm -rf /var/lib/apt/lists/*

# Databricks CLI (latest) -> /usr/local/bin/databricks
RUN curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh \
    && databricks --version

# Claude Code. NPM_REGISTRY forwards a non-default registry (e.g. a corporate
# mirror) from the builder's `npm config` — see the Makefile, which only passes
# it when set. Empty (the default) uses the public npm registry, so builds on
# machines without the mirror are unaffected.
ARG NPM_REGISTRY=""
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install -g @anthropic-ai/claude-code

# Non-root user for interactive sessions (Claude Code + auth). Managed settings
# are written root-owned at container start; the shell runs as this user.
# UID 1001: the node base image already uses 1000 (the `node` user).
RUN useradd -m -u 1001 -s /bin/bash dev \
    && mkdir -p /home/dev/work \
    && chown -R dev:dev /home/dev
ENV HOME=/home/dev
WORKDIR /home/dev/work

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
