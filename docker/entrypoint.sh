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

# 2. Stage agent configs using install.sh (the single placement authority).
#    Guard on each agent's key file before requesting it — install.sh requires
#    managed-settings.json for claude-code (exit 4 if absent) and managed_config.toml
#    for codex managed mode.
#    ORDERING NOTE: install.sh must run here as root, BEFORE exec gosu below.
_install_agents=""
if [ -f /opt/agent-config/managed-settings.json ]; then
  _install_agents="claude-code"
fi
if [ -f /opt/agent-config-codex/etc/managed_config.toml ]; then
  _install_agents="${_install_agents:+${_install_agents},}codex"
fi
if [ -f /opt/agent-config-opencode/ai.opencode.managed.mobileconfig ]; then
  _install_agents="${_install_agents:+${_install_agents},}opencode"
fi

if [ -n "${_install_agents}" ]; then
  /usr/local/lib/unity-gateway/install.sh \
    --agents "${_install_agents}" \
    --claude-source /opt/agent-config \
    --codex-source /opt/agent-config-codex \
    --opencode-source /opt/agent-config-opencode \
    --profile "${DATABRICKS_PROFILE_NAME}"
else
  echo "[entrypoint] note: no Claude Code config at /opt/agent-config (run 'make docker-config')." >&2
  echo "[entrypoint] note: no Codex config at /opt/agent-config-codex (run 'make docker-config-codex')." >&2
  echo "[entrypoint] note: no opencode config at /opt/agent-config-opencode (run 'make docker-config-opencode')." >&2
fi

# 2b. Stage the DeepSeek Harness config. DSH has no managed system path: its home
#     patch + token plugin live in the dev user's $DSH_HOME (~/.dsh). Run the
#     user-scoped installer AS dev so the files are dev-owned, using the same real
#     installer the deploy path uses.
if [ -f /opt/agent-config-dsh/cordis.patch.yml ]; then
  gosu dev /usr/local/lib/unity-gateway/install-dsh-local.sh \
    --source /opt/agent-config-dsh/cordis.patch.yml \
    --target-dir /home/dev/.dsh \
    --no-backup
else
  echo "[entrypoint] note: no DeepSeek Harness config at /opt/agent-config-dsh (run 'make docker-config-dsh')." >&2
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
