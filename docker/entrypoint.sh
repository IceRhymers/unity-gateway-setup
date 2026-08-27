#!/usr/bin/env bash
# Container start: write an isolated databricks config, stage the generated
# Claude Code managed settings, bridge the OAuth callback, then run as `dev`.
set -euo pipefail

: "${DATABRICKS_WS_HOST:?DATABRICKS_WS_HOST must be set (workspace URL)}"
: "${DATABRICKS_PROFILE_NAME:=fevm-west}"

# 1. Fresh, isolated ~/.databrickscfg for the dev user. Both DEFAULT and the
#    named profile point at the workspace, so `databricks auth login` (bare) and
#    the settings' `--profile ${DATABRICKS_PROFILE_NAME}` calls both resolve.
cfg="/home/dev/.databrickscfg"
cat > "$cfg" <<EOF
[DEFAULT]
host = ${DATABRICKS_WS_HOST}

[${DATABRICKS_PROFILE_NAME}]
host = ${DATABRICKS_WS_HOST}
EOF
chown dev:dev "$cfg"
chmod 600 "$cfg"

# 2a. Stage the generated Claude Code config as Linux enterprise-managed settings
#     (root-owned, matching how MDM would deploy it). Mounted read-only at
#     /opt/agent-config.
if [ -f /opt/agent-config/managed-settings.json ]; then
  mkdir -p /etc/claude-code
  cp /opt/agent-config/managed-settings.json /etc/claude-code/managed-settings.json
  if [ -f /opt/agent-config/otel-headers-helper.sh ]; then
    cp /opt/agent-config/otel-headers-helper.sh /etc/claude-code/otel-headers-helper.sh
    chmod +x /etc/claude-code/otel-headers-helper.sh
  fi
else
  echo "[entrypoint] note: no Claude Code config at /opt/agent-config (run 'make docker-config')." >&2
fi

# 2b. Stage the generated Codex config. Codex has no OS-level managed path — it
#     reads $CODEX_HOME/config.toml per user — so this lands in the dev user's
#     home (dev-owned), mounted read-only at /opt/agent-config-codex.
if [ -f /opt/agent-config-codex/config.toml ]; then
  mkdir -p /home/dev/.codex
  cp /opt/agent-config-codex/config.toml /home/dev/.codex/config.toml
  chown -R dev:dev /home/dev/.codex
else
  echo "[entrypoint] note: no Codex config at /opt/agent-config-codex (run 'make docker-config-codex')." >&2
fi

# 3. Bridge the OAuth loopback callback. The databricks CLI's login listener binds
#    127.0.0.1:8020 (loopback only), which a docker -p mapping cannot reach. socat
#    listens on the container's external IP :8020 and forwards to that loopback, so
#    the redirect from the host browser (localhost:8020 -> -p 8020:8020) completes.
if command -v socat >/dev/null 2>&1; then
  ip="$(hostname -i 2>/dev/null | awk '{print $1}')"
  if [ -n "${ip:-}" ]; then
    socat "TCP-LISTEN:8020,bind=${ip},fork,reuseaddr" TCP:127.0.0.1:8020 \
      >/tmp/socat-oauth.log 2>&1 &
    echo "[entrypoint] OAuth callback bridge on ${ip}:8020 -> 127.0.0.1:8020" >&2
  fi
fi

# 4. Run the container command as the non-root dev user.
exec gosu dev "$@"
