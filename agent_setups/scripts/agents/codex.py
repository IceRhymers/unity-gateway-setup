"""Codex CLI config.toml generator for the Databricks Unity AI Gateway.

Turns the deployed model services (from Terraform outputs) into a single,
self-contained `config.toml` that routes the Codex CLI through
`<host>/ai-gateway/mlflow/v1` (the MLflow serving route — the real model-inference
surface) with U2M OAuth. Codex speaks the Responses wire API, so it posts to
`<base_url>/responses`, landing on `mlflow/v1/responses`.

Why this is *only* gateway routing
----------------------------------
A working Codex install typically also carries a lot of ChatGPT-desktop-app
machinery — plugins, marketplaces, `node_repl`, computer-use, `CODEX_CLI_PATH`
pointing at `/Applications/ChatGPT.app`, etc. None of that is reproduced here: it
is installed by the ChatGPT app itself and is machine-specific. Per the Databricks
docs, the ChatGPT app need not even be running for the CLI to use the gateway, so
this generator emits the gateway provider + model + auth and nothing else.

Codex vs. Claude Code — two different deployment models
-------------------------------------------------------
Claude Code reads an OS-level, root-owned `managed-settings.json` (MDM-enforced).
Codex has no such system path: it reads `$CODEX_HOME/config.toml` (default
`~/.codex/config.toml`) per user, and `-p <name>` layers
`$CODEX_HOME/<name>.config.toml` on top. So the generated file is deployed into a
user's `$CODEX_HOME` (or dropped in as `databricks.config.toml` and activated with
`codex -p databricks`), not pushed to a system path.

Responses surface
-----------------
Codex sets `wire_api = "responses"`, so it POSTs to `<base_url>/responses`. We
point `base_url` at `/ai-gateway/mlflow/v1` — the gateway's MLflow serving route,
which is the actual model-inference surface — so Codex lands on
`mlflow/v1/responses`. That's the broad Responses surface (a superset of
`openai/v1/responses`): GPT, Gemini, Claude, and the open models are all reachable.
`--api-type` narrows it (e.g. to `openai/v1/responses`), and `--gateway-path`
overrides the route (e.g. back to `/ai-gateway/codex/v1`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request

from agents.base import AgentGenerator
from gateway import Endpoint, GatewayContext, discover_api_types

# Codex speaks the Responses wire API. We route through the MLflow serving surface
# (mlflow/v1/responses is the broad Responses surface — a superset of
# openai/v1/responses), so we filter endpoints on it by default.
DEFAULT_API_TYPE = "mlflow/v1/responses"
NARROW_API_TYPE = "openai/v1/responses"

# Gateway route Codex posts to. The MLflow serving route is the real inference
# surface; Codex appends /responses, so <base_url>/responses = mlflow/v1/responses.
DEFAULT_GATEWAY_PATH = "/ai-gateway/mlflow/v1"

# Preferred default model, by endpoint leaf name. First match wins; falls back
# to the first discovered endpoint if none are present.
DEFAULT_MODEL_PREFERENCES = ["kimi-k3", "gpt", "gpt-sol", "gpt-5-6-sol", "gpt-5-5", "gpt-5-6-luna"]

# Codex reasoning-effort levels (mirrors the CLI's own enum).
REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]

# Where Codex reads its config (documentation only; nothing is written there).
CODEX_HOME_DEFAULT = "~/.codex"

# --- Hook telemetry (custom reporting events via Zerobus REST) --------------
# Codex ships a `[hooks]` system (config.toml or a standalone hooks.json) that is
# a near-clone of Claude Code's: stdin-JSON in, stdout-JSON out, a regex `matcher`
# on the tool name, and the same snake_case payload fields (session_id, tool_name,
# tool_input, tool_response, agent_type). So the delivery half of the emitter —
# the Zerobus M2M token mint, the per-session spool, the batched flush — is reused
# verbatim from the Claude Code emitter (same UC table, service principal, and UC
# secret); only the per-event handlers differ.
#
# Categories that DON'T port (dropped here): 'reliability' — Codex has no error/
# failure hook (turn failures surface only in `codex exec --json`, not the TUI);
# and Claude-only tool signals (skill_used: no Skill tool; doc_read: no Read tool
# surfaced to hooks). The clean set is governance (Bash risk/secret prefilter),
# adoption (pr_pushed from Bash), and usage (subagent_used from SubagentStart).
HOOK_CATEGORIES = ("usage", "governance", "adoption")
HOOK_SCRIPT_FILENAME = "emit_hook_events.sh"
HOOKS_JSON_FILENAME = "hooks.json"

# Regex matcher (on tool_name) for the shell-execution tool(s) governance/adoption
# key off. Verified against the codex-cli 0.150.1 binary's string table: the runtime
# tool_name is "shell" (137 refs), with "unified_exec"/"exec_command"/"local_shell"
# as sibling exec-tool variants; "Bash" appears once (a CC-compat alias mention), so a
# "^Bash$" matcher would fire on NOTHING. We match the full shell-exec set so the scan
# fires regardless of which variant a given Codex build/config activates — scanning any
# shell command for risky patterns/secrets is correct either way. (apply_patch,
# spawn_agent, web_search are deliberately excluded — not shell commands.)
SHELL_TOOL_MATCHER = "^(Bash|shell|local_shell|exec_command|unified_exec)$"

# --- Deployment layout ------------------------------------------------------
# Codex reads three system-level files under /etc/codex (verified against the
# codex-cli 0.150.1 binary; managed_config.toml is loaded and OVERRIDES the user's
# ~/.codex/config.toml — confirmed empirically): config.toml, managed_config.toml,
# and requirements.toml. We use the latter two as an MDM-style enforcement layer —
# the Codex analogue of Claude Code's root-owned managed-settings.json.
MANAGED_ETC_DIR = "/etc/codex"
MANAGED_CONFIG_FILENAME = "managed_config.toml"
REQUIREMENTS_FILENAME = "requirements.toml"
# Managed mode references the emitter by ABSOLUTE root-owned path — no $CODEX_HOME
# expansion needed (and no ambiguity about whether Codex runs the command via a shell).
MANAGED_HOOK_SCRIPT_PATH = f"{MANAGED_ETC_DIR}/{HOOK_SCRIPT_FILENAME}"

# User (non-managed) mode: Codex has no fixed managed dir, so the script is deployed
# into the developer's $CODEX_HOME (default ~/.codex) and referenced with a
# shell-expanded fallback. Override with --hook-script-path if you deploy it elsewhere.
DEFAULT_HOOK_SCRIPT_PATH = "${CODEX_HOME:-$HOME/.codex}/emit_hook_events.sh"

# Dispatcher script. One file, one subcommand per hook event. Sentinels (__NAME__)
# are replaced with baked defaults at generation time; each is env-overridable so
# one script serves a whole fleet. Report-only: it never blocks a tool call and
# always exits 0 (telemetry must not disrupt the developer).
_HOOK_DISPATCHER_TEMPLATE = r"""#!/usr/bin/env bash
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
#
# Codex CLI hook -> Zerobus REST reporting event emitter.
#   usage: emit_hook_events.sh <pretool|posttool|subagent|flush>
#
# Reads the hook payload on stdin, appends ONE event to a per-session spool
# (instant, no network), and drains the spool as a batched insert to the Zerobus
# REST endpoint at turn/session boundaries, authenticating as the telemetry
# service principal. Report-only and non-blocking: any failure is swallowed and
# the script always exits 0.
#
# Mirrors the Claude Code emitter (same UC table, SP, and Zerobus auth). Codex has
# no error/failure hook, so the 'reliability' category has no analogue here.
set -u

# --- baked config (override via environment for fleet reuse) ---------------
ZEROBUS_ENDPOINT="${ZEROBUS_ENDPOINT:-__ENDPOINT__}"
HOOK_EVENTS_TABLE="${HOOK_EVENTS_TABLE:-__TABLE__}"
SECRET_FULL_NAME="${ZEROBUS_UC_SECRET:-__SECRET__}"
PROFILE="${DATABRICKS_PROFILE:-__PROFILE__}"
DATABRICKS_BIN="${DATABRICKS_BIN:-__DBX_BIN__}"
TOKEN_HOST="${DATABRICKS_HOST:-__HOST__}"
ENABLED_CATEGORIES="${HOOK_EVENTS_CATEGORIES:-__CATEGORIES__}"
TOKEN_TTL="${HOOK_EVENTS_TOKEN_TTL:-__TTL__}"
AGENT="codex"

# Telemetry must never block or error the developer.
command -v jq >/dev/null 2>&1 || exit 0
command -v curl >/dev/null 2>&1 || exit 0
[ -n "$ZEROBUS_ENDPOINT" ] || exit 0

SUB="${1:-}"
INPUT="$(cat)"

CACHE_DIR="${HOME}/.cache/unity-gateway"
TOKEN_FILE="${CACHE_DIR}/zerobus_token"
EXP_FILE="${CACHE_DIR}/zerobus_token.exp"
SPOOL_DIR="${CACHE_DIR}/spool"
WS_USER_FILE="${CACHE_DIR}/ws_user"

# Per-session spool file. Events are appended here (instant, no network) and
# drained by flush_spool at turn/session boundaries. Keyed by session so
# concurrent sessions don't interleave, and sanitized for the filesystem.
_session_id() { printf '%s' "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null; }
_spool_file() {
  local s; s="$(_session_id)"
  s="$(printf '%s' "${s:-unknown}" | tr -c 'A-Za-z0-9._-' '_')"
  printf '%s/%s.jsonl' "$SPOOL_DIR" "${s:-unknown}"
}

# Is a reporting category enabled? (comma-membership test)
cat_enabled() { case ",${ENABLED_CATEGORIES}," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# Mint an M2M bearer for the telemetry SP: read the UC secret as the developer
# (who holds READ_SECRET), then client-credentials against the workspace OIDC
# token endpoint. Cache atomically with a refresh-at hint. Same flow the OTEL
# headers helper uses. Returns non-zero on any failure (caller degrades to no-op).
_mint_token() {
  mkdir -p "$CACHE_DIR" 2>/dev/null || return 1
  # Restrict the cache dir before a token is ever written into it.
  chmod 700 "$CACHE_DIR" 2>/dev/null || true
  local secret_json token
  secret_json="$("$DATABRICKS_BIN" api get \
    "/api/2.1/unity-catalog/secrets/${SECRET_FULL_NAME}?include_value=true" \
    --profile "$PROFILE" 2>/dev/null)" || return 1
  token="$(printf '%s' "$secret_json" | ZB_HOST="$TOKEN_HOST" ZB_ENDPOINT="$ZEROBUS_ENDPOINT" ZB_TABLE="$HOOK_EVENTS_TABLE" python3 -c '
import base64, json, os, sys, urllib.parse, urllib.request
obj = json.load(sys.stdin)
creds = json.loads(obj["effective_value"])
# Zerobus REST rejects a plain all-apis token ("invalid token audience"): the token
# must be minted for the Zerobus Direct Write API (resource = the workspace audience)
# and down-scoped to the target UC objects via authorization_details. The numeric
# workspace id is the first label of the Zerobus host.
wsid = urllib.parse.urlparse(os.environ["ZB_ENDPOINT"]).hostname.split(".")[0]
table = os.environ["ZB_TABLE"]
parts = table.split(".")
ad = [
    {"type": "unity_catalog_privileges", "privileges": ["USE CATALOG"], "object_type": "CATALOG", "object_full_path": parts[0]},
    {"type": "unity_catalog_privileges", "privileges": ["USE SCHEMA"], "object_type": "SCHEMA", "object_full_path": ".".join(parts[:2])},
    {"type": "unity_catalog_privileges", "privileges": ["SELECT", "MODIFY"], "object_type": "TABLE", "object_full_path": table},
]
data = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "scope": "all-apis",
    "resource": "api://databricks/workspaces/" + wsid + "/zerobusDirectWriteApi",
    "authorization_details": json.dumps(ad),
}).encode()
req = urllib.request.Request(os.environ["ZB_HOST"].rstrip("/") + "/oidc/v1/token", data=data)
basic = base64.b64encode((creds["client_id"] + ":" + creds["client_secret"]).encode()).decode()
req.add_header("Authorization", "Basic " + basic)
req.add_header("Content-Type", "application/x-www-form-urlencoded")
with urllib.request.urlopen(req, timeout=30) as r:
    sys.stdout.write(json.load(r)["access_token"])
' 2>/dev/null)" || return 1
  [ -n "$token" ] || return 1
  # Write the token 0600 from creation (umask 077 in a subshell, so there is no
  # world-readable window), then chmod 600 explicitly before the atomic rename
  # (mv preserves the temp file's mode). The cached bearer must not be readable
  # by other local users.
  ( umask 077; printf '%s' "$token" > "${TOKEN_FILE}.tmp"; ) 2>/dev/null \
    && chmod 600 "${TOKEN_FILE}.tmp" 2>/dev/null \
    && mv -f "${TOKEN_FILE}.tmp" "$TOKEN_FILE" 2>/dev/null || return 1
  echo "$(( $(date +%s) + TOKEN_TTL ))" > "$EXP_FILE" 2>/dev/null || true
  return 0
}

# Serve a bearer. Mint synchronously when there is no token yet, OR when the cached
# token is past its refresh hint. A stale-but-not-expired token is still fine
# (refresh_at is a proactive hint, not the real expiry), so a failed re-mint falls
# back to serving the cached token.
_get_token() {
  if [ ! -s "$TOKEN_FILE" ]; then
    _mint_token || return 1
  else
    local exp now
    exp="$(cat "$EXP_FILE" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    case "$exp" in ''|*[!0-9]*) exp=0 ;; esac
    if [ "$now" -ge "$exp" ]; then
      _mint_token >/dev/null 2>&1 || true
    fi
  fi
  cat "$TOKEN_FILE" 2>/dev/null
}

# Resolve the workspace identity (email) of the developer this session runs as —
# NOT the OS login ($USER, often unset in a hook's sandboxed process). Asks the
# Databricks CLI once (`current-user me`) and caches the answer; identity is stable,
# so only the first hook that needs it touches the network. HOOK_EVENTS_USER forces
# a value; on any failure we fall back to the OS user, then "unknown".
_ws_user() {
  if [ -n "${HOOK_EVENTS_USER:-}" ]; then printf '%s' "$HOOK_EVENTS_USER"; return 0; fi
  if [ -s "$WS_USER_FILE" ]; then cat "$WS_USER_FILE" 2>/dev/null; return 0; fi
  local email
  email="$("$DATABRICKS_BIN" current-user me --profile "$PROFILE" -o json 2>/dev/null \
    | jq -r '.userName // .emails[0].value // empty' 2>/dev/null)"
  if [ -n "$email" ]; then
    mkdir -p "$CACHE_DIR" 2>/dev/null || true
    printf '%s' "$email" > "${WS_USER_FILE}.tmp" 2>/dev/null \
      && mv -f "${WS_USER_FILE}.tmp" "$WS_USER_FILE" 2>/dev/null || true
    printf '%s' "$email"
    return 0
  fi
  printf '%s' "${USER:-unknown}"
}

# emit <category> <event_name> <plugin_name> <attributes_json>
# Appends ONE event (a single-line JSON object) to the session spool. Instant and
# local — no token, no network — so the hot path (PreToolUse/PostToolUse) never
# blocks. flush_spool delivers the batch later, at a turn/session boundary.
emit() {
  cat_enabled "$1" || return 0
  local category="$1" event_name="$2" plugin="$3" attrs="$4"
  local session user machine ts event_id record spool
  session="$(_session_id)"
  user="$(_ws_user)"
  machine="$(hostname 2>/dev/null || echo unknown)"
  ts="$(( $(date +%s) * 1000000 ))"
  event_id="$(uuidgen 2>/dev/null || printf '%s-%s-%s' "$session" "$ts" "${RANDOM:-0}")"
  record="$(jq -cn \
    --arg id "$event_id" --argjson ts "$ts" --arg cat "$category" --arg ev "$event_name" \
    --arg s "$session" --arg u "$user" --arg m "$machine" --arg a "$AGENT" \
    --arg p "$plugin" --arg attrs "$attrs" \
    '{event_id:$id, event_time:$ts, category:$cat, event_name:$ev, session_id:$s, user:$u, machine:$m, agent:$a, plugin_name:$p, attributes:$attrs}' 2>/dev/null)" || return 0
  [ -n "$record" ] || return 0
  mkdir -p "$SPOOL_DIR" 2>/dev/null || return 0
  spool="$(_spool_file)"
  printf '%s\n' "$record" >> "$spool" 2>/dev/null || true
}

# flush_spool: deliver the session's spooled events as one batched insert. Runs at
# turn/session boundaries (Stop, SubagentStop, SessionEnd) and on SessionStart to
# sweep anything a prior run left behind. The spool is persistent: if delivery
# fails it stays on disk and the next flush retries (at-least-once; dedupe on event_id).
flush_spool() {
  local spool sending token array
  spool="$(_spool_file)"
  [ -s "$spool" ] || return 0
  # Rotate first so events appended during the POST land in a fresh spool (no loss).
  sending="${spool}.sending.$$"
  mv -f "$spool" "$sending" 2>/dev/null || return 0
  token="$(_get_token)" || { cat "$sending" >> "$spool" 2>/dev/null; rm -f "$sending"; return 0; }
  [ -n "$token" ] || { cat "$sending" >> "$spool" 2>/dev/null; rm -f "$sending"; return 0; }
  array="$(jq -cs '.' "$sending" 2>/dev/null)" || { cat "$sending" >> "$spool" 2>/dev/null; rm -f "$sending"; return 0; }
  if curl -fsS --connect-timeout 3 -m 15 -X POST \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      -d "$array" \
      "${ZEROBUS_ENDPOINT%/}/zerobus/v1/tables/${HOOK_EVENTS_TABLE}/insert" \
      >/dev/null 2>&1; then
    rm -f "$sending"
  else
    cat "$sending" >> "$spool" 2>/dev/null || true
    rm -f "$sending"
  fi
}

# split "plugin:name" -> sets SPLIT_PLUGIN / SPLIT_NAME; bare "name" -> "" / name.
_split_prefixed() {
  if printf '%s' "$1" | grep -qE '^[A-Za-z0-9_-]+:[A-Za-z0-9_:-]+$'; then
    SPLIT_PLUGIN="${1%%:*}"; SPLIT_NAME="${1#*:}"
  else
    SPLIT_PLUGIN=""; SPLIT_NAME="$1"
  fi
}

# Extract the shell command from a tool_input payload. Codex's shell tool is "shell"
# (with exec_command/local_shell/unified_exec variants; see SHELL_TOOL_MATCHER); the
# exact inner field is not pinned in the public schema, so we're type-aware: an
# object yields .command/.cmd (else its JSON), a bare string yields itself, anything
# else is stringified — the risk/secret scan only needs to see the text, so a superset
# is safe (report-only). NOTE: indexing must be guarded by `type=="object"` first —
# `.tool_input.command` on a string ABORTS jq (which `//` does not catch), so a bare
# string tool_input would otherwise silently blind the scan.
_tool_command() {
  printf '%s' "$INPUT" | jq -r \
    '(.tool_input // "") | if type=="object" then (.command // .cmd // tostring) elif type=="string" then . else tostring end' \
    2>/dev/null
}

# --- PreToolUse (Bash): governance signals (report-only, never blocks) ------
handle_pretool() {
  cat_enabled governance || return 0
  local tool command
  # The hooks.json matcher (SHELL_TOOL_MATCHER) is the sole tool gate — we don't
  # re-hardcode the tool name here (it's unpinned), just record it in the event.
  tool="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)"
  command="$(_tool_command)"
  [ -n "$command" ] || return 0
  local default_risk='rm[[:space:]]+-rf[[:space:]]+/|mkfs|dd[[:space:]]+if=|curl[^|]*\|[[:space:]]*(ba)?sh|chmod[[:space:]]+-R[[:space:]]+777'
  local risk="${HOOK_EVENTS_RISK_PATTERNS:-$default_risk}"
  if printf '%s' "$command" | grep -qE "$risk"; then
    emit governance command_flagged "" "$(jq -cn --arg t "$tool" '{tool_name: $t, matched: "risk_pattern"}' 2>/dev/null || echo '{}')"
  fi
  # Cheap secret prefilter (a superset starter set; customers extend it).
  if printf '%s' "$command" | grep -qiE 'ds?(api|kea|ose)|AKIA|gh[pous]_|github_pat_|sk-(proj|ant)?-|xox[bpas]-|glpat-|AIza|-----BEGIN'; then
    emit governance secret_detected "" "$(jq -cn --arg t "$tool" '{tool_name: $t, location: "bash_command"}' 2>/dev/null || echo '{}')"
  fi
}

# --- PostToolUse (Bash): adoption (PR pushes) -------------------------------
handle_posttool() {
  cat_enabled adoption || return 0
  local command resp pr
  command="$(_tool_command)"
  printf '%s' "$command" | grep -qiE '(git([[:space:]]+|-)stack[[:space:]]+push|git[[:space:]]+(pp|push)|gh[[:space:]]+pr[[:space:]]+create|createPullRequest)' || return 0
  resp="$(printf '%s' "$INPUT" | jq -r '(.tool_response | if type == "string" then . else tostring end) // ""' 2>/dev/null)"
  pr="$(printf '%s' "$resp" | grep -oiE '/pull/[0-9]+|PR #[0-9]+' | grep -oE '[0-9]+' | head -n1)"
  [ -n "$pr" ] || return 0
  emit adoption pr_pushed "" "$(jq -cn --arg pr "$pr" '{pr_number: $pr}' 2>/dev/null || echo '{}')"
}

# --- SubagentStart: usage (a subagent of type X was spawned) ----------------
# Codex fires SubagentStart in the child's own context; agent_type is a top-level
# field (unlike Claude Code, which reads subagent_type from the Task tool_input).
handle_subagent() {
  cat_enabled usage || return 0
  local at plugin sub
  at="$(printf '%s' "$INPUT" | jq -r '.agent_type // ""' 2>/dev/null)"
  [ -n "$at" ] || return 0
  _split_prefixed "$at"; plugin="$SPLIT_PLUGIN"; sub="$SPLIT_NAME"
  emit usage subagent_used "$plugin" "$(jq -cn --arg s "$sub" --arg sf "$at" '{subagent_type: $s, subagent_full_type: $sf}' 2>/dev/null || echo '{}')"
}

case "$SUB" in
  pretool)  handle_pretool ;;
  posttool) handle_posttool ;;
  subagent) handle_subagent ;;
  flush)    flush_spool ;;
esac
exit 0
"""


def _toml_basic(s: str) -> str:
    """Serialize a string as a TOML basic (double-quoted) string."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(items: list[str]) -> str:
    return "[" + ", ".join(_toml_basic(i) for i in items) + "]"


def _auth_command_script(host: str, profile: str, databricks_bin: str) -> str:
    """Inline `bash -c` script for Codex's auth command: print ONLY a bare OAuth token.

    Honors a $DATABRICKS_BEARER override, else mints a fresh U2M token via the
    databricks CLI and extracts access_token with python3 (always present; no jq
    dependency). --force-refresh avoids hourly 403s from cached near-expiry tokens.
    Kept inline so the whole setup is a single config.toml — no helper script to
    deploy alongside it.
    """
    extract = (
        "python3 -c 'import json,sys;"
        'sys.stdout.write(json.load(sys.stdin)["access_token"])\''
    )
    mint = (
        f"{databricks_bin} auth token --host {host} --profile {profile} "
        f"--force-refresh --output json | {extract}"
    )
    return f'if [ -n "${{DATABRICKS_BEARER:-}}" ]; then printf %s "$DATABRICKS_BEARER"; else {mint}; fi'


def _zerobus_cloud_suffix(host: str) -> str:
    """Zerobus host suffix for the workspace's cloud, inferred from the workspace host."""
    h = host.lower()
    if h.endswith(".azuredatabricks.net"):
        return ".azuredatabricks.net"
    if h.endswith(".gcp.databricks.com"):
        return ".gcp.databricks.com"
    return ".cloud.databricks.com"  # AWS (default)


def _derive_zerobus_endpoint(host: str, profile: str, databricks_bin: str) -> str | None:
    """Derive https://<workspace-id>.zerobus.<region><cloud-suffix> from workspace metadata.

    Zerobus has no discovery endpoint, so build the host from three probes: the
    workspace (org) id from the `x-databricks-org-id` response header, the region
    from the UC metastore summary, and the cloud suffix from the workspace host.
    Best-effort: returns None on any failure so generation still succeeds (the hook
    ships dormant until ZEROBUS_ENDPOINT is known).
    """
    try:
        proc = subprocess.run(
            [databricks_bin, "auth", "token", "--host", host, "--profile", profile, "--output", "json"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        token = json.loads(proc.stdout)["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        me = urllib.request.Request(f"{host}/api/2.0/preview/scim/v2/Me", headers=auth)
        with urllib.request.urlopen(me, timeout=15) as r:
            org_id = r.headers.get("x-databricks-org-id")
        if not org_id:
            return None

        ms = urllib.request.Request(f"{host}/api/2.0/unity-catalog/metastore_summary", headers=auth)
        with urllib.request.urlopen(ms, timeout=15) as r:
            region = json.loads(r.read()).get("region")
        if not region:
            return None

        return f"https://{org_id}.zerobus.{region}{_zerobus_cloud_suffix(host)}"
    except Exception:
        return None


def _hook_dispatcher_script(endpoint: str, table: str, secret_full_name: str, host: str,
                            profile: str, databricks_bin: str, categories: list[str],
                            token_ttl: int) -> str:
    """Render emit_hook_events.sh with baked (env-overridable) defaults."""
    repl = {
        "__ENDPOINT__": endpoint.rstrip("/"),
        "__TABLE__": table,
        "__SECRET__": secret_full_name,
        "__PROFILE__": profile,
        "__DBX_BIN__": databricks_bin,
        "__HOST__": host,
        "__CATEGORIES__": ",".join(categories),
        "__TTL__": str(token_ttl),
    }
    script = _HOOK_DISPATCHER_TEMPLATE
    for sentinel, val in repl.items():
        script = script.replace(sentinel, val)
    return script


def _hooks_event_map(script_ref: str, categories: list[str]) -> dict[str, list]:
    """Map hook events -> matcher-groups of dispatcher-command handlers.

    Producers spool an event for their enabled category; flush drains the spool at
    turn/session boundaries (registered whenever anything is on). `script_ref` is the
    (already-quoted, if needed) command prefix to the emitter — e.g. an absolute
    "/etc/codex/emit_hook_events.sh" (managed) or "\"$CODEX_HOME/emit_hook_events.sh\""
    (user). Groups without a `matcher` fire on every occurrence of the event. Shared by
    the JSON (user hooks.json) and TOML (managed [hooks]) emitters so they never drift.
    """
    def cmd(sub: str, timeout: int) -> dict:
        return {"type": "command", "command": f"{script_ref} {sub}", "timeout": timeout}

    hooks: dict[str, list] = {}

    def add(event: str, entry: dict) -> None:
        hooks.setdefault(event, []).append(entry)

    # Producers (only for enabled categories).
    if "governance" in categories:
        add("PreToolUse", {"matcher": SHELL_TOOL_MATCHER, "hooks": [cmd("pretool", 10)]})
    if "adoption" in categories:
        add("PostToolUse", {"matcher": SHELL_TOOL_MATCHER, "hooks": [cmd("posttool", 10)]})
    if "usage" in categories:
        add("SubagentStart", {"hooks": [cmd("subagent", 10)]})

    # Drain at boundaries (flush handles all categories). SessionStart also sweeps
    # anything a prior run left spooled.
    for boundary in ("SessionStart", "Stop", "SubagentStop", "SessionEnd"):
        add(boundary, {"hooks": [cmd("flush", 15)]})

    return hooks


def _hooks_json_block(script_ref: str, categories: list[str]) -> dict:
    """The Codex hooks.json object (user mode: standalone ~/.codex/hooks.json)."""
    return {
        "description": "unity-gateway-setup Codex reporting hooks (report-only, Zerobus REST).",
        "hooks": _hooks_event_map(script_ref, categories),
    }


def _toml_inline(obj) -> str:
    """Serialize a scalar/list/dict as a TOML inline value (inline tables + arrays)."""
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, list):
        return "[" + ", ".join(_toml_inline(v) for v in obj) + "]"
    if isinstance(obj, dict):
        return "{ " + ", ".join(f"{k} = {_toml_inline(v)}" for k, v in obj.items()) + " }"
    return _toml_basic(str(obj))


def _hooks_toml_section(script_ref: str, categories: list[str]) -> str:
    """Render the `[hooks]` section for managed_config.toml (managed mode).

    config.toml/managed_config.toml take hooks inline under [hooks] as
    `<Event> = [ { matcher = "...", hooks = [ { type = "command", ... } ] } ]`.
    """
    lines = ["[hooks]"]
    for event, groups in _hooks_event_map(script_ref, categories).items():
        lines.append(f"{event} = {_toml_inline(groups)}")
    return "\n".join(lines)


def _requirements_toml(hooks_enabled: bool) -> str:
    """Render /etc/codex/requirements.toml — the fleet enforcement policy.

    Only binary-CONFIRMED keys are emitted live. `allow_managed_hooks_only` locks the
    telemetry hooks on (users can't add/replace/disable hooks). The model/provider
    allowlist (ModelsRequirementsToml) is emitted as a COMMENTED stub: the routing lock
    is already enforced by managed_config.toml's override of user config, and the exact
    `[models]` schema is not pinned in this Codex version — an empirically-verified fact
    that a wrong shape makes Codex fail to load config entirely, so it must be confirmed
    against the target version before being enabled.
    """
    lines = [
        "# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.",
        "#",
        "# Codex fleet enforcement policy, read from /etc/codex/requirements.toml.",
        "# Provider/model ROUTING is already locked by /etc/codex/managed_config.toml,",
        "# which overrides each user's ~/.codex/config.toml (verified on codex-cli 0.150.1).",
        "",
    ]
    if hooks_enabled:
        lines += [
            "# Lock the telemetry hooks ON: only managed (/etc/codex) hooks run; a user",
            "# cannot add, replace, or disable them via ~/.codex/hooks.json or config.toml.",
            "allow_managed_hooks_only = true",
            "",
        ]
    lines += [
        "# ---- OPTIONAL: deeper model/provider + guardrail locks --------------------",
        "# The keys below are the ones this Codex build exposes (from the requirements",
        "# schema), but their exact VALUE shapes are not pinned for this version. A wrong",
        "# shape makes Codex fail to load config entirely (verified), so confirm against",
        "# your target `codex` before uncommenting — validate with `codex --strict-config",
        "# doctor`. The model allowlist type is `ModelsRequirementsToml`.",
        "#",
        "# [models]            # ModelsRequirementsToml — restrict selectable models",
        "#   ...               # (shape unconfirmed for this version)",
        "#",
        "# allowed_approval_policies   = [\"on-request\", \"on-failure\"]",
        "# allowed_sandbox_modes       = [\"workspace-write\", \"read-only\"]",
        "# allowed_permission_profiles = [\"...\"]",
        "# allowed_web_search_modes    = [\"...\"]",
        "# default_permissions         = \"...\"",
        "",
    ]
    return "\n".join(lines)


# --- Cross-agent hardening knobs NOT wired here (custom CA + version floor) --
# Claude Code exposes --ssl-cert-file (SSL_CERT_FILE + NODE_EXTRA_CA_CERTS in its
# managed env) and --required-min-version (requiredMinimumVersion). Codex has no
# equivalent this generator can emit honestly, so neither flag is offered:
#   * Custom CA / TLS: Codex is a Rust CLI, so NODE_EXTRA_CA_CERTS (a Node-only var)
#     does not apply to it, and no CA-bundle key in config.toml / managed_config.toml
#     is confirmed for this Codex version. This file emits only binary-CONFIRMED keys
#     (see _requirements_toml), so no CA config key is invented — set the trust store
#     in the launch environment instead.
#   * Version floor: requirements.toml is the enforcement surface, but no
#     minimum-version key is confirmed in its schema for this version, and a wrong
#     requirements shape makes Codex fail to load config entirely (see
#     _requirements_toml). Not faked.
class CodexGenerator(AgentGenerator):
    name = "codex"
    help = "Generate a Codex CLI config.toml that routes through the Unity AI Gateway."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--api-type",
            default=DEFAULT_API_TYPE,
            help=(
                "supported_api_types value an endpoint must expose to be included. "
                f"Default '{DEFAULT_API_TYPE}' (the broad Responses surface served by the "
                "MLflow route — covers GPT, Gemini, Claude, and open models). Narrow to "
                f"'{NARROW_API_TYPE}' for OpenAI-native responses only."
            ),
        )
        parser.add_argument(
            "--skip-api-discovery",
            action="store_true",
            help=(
                "Skip live discovery of each model service's supported_api_types and fall "
                "back to the --fallback-schema heuristic (for offline/--tf-output-json use)."
            ),
        )
        parser.add_argument(
            "--fallback-schema",
            default="openai",
            help="Schema assumed responses-capable when discovery is skipped (default: openai).",
        )
        parser.add_argument(
            "--default-model",
            default=None,
            help=(
                "Model Codex starts on: an endpoint leaf name (e.g. 'gpt', 'gpt-5-6-sol') or "
                "a full three-level UC name. Default: the flagship 'gpt' alias if present, "
                "else the first discovered endpoint."
            ),
        )
        parser.add_argument(
            "--reasoning-effort",
            default="high",
            choices=REASONING_EFFORTS,
            help="model_reasoning_effort Codex starts with (default: high).",
        )
        parser.add_argument(
            "--provider-name",
            default="databricks",
            help="Key for the [model_providers.<name>] block and the model_provider value (default: databricks).",
        )
        parser.add_argument(
            "--gateway-path",
            default=DEFAULT_GATEWAY_PATH,
            help=(
                "Gateway route base appended to the host for base_url; Codex appends "
                f"/responses (default: {DEFAULT_GATEWAY_PATH}). Override to route elsewhere, "
                "e.g. --gateway-path /ai-gateway/codex/v1."
            ),
        )
        parser.add_argument(
            "--refresh-interval-ms",
            type=int,
            default=900000,
            help="auth.refresh_interval_ms: how often Codex re-runs the token command (default: 900000 = 15 min).",
        )
        parser.add_argument(
            "--auth-timeout-ms",
            type=int,
            default=5000,
            help="auth.timeout_ms: token-command timeout (default: 5000).",
        )
        parser.add_argument(
            "--databricks-bin",
            default="databricks",
            help="Path to the databricks CLI used in the auth command (default: databricks; use an absolute path for minimal-PATH contexts).",
        )
        # ---- deployment model ----
        parser.add_argument(
            "--user-config",
            action="store_true",
            help=(
                "Emit a per-user, non-enforced bundle (config.toml [+ hooks.json + "
                "emit_hook_events.sh] for $CODEX_HOME) instead of the default managed "
                "bundle. Default (managed) emits an /etc/codex enforcement bundle "
                "(managed_config.toml overriding user config + requirements.toml + the "
                "emitter) — the Codex analogue of Claude Code's root-owned managed settings."
            ),
        )
        # ---- hook telemetry (custom reporting events via Zerobus REST) ----
        parser.add_argument(
            "--hook-telemetry",
            choices=["auto", "on", "off"],
            default="auto",
            help=(
                "Also emit the hook telemetry — a managed [hooks] block in managed_config.toml "
                "(default), or a hooks.json with --user-config — plus emit_hook_events.sh, "
                "streaming reporting events (usage, governance, adoption) to Zerobus REST, "
                "reusing the same telemetry table/SP/secret as Claude Code. 'auto' (default) "
                "enables it iff the Terraform telemetry.hook_events table is present; 'on' "
                "requires it; 'off' skips. NOTE: Codex has no error/failure hook, so the "
                "'reliability' category (available for Claude Code) is not offered here."
            ),
        )
        parser.add_argument(
            "--hook-categories",
            default=",".join(HOOK_CATEGORIES),
            help=(
                "Comma-separated reporting categories to wire up (any of: "
                f"{', '.join(HOOK_CATEGORIES)}). Only the hook events for the selected "
                "categories are registered. Default: all three."
            ),
        )
        parser.add_argument(
            "--hook-token-ttl-seconds",
            type=int,
            default=600,
            help="Refresh-hint TTL for the cached Zerobus bearer, in seconds (default: 600).",
        )
        parser.add_argument(
            "--hook-script-path",
            default=None,
            help=(
                "Path the hooks command invokes the emitter from. Defaults to "
                f"'{MANAGED_HOOK_SCRIPT_PATH}' in managed mode (absolute, root-owned) or "
                f"'{DEFAULT_HOOK_SCRIPT_PATH}' with --user-config. Override to relocate."
            ),
        )
        parser.add_argument(
            "--zerobus-endpoint",
            default=None,
            help=(
                "Override the Zerobus REST base URL. Precedence: this flag > the Terraform "
                "telemetry.hook_events endpoint > auto-derivation from workspace metadata "
                "(skipped with --skip-api-discovery). Format: "
                "https://<workspace-id>.zerobus.<region>.cloud.databricks.com"
            ),
        )

    def _select(self, ctx: GatewayContext, args: argparse.Namespace) -> list[Endpoint]:
        """Endpoints Codex can use: those exposing the chosen Responses api type."""
        candidates = ctx.endpoints
        if not candidates:
            raise SystemExit("No endpoints found in the Terraform outputs.")

        if args.skip_api_discovery:
            eps = [e for e in candidates if e.schema == args.fallback_schema]
            print(f"[codex] discovery skipped; using schema '{args.fallback_schema}' "
                  f"({len(eps)} endpoints).", file=sys.stderr)
        else:
            print(f"[codex] discovering supported_api_types for {len(candidates)} endpoints...",
                  file=sys.stderr)
            api_types = discover_api_types([e.full_name for e in candidates], args.profile,
                                           databricks_bin=args.databricks_bin)
            eps = [e for e in candidates if args.api_type in api_types.get(e.full_name, [])]
            skipped = sorted({e.schema for e in candidates} - {e.schema for e in eps})
            print(f"[codex] {len(eps)}/{len(candidates)} endpoints expose {args.api_type}"
                  + (f"; schemas without it: {', '.join(skipped)}" if skipped else ""),
                  file=sys.stderr)

        if not eps:
            raise SystemExit(
                f"No endpoints expose '{args.api_type}' in this workspace, so Codex cannot "
                "route through this gateway. Try --api-type mlflow/v1/responses, or "
                "--skip-api-discovery with --fallback-schema."
            )
        return eps

    def _resolve_default(self, eps: list[Endpoint], requested: str | None) -> Endpoint:
        by_name = {e.name: e for e in eps}
        by_full = {e.full_name: e for e in eps}
        if requested:
            if requested in by_full:
                return by_full[requested]
            if requested in by_name:
                return by_name[requested]
            raise SystemExit(
                f"--default-model '{requested}' is not among the {len(eps)} responses-capable "
                f"endpoints. Available leaves: {', '.join(sorted(by_name))}."
            )
        for pref in DEFAULT_MODEL_PREFERENCES:
            if pref in by_name:
                return by_name[pref]
        return sorted(eps, key=lambda e: (e.schema, e.name))[0]

    def _hook_parts(self, ctx: GatewayContext, args: argparse.Namespace) -> tuple[str | None, list[str]]:
        """Return (emit_hook_events.sh content, enabled categories).

        Reuses the telemetry service principal + UC secret + hook_events table (the
        same objects the Claude Code hook uses) and streams events to Zerobus REST.
        Returns (None, []) when hook telemetry is off / not deployed.
        """
        if args.hook_telemetry == "off":
            return None, []
        tel = ctx.telemetry
        he = tel.hook_events if tel else None
        table = (he or {}).get("table") if isinstance(he, dict) else None
        if not table:
            if args.hook_telemetry == "on":
                raise SystemExit(
                    "--hook-telemetry on, but the Terraform 'telemetry.hook_events' output is "
                    "absent. Set telemetry_hook_events_enabled = true and apply, or use "
                    "--hook-telemetry off."
                )
            return None, []  # auto + not deployed

        # Resolve the Zerobus endpoint. Precedence: --zerobus-endpoint > the Terraform
        # telemetry.hook_events endpoint > auto-derivation from workspace metadata.
        # Auto-derivation makes live workspace calls, so it is skipped in offline mode.
        # The endpoint is NOT required: the hooks ship regardless and no-op until
        # ZEROBUS_ENDPOINT is known (baked here, or set at runtime).
        endpoint = args.zerobus_endpoint or (he.get("endpoint") if isinstance(he, dict) else "") or ""
        profile = args.__dict__.get("profile", "DEFAULT")
        if not endpoint and not args.skip_api_discovery:
            endpoint = _derive_zerobus_endpoint(ctx.host, profile, args.databricks_bin) or ""
            if endpoint:
                print(f"[codex] hook telemetry: derived Zerobus endpoint {endpoint} "
                      "from workspace metadata.", file=sys.stderr)
        if not endpoint:
            print("[codex] hook telemetry: wiring hooks with NO Zerobus endpoint "
                  "(auto-derivation unavailable) — they stay dormant until ZEROBUS_ENDPOINT "
                  "is set (telemetry_zerobus_endpoint, --zerobus-endpoint, or the env var).",
                  file=sys.stderr)

        categories = [c.strip() for c in args.hook_categories.split(",") if c.strip()]
        unknown = [c for c in categories if c not in HOOK_CATEGORIES]
        if unknown:
            raise SystemExit(
                f"--hook-categories has unknown value(s): {', '.join(unknown)}. "
                f"Valid: {', '.join(HOOK_CATEGORIES)} (Codex has no 'reliability' hook)."
            )
        if not categories:
            return None, []

        script = _hook_dispatcher_script(
            endpoint=endpoint,
            table=table,
            secret_full_name=tel.secret_full_name,
            host=ctx.host,
            profile=profile,
            databricks_bin=args.databricks_bin,
            categories=categories,
            token_ttl=args.hook_token_ttl_seconds,
        )
        print(f"[codex] hook telemetry: {len(categories)} categor"
              f"{'y' if len(categories) == 1 else 'ies'} -> {table}", file=sys.stderr)
        return script, categories

    def _routing_lines(self, ctx: GatewayContext, args: argparse.Namespace,
                       provider: str, base_url: str, default_ep: Endpoint) -> list[str]:
        """The provider/model/auth body shared by managed_config.toml and user config.toml."""
        auth_script = _auth_command_script(ctx.host, args.__dict__.get("profile", "DEFAULT"), args.databricks_bin)
        return [
            f"model_provider = {_toml_basic(provider)}",
            f"model = {_toml_basic(default_ep.full_name)}",
            f"model_reasoning_effort = {_toml_basic(args.reasoning_effort)}",
            "",
            f"[model_providers.{provider}]",
            f"name = {_toml_basic('Databricks Unity AI Gateway')}",
            f"base_url = {_toml_basic(base_url)}",
            'wire_api = "responses"',
            "supports_websockets = false",
            "",
            f"[model_providers.{provider}.http_headers]",
            f"User-Agent = {_toml_basic('unity-gateway-setup/codex')}",
            "",
            f"[model_providers.{provider}.auth]",
            '# Prints a bare, short-lived Databricks OAuth token. Codex re-runs this every',
            '# refresh_interval_ms. Honors $DATABRICKS_BEARER; else mints via the databricks CLI.',
            'command = "bash"',
            f"args = {_toml_array(['-c', auth_script])}",
            f"timeout_ms = {args.auth_timeout_ms}",
            f"refresh_interval_ms = {args.refresh_interval_ms}",
        ]

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        eps = self._select(ctx, args)
        default_ep = self._resolve_default(eps, args.default_model)
        provider = args.provider_name
        base_url = f"{ctx.host}{args.gateway_path}"
        managed = not args.user_config

        # Commented catalog of switchable models (aliases first, then version pins).
        catalog = sorted(eps, key=lambda e: (not e.is_alias, e.schema, e.name))
        catalog_lines = "\n".join(f"#   {e.full_name}" for e in catalog)
        routing = self._routing_lines(ctx, args, provider, base_url, default_ep)

        hook_script, hook_categories = self._hook_parts(ctx, args)
        script_path = args.hook_script_path or (
            MANAGED_HOOK_SCRIPT_PATH if managed else DEFAULT_HOOK_SCRIPT_PATH
        )
        # Record what was actually emitted so install_notes doesn't over-claim (hooks are
        # only written when telemetry.hook_events is deployed — see _hook_parts).
        self._managed = managed
        self._emitted_hooks = hook_script is not None
        # Managed hooks are invoked UNQUOTED in the TOML command, so any whitespace
        # (space, tab, newline) in the path would mis-split the command at runtime and
        # break the TOML string. Fail loudly rather than emit a broken enforced file.
        if managed and hook_script is not None and any(c.isspace() for c in script_path):
            raise SystemExit(
                f"--hook-script-path {script_path!r} contains whitespace, but the managed "
                "[hooks] command is unquoted. Use a space-free absolute path."
            )

        if managed:
            deploy = "managed_config.toml, requirements.toml" + (
                f", {HOOK_SCRIPT_FILENAME}" if hook_script else ""
            )
            header = [
                "# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.",
                "#",
                "# Codex MANAGED config — deploy to /etc/codex/managed_config.toml (root-owned).",
                "# It OVERRIDES each user's ~/.codex/config.toml (verified on codex-cli 0.150.1),",
                f"# enforcing gateway routing ({args.gateway_path} + /responses = mlflow/v1/responses)",
                "# and the default model/provider fleet-wide — the Codex analogue of Claude Code's",
                "# root-owned managed-settings.json. requirements.toml carries the enforcement policy.",
                "#",
                f"# Deploy this bundle to /etc/codex/ on each machine (via MDM/config-mgmt): {deploy}.",
                "# Each developer authenticates once: databricks auth login --profile <profile>.",
                "#",
                f"# Switchable gateway models exposing {args.api_type} (use `codex -m <full-name>`):",
                catalog_lines,
                "",
            ]
            body = header + routing
            if hook_script is not None:
                # Managed hooks: absolute root path — no spaces, so it needs no inner
                # quoting and no $CODEX_HOME expansion (resolves the shell-exec ambiguity).
                body += ["", _hooks_toml_section(script_path, hook_categories)]
            files = {
                f"codex/etc/{MANAGED_CONFIG_FILENAME}": "\n".join(body) + "\n",
                f"codex/etc/{REQUIREMENTS_FILENAME}": _requirements_toml(hook_script is not None),
            }
            if hook_script is not None:
                files[f"codex/etc/{HOOK_SCRIPT_FILENAME}"] = hook_script
            return files

        # --- user (non-managed) mode: per-user $CODEX_HOME bundle -----------------
        header = [
            "# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.",
            "#",
            "# Routes the Codex CLI through the Databricks Unity AI Gateway",
            f"# ({args.gateway_path} + /responses = mlflow/v1/responses, wire_api=\"responses\").",
            "#",
            f"# Deploy to $CODEX_HOME/config.toml (default {CODEX_HOME_DEFAULT}/config.toml), OR keep",
            f"# your existing config.toml and drop this in as $CODEX_HOME/{provider}.config.toml,",
            f"# then launch with:  codex -p {provider}",
            "#",
            "# Each developer authenticates once: databricks auth login --profile <profile>.",
            "#",
            f"# Switchable gateway models exposing {args.api_type} (use `codex -m <full-name>`):",
            catalog_lines,
            "",
        ]
        files = {"codex/config.toml": "\n".join(header + routing) + "\n"}
        if hook_script is not None:
            script_ref = f'"{script_path}"'  # user path holds $CODEX_HOME/$HOME — quote it
            files[f"codex/{HOOK_SCRIPT_FILENAME}"] = hook_script
            files[f"codex/{HOOKS_JSON_FILENAME}"] = (
                json.dumps(_hooks_json_block(script_ref, hook_categories), indent=2) + "\n"
            )
        return files

    def install_notes(self, args: argparse.Namespace) -> str:
        provider = args.provider_name
        # Reflect what generate() actually wrote (hooks ship only when telemetry.hook_events
        # is deployed), not just the flag — falls back to the flag if generate() didn't run.
        hooks_on = getattr(self, "_emitted_hooks", args.hook_telemetry != "off")
        if args.user_config:
            lines = [
                "USER-CONFIG mode (--user-config): per-user, non-enforced. Deploy per developer:",
                f"  Full config     : copy config.toml -> $CODEX_HOME/config.toml (default {CODEX_HOME_DEFAULT}/config.toml)",
                f"  Non-destructive : copy config.toml -> $CODEX_HOME/{provider}.config.toml, then `codex -p {provider}`",
                "    (layers the gateway provider on top of an existing (e.g. ChatGPT-app) config.toml)",
                "",
                "Each developer authenticates once:",
                "  databricks auth login --host <workspace-url> --profile <profile>",
                "",
                "Requires python3 + the databricks CLI on PATH. Verify with:  codex doctor",
            ]
            if hooks_on:
                lines += [
                    "",
                    "Hook telemetry (when telemetry.hook_events is deployed):",
                    f"  emit_hook_events.sh + hooks.json ship alongside config.toml. Deploy both into",
                    f"  $CODEX_HOME; chmod +x the script; hooks.json invokes it via",
                    f"  '{DEFAULT_HOOK_SCRIPT_PATH}' (needs shell execution of the command; override",
                    "  with an absolute --hook-script-path if your Codex execs argv without a shell).",
                    "  NOTE: user hooks require per-user trust (or --dangerously-bypass-hook-trust);",
                    "  the managed bundle (default, no --user-config) avoids that via allow_managed_hooks_only.",
                ]
            return "\n".join(lines)

        # Managed mode (default).
        deploy = f"managed_config.toml + requirements.toml" + (
            f" + emit_hook_events.sh" if hooks_on else ""
        )
        lines = [
            f"MANAGED mode (default): enforced, root-owned. The bundle was written to codex/etc/.",
            f"Deploy to /etc/codex/ on each machine (via MDM/config-mgmt): {deploy}",
            f"  - managed_config.toml OVERRIDES ~/.codex/config.toml -> locks gateway routing +",
            "    default model/provider fleet-wide (the Codex analogue of Claude Code managed settings).",
            "  - requirements.toml carries the enforcement policy" + (
                " (allow_managed_hooks_only = true)." if hooks_on else "."
            ),
            "  Root-owned (chown root:root, chmod 644 the .toml; 755 the .sh).",
            "",
            "Each developer still authenticates once:",
            "  databricks auth login --host <workspace-url> --profile <profile>",
            "",
            "Verify with:  codex doctor   (config parse + effective provider/model + reachability)",
        ]
        if hooks_on:
            lines += [
                "",
                "Hook telemetry (managed [hooks] in managed_config.toml, invoking",
                f"  {MANAGED_HOOK_SCRIPT_PATH} by absolute path):",
                "  Needs jq + curl + python3 + the databricks CLI on PATH, and READ_SECRET on the",
                "  telemetry UC secret. Reuses the SAME table/SP/secret as Claude Code; events land",
                "  in telemetry.hook_events via Zerobus REST. Report-only — never blocks a tool call.",
                "  allow_managed_hooks_only = true means users can't disable/replace these hooks.",
                "  Confirm Zerobus REST is available in the workspace region.",
                "  Codex has no error/failure hook, so the 'reliability' category is not emitted.",
                "",
                "requirements.toml ships a COMMENTED model/provider allowlist stub: the routing",
                "lock is already enforced by managed_config's override; the deeper [models] schema is",
                "unconfirmed for this Codex version (a wrong shape fails config load), so validate",
                "with `codex --strict-config doctor` before enabling it.",
            ]
        return "\n".join(lines)
