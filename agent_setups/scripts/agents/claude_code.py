"""Claude Code managed-settings.json generator for the Databricks Unity AI Gateway.

Turns the deployed anthropic model services (from Terraform outputs) into an
opinionated, MDM-deployable managed-settings.json that routes Claude Code
through `<host>/ai-gateway/anthropic` with U2M OAuth.

Conventions follow the internal "Onboarding Coding Agents - AI Gateway" playbook.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import urllib.request

from agents.base import AgentGenerator
from gateway import Endpoint, GatewayContext, discover_api_types

# Claude Code speaks the Anthropic Messages API; only model services that expose
# this route through the gateway are usable by it.
ANTHROPIC_API_TYPE = "anthropic/v1/messages"

# macOS / Linux+WSL / Windows managed-settings.json locations.
INSTALL_PATHS = {
    "macos": "/Library/Application Support/ClaudeCode/managed-settings.json",
    "linux": "/etc/claude-code/managed-settings.json",
    "windows": r"C:\Program Files\ClaudeCode\managed-settings.json",
}

# Preferred endpoint (by leaf name) for each Claude Code tier. Ordered fallbacks.
# haiku pins the *versioned* endpoint: Claude Code hardcodes claude-haiku-4-5
# patterns (reasoning effort etc.), so the tier must resolve to that model.
TIER_PREFERENCES = {
    "opus": ["claude-opus", "claude-opus-5"],
    "sonnet": ["claude-sonnet", "claude-sonnet-5"],
    "haiku": ["claude-haiku-4-5", "claude-haiku"],
    "fable": ["claude-fable", "claude-fable-5"],
}

# Declared capabilities per tier (custom gateway model IDs skip auto-detection,
# so reasoning effort / thinking must be declared explicitly).
TIER_CAPABILITIES = {
    "opus": "effort,xhigh_effort,max_effort,thinking,adaptive_thinking,interleaved_thinking",
    "sonnet": "effort,thinking,adaptive_thinking,interleaved_thinking",
    "haiku": "effort,thinking",
    "fable": "effort,thinking,adaptive_thinking",
}

TIER_DISPLAY = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable"}

# Model families that default to 1M context (the `[1m]` suffix). Claude Code
# strips the suffix before sending to the gateway; it only requests the larger
# window. Haiku/Fable are left at their native window.
LARGE_CONTEXT_FAMILIES = ("opus", "sonnet")

# --- OpenTelemetry ---------------------------------------------------------
# Databricks OTLP ingest route on the workspace; the exporter posts protobuf to
# the per-signal path below (matching the reference setup and OTLP conventions).
OTEL_INGEST_PATH = "/api/2.0/otel"
OTEL_SIGNAL_PATHS = {"metrics": "v1/metrics", "logs": "v1/logs", "traces": "v1/traces"}
OTEL_EXPORTER_ENV = {
    "metrics": "OTEL_METRICS_EXPORTER",
    "logs": "OTEL_LOGS_EXPORTER",
    "traces": "OTEL_TRACES_EXPORTER",
}
OTEL_ENDPOINT_ENV = {
    "metrics": "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "logs": "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "traces": "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
}
# Per-signal STATIC headers carry the UC target table (X-Databricks-UC-Table-Name).
# The sensitive Authorization header comes from otelHeadersHelper and is *merged*
# with these by Claude Code, so the table routing can differ per signal even
# though the helper returns a single header set.
OTEL_HEADERS_ENV = {
    "metrics": "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "logs": "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    "traces": "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
}
# Content-logging vars: they capture prompts, tool I/O, and raw API bodies, so
# they are OFF by default and gated behind --otel-log-content. Note this does NOT
# include CLAUDE_CODE_ENHANCED_TELEMETRY_BETA — that flag gates trace/span export
# (a prerequisite for content logging too) and is enabled independently whenever
# traces are exported; see _apply_telemetry.
OTEL_CONTENT_ENV = (
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_TOOL_DETAILS",
    "OTEL_LOG_TOOL_CONTENT",
    "OTEL_LOG_RAW_API_BODIES",
)
OTEL_HELPER_FILENAME = "otel-headers-helper.sh"

# --- Per-platform output ----------------------------------------------------
# One self-contained bundle per OS. The managed-settings.json is identical across
# platforms EXCEPT the on-disk paths it references (otelHeadersHelper + the hooks
# command) — which point at each OS's ClaudeCode install directory (the same dir
# managed-settings.json itself is deployed to). The helper scripts are byte-for-
# byte identical everywhere; only these referenced paths differ.
PLATFORMS = ("macos", "linux", "windows")
PLATFORM_INSTALL_DIRS = {
    "macos": "/Library/Application Support/ClaudeCode",
    "linux": "/etc/claude-code",
    "windows": r"C:\Program Files\ClaudeCode",
}

# --- Per-user (local, non-managed) output -----------------------------------
# Claude Code reads a per-user settings.json from ~/.claude/settings.json. It
# accepts the same schema as managed-settings.json, EXCEPT requiredMinimumVersion
# (managed-only). --user-config emits one bundle to claude-code/user/ — a
# settings.json plus the (identical) helper scripts — for a developer who wants a
# local gateway route without root or MDM. The helper paths inside settings.json
# are baked as ABSOLUTE ~/.claude paths: otelHeadersHelper's shell-expansion is not
# documented, and a local install generates and installs on the same machine, so
# the expanded home is exactly right (and unambiguous).
USER_CONFIG_DIR = "~/.claude"


def _platform_path(install_dir: str, filename: str, platform: str) -> str:
    """Join an install dir + filename with the platform's path separator."""
    sep = "\\" if platform == "windows" else "/"
    return f"{install_dir}{sep}{filename}"

# --- Hook telemetry (custom reporting events via Zerobus REST) --------------
# Ports the reporting ideas from the internal llm-cli hooks (slash-command /
# skill / subagent usage, plugin inventory, StopFailure stalls, secret/command
# guardrail hits, workflow adoption) to a customer's Unity Catalog. Native OTEL
# does NOT emit these; the internal metric-proxy that collected them isn't
# available to customers. Each hook fires a backgrounded curl of a one-record
# JSON array to the Zerobus REST insert endpoint, authenticating as the same
# telemetry service principal (bearer minted from the UC secret, cached).
HOOK_CATEGORIES = ("usage", "reliability", "governance", "adoption")
HOOK_SCRIPT_FILENAME = "emit_hook_events.sh"

# Dispatcher script. One file, one subcommand per hook event. Sentinels
# (__NAME__) are replaced with baked defaults at generation time; each is
# env-overridable so one script serves a whole fleet. Report-only: it never
# blocks a tool call and always exits 0 (telemetry must not disrupt the dev).
_HOOK_DISPATCHER_TEMPLATE = r"""#!/usr/bin/env bash
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
#
# Claude Code hook -> Zerobus REST reporting event emitter.
#   usage: emit_hook_events.sh <inventory|prompt|pretool|posttool|stopfail>
#
# Reads the hook payload on stdin, builds a single-record JSON array, and POSTs
# it (backgrounded, best-effort) to the Zerobus REST insert endpoint as the
# telemetry service principal. Report-only and non-blocking: any failure is
# swallowed and the script always exits 0.
set -u

# --- baked config (override via environment for fleet reuse) ---------------
ZEROBUS_ENDPOINT="${ZEROBUS_ENDPOINT:-__ENDPOINT__}"
HOOK_EVENTS_TABLE="${HOOK_EVENTS_TABLE:-__TABLE__}"
SECRET_FULL_NAME="${ZEROBUS_UC_SECRET:-__SECRET__}"
PROFILE="${DATABRICKS_PROFILE:-__PROFILE__}"
DATABRICKS_BIN="${DATABRICKS_BIN:-__DBX_BIN__}"
TOKEN_HOST="${DATABRICKS_HOST:-__HOST__}"
ENABLED_CATEGORIES="${HOOK_EVENTS_CATEGORIES:-__CATEGORIES__}"
DOC_PATTERNS="${HOOK_EVENTS_DOC_PATTERNS:-__DOCPATTERNS__}"
LOG_PATHS="${HOOK_EVENTS_LOG_PATHS:-__LOGPATHS__}"
TOKEN_TTL="${HOOK_EVENTS_TOKEN_TTL:-__TTL__}"
AGENT="claude-code"

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
  printf '%s' "$token" > "${TOKEN_FILE}.tmp" 2>/dev/null && mv -f "${TOKEN_FILE}.tmp" "$TOKEN_FILE" 2>/dev/null || return 1
  echo "$(( $(date +%s) + TOKEN_TTL ))" > "$EXP_FILE" 2>/dev/null || true
  return 0
}

# Serve a bearer. Mint synchronously when there is no token yet, OR when the cached
# token is past its refresh hint. We do NOT refresh in the background: Claude Code
# waits for the hook to exit and then tears the hook process (and any children/
# sandbox) down, so a backgrounded mint would be killed mid-flight and the cache
# would never reseed — over ~1h the token would truly expire and inserts would 401.
# A stale-but-not-expired token is still fine (refresh_at is a proactive hint, not
# the real expiry), so a failed re-mint falls back to serving the cached token.
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
# e.g. tanner.wendland@databricks.com — NOT the OS login ($USER, which is often
# unset in a hook's sandboxed process, hence the old "unknown"). Asks the Databricks
# CLI once (`current-user me`, as the developer's own profile) and caches the answer;
# identity is stable, so only the first hook that needs it touches the network and
# every later emit reads the cache. HOOK_EVENTS_USER forces a value; on any failure we
# fall back to the OS user, then "unknown", so telemetry still ships (report-only).
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
# local — no token, no network — so the hot path (PostToolUse) never blocks and
# there is nothing a sandbox teardown can kill. flush_spool delivers the batch
# later, at a turn/session boundary. attributes_json must be a JSON object string.
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
# turn/session boundaries (Stop, StopFailure, SubagentStop, SessionEnd) and on
# SessionStart to sweep anything a prior run left behind. Synchronous — but off the
# per-tool-call hot path and batched — so it can't be torn down mid-flight. The
# spool is persistent: if delivery fails (or is interrupted), the events stay on
# disk and the next flush retries them (at-least-once; dedupe on event_id).
flush_spool() {
  local spool sending token array
  spool="$(_spool_file)"
  [ -s "$spool" ] || return 0
  # Rotate first so events appended during the POST land in a fresh spool (no loss).
  sending="${spool}.sending.$$"
  mv -f "$spool" "$sending" 2>/dev/null || return 0
  token="$(_get_token)" || { cat "$sending" >> "$spool" 2>/dev/null; rm -f "$sending"; return 0; }
  [ -n "$token" ] || { cat "$sending" >> "$spool" 2>/dev/null; rm -f "$sending"; return 0; }
  # Slurp the newline-delimited event objects into a single JSON array.
  array="$(jq -cs '.' "$sending" 2>/dev/null)" || { cat "$sending" >> "$spool" 2>/dev/null; rm -f "$sending"; return 0; }
  if curl -fsS --connect-timeout 3 -m 15 -X POST \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      -d "$array" \
      "${ZEROBUS_ENDPOINT%/}/zerobus/v1/tables/${HOOK_EVENTS_TABLE}/insert" \
      >/dev/null 2>&1; then
    rm -f "$sending"
  else
    # Delivery failed — requeue for the next flush (order isn't significant; the
    # consumer dedupes/sorts on event_id + event_time).
    cat "$sending" >> "$spool" 2>/dev/null || true
    rm -f "$sending"
  fi
}

# split "plugin:name" -> sets SPLIT_PLUGIN / SPLIT_NAME; bare "name" -> "" / name.
# (Sets globals rather than printing tab-separated: `read` with a tab IFS trims a
# leading tab, which would misassign the bare/no-plugin case.)
_split_prefixed() {
  if printf '%s' "$1" | grep -qE '^[A-Za-z0-9_-]+:[A-Za-z0-9_:-]+$'; then
    SPLIT_PLUGIN="${1%%:*}"; SPLIT_NAME="${1#*:}"
  else
    SPLIT_PLUGIN=""; SPLIT_NAME="$1"
  fi
}

# --- SessionStart: snapshot installed plugin -> version (usage join key) ----
handle_inventory() {
  cat_enabled usage || return 0
  local f="${HOME}/.claude/plugins/installed_plugins.json"
  local plugins='[]' count=0 attrs
  if [ -f "$f" ]; then
    plugins="$(jq -c '[(.plugins // {}) | to_entries[] | (.key | split("@")) as $p | {p: $p[0], v: (.value[0].version // "unknown")}]' "$f" 2>/dev/null || echo '[]')"
    count="$(printf '%s' "$plugins" | jq 'length' 2>/dev/null || echo 0)"
  fi
  attrs="$(jq -cn --argjson pl "$plugins" --argjson c "$count" '{plugin_count: $c, plugins: $pl}' 2>/dev/null || echo '{}')"
  emit usage plugin_inventory "" "$attrs"
}

# --- UserPromptSubmit: slash-command usage ----------------------------------
handle_prompt() {
  cat_enabled usage || return 0
  local prompt first body plugin cmd attrs
  prompt="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null)"
  first="${prompt%% *}"
  case "$first" in /*) ;; *) return 0 ;; esac
  body="${first#/}"
  _split_prefixed "$body"; plugin="$SPLIT_PLUGIN"; cmd="$SPLIT_NAME"
  [ -n "$cmd" ] || return 0
  attrs="$(jq -cn --arg c "$cmd" --arg cf "$body" '{command_name: $c, command_full_name: $cf}' 2>/dev/null || echo '{}')"
  emit usage slash_command "$plugin" "$attrs"
}

# --- PreToolUse (Bash): governance signals (report-only, never blocks) ------
handle_pretool() {
  cat_enabled governance || return 0
  local tool command
  tool="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)"
  [ "$tool" = "Bash" ] || return 0
  command="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)"
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

# --- PostToolUse (Skill|Task|Read|Bash): usage + adoption -------------------
handle_posttool() {
  local tool
  tool="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null)"
  case "$tool" in
    Skill)
      cat_enabled usage || return 0
      local sf plugin skill
      sf="$(printf '%s' "$INPUT" | jq -r '.tool_input.skill // ""' 2>/dev/null)"
      [ -n "$sf" ] || return 0
      _split_prefixed "$sf"; plugin="$SPLIT_PLUGIN"; skill="$SPLIT_NAME"
      emit usage skill_used "$plugin" "$(jq -cn --arg s "$skill" --arg sf "$sf" '{skill_name: $s, skill_full_name: $sf}' 2>/dev/null || echo '{}')"
      ;;
    Task)
      cat_enabled usage || return 0
      local st plugin sub
      st="$(printf '%s' "$INPUT" | jq -r '.tool_input.subagent_type // ""' 2>/dev/null)"
      [ -n "$st" ] || return 0
      _split_prefixed "$st"; plugin="$SPLIT_PLUGIN"; sub="$SPLIT_NAME"
      emit usage subagent_used "$plugin" "$(jq -cn --arg s "$sub" --arg sf "$st" '{subagent_type: $s, subagent_full_type: $sf}' 2>/dev/null || echo '{}')"
      ;;
    Read)
      cat_enabled adoption || return 0
      local fp base pathval
      fp="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)"
      [ -n "$fp" ] || return 0
      base="$(basename "$fp")"
      printf '%s' "$base" | grep -qiE "$DOC_PATTERNS" || return 0
      if [ "$LOG_PATHS" = "1" ]; then pathval="$fp"; else pathval="$base"; fi
      emit adoption doc_read "" "$(jq -cn --arg p "$pathval" '{doc: $p}' 2>/dev/null || echo '{}')"
      ;;
    Bash)
      cat_enabled adoption || return 0
      local command resp pr
      command="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)"
      printf '%s' "$command" | grep -qiE '(git([[:space:]]+|-)stack[[:space:]]+push|git[[:space:]]+(pp|push)|gh[[:space:]]+pr[[:space:]]+create|createPullRequest)' || return 0
      resp="$(printf '%s' "$INPUT" | jq -r '(.tool_response | if type == "string" then . else tostring end) // ""' 2>/dev/null)"
      pr="$(printf '%s' "$resp" | grep -oiE '/pull/[0-9]+|PR #[0-9]+' | grep -oE '[0-9]+' | head -n1)"
      [ -n "$pr" ] || return 0
      emit adoption pr_pushed "" "$(jq -cn --arg pr "$pr" '{pr_number: $pr}' 2>/dev/null || echo '{}')"
      ;;
  esac
}

# --- StopFailure: turn-ending API errors, incl. the mid-stream stalls -------
handle_stopfail() {
  cat_enabled reliability || return 0
  case "$INPUT" in *"API Error"*) ;; *) return 0 ;; esac
  local msg model qs at origin variant
  msg="$(printf '%s' "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null)"
  case "$msg" in "API Error"*) ;; *) return 0 ;; esac
  model="$(printf '%s' "$INPUT" | jq -r '.model // ""' 2>/dev/null)"
  qs="$(printf '%s' "$INPUT" | jq -r '.query_source // ""' 2>/dev/null)"
  at="$(printf '%s' "$INPUT" | jq -r '.agent_type // ""' 2>/dev/null)"
  case "$msg" in
    *"stalled mid-stream"*)             variant="stalled_mid_stream" ;;
    *"Connection closed mid-response"*) variant="connection_closed_mid_response" ;;
    *"Server error mid-response"*)      variant="server_error_mid_response" ;;
    *)                                  variant="other" ;;
  esac
  if [ -n "$at" ]; then origin="subagent"; else origin="main"; at="main"; fi
  emit reliability stop_failure "" "$(jq -cn --arg v "$variant" --arg e "$msg" --arg m "$model" --arg q "$qs" --arg o "$origin" --arg a "$at" \
    '{variant: $v, error_text: $e, model: $m, query_source: $q, origin: $o, agent_type: $a}' 2>/dev/null || echo '{}')"
}

case "$SUB" in
  inventory) handle_inventory ;;
  prompt)    handle_prompt ;;
  pretool)   handle_pretool ;;
  posttool)  handle_posttool ;;
  stopfail)  handle_stopfail ;;
  flush)     flush_spool ;;
esac
exit 0
"""


def _hook_dispatcher_script(endpoint: str, table: str, secret_full_name: str, host: str,
                            profile: str, databricks_bin: str, categories: list[str],
                            doc_patterns: str, log_paths: bool, token_ttl: int) -> str:
    """Render emit_hook_events.sh with baked (env-overridable) defaults."""
    repl = {
        "__ENDPOINT__": endpoint.rstrip("/"),
        "__TABLE__": table,
        "__SECRET__": secret_full_name,
        "__PROFILE__": profile,
        "__DBX_BIN__": databricks_bin,
        "__HOST__": host,
        "__CATEGORIES__": ",".join(categories),
        "__DOCPATTERNS__": doc_patterns,
        "__LOGPATHS__": "1" if log_paths else "0",
        "__TTL__": str(token_ttl),
    }
    script = _HOOK_DISPATCHER_TEMPLATE
    for sentinel, val in repl.items():
        script = script.replace(sentinel, val)
    return script


def _hook_settings_block(script_path: str, categories: list[str]) -> dict:
    """Map hook events -> dispatcher subcommands, for managed-settings.json.

    Two roles per the spool/flush model:
      * producers spool an event (SessionStart/UserPromptSubmit/PostToolUse/
        PreToolUse/StopFailure) — only those for enabled categories are registered;
      * flush drains the spool at turn/session boundaries (SessionStart, Stop,
        SubagentStop, StopFailure, SessionEnd) — registered whenever anything is on.
    Commands run in array order, so where a boundary is also a producer (SessionStart
    inventory, StopFailure stopfail) the event is spooled before the flush.
    """
    q = f'"{script_path}"'

    def cmd(sub: str) -> dict:
        return {"type": "command", "command": f"{q} {sub}"}

    ordered: dict[str, list[str]] = {}
    matchers: dict[str, str] = {}

    def add(event: str, sub: str, matcher: str | None = None) -> None:
        ordered.setdefault(event, []).append(sub)
        if matcher:
            matchers[event] = matcher

    # Producers.
    if "usage" in categories:
        add("SessionStart", "inventory")
        add("UserPromptSubmit", "prompt")
    post_matchers: list[str] = []
    if "usage" in categories:
        post_matchers += ["Skill", "Task"]
    if "adoption" in categories:
        post_matchers += ["Read", "Bash"]
    if post_matchers:
        add("PostToolUse", "posttool", "|".join(post_matchers))
    if "governance" in categories:
        add("PreToolUse", "pretool", "Bash")
    if "reliability" in categories:
        add("StopFailure", "stopfail")

    # Drain at boundaries (flush handles all categories). SessionStart also sweeps
    # anything a prior run left spooled.
    for boundary in ("SessionStart", "Stop", "SubagentStop", "StopFailure", "SessionEnd"):
        add(boundary, "flush")

    block: dict[str, list] = {}
    for event, subs in ordered.items():
        entry: dict = {"hooks": [cmd(s) for s in subs]}
        if event in matchers:
            entry["matcher"] = matchers[event]
        block[event] = [entry]
    return block


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

    Zerobus has no discovery endpoint, so build the host from three probes:
      * workspace (org) id — the `x-databricks-org-id` response header (the value the
        `<workspace-id>.zerobus.…` convention wants; not in any response body);
      * region — the current UC metastore summary's `region` (works on serverless,
        where the classic list-zones call does not);
      * cloud suffix — from the workspace host.
    Best-effort: returns None on any failure (offline, no auth, non-UC workspace) so
    generation still succeeds — the hook just ships dormant.
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


def _otel_headers_helper_script(host: str, profile: str, secret_full_name: str, databricks_bin: str) -> str:
    """Bash script for `otelHeadersHelper`: emit {"Authorization": "Bearer <token>"}.

    Reads the OTEL exporter OAuth credentials from the Unity Catalog secret (as the
    current developer, who holds READ_SECRET), then mints a short-lived M2M token
    for the telemetry service principal via the workspace OAuth token endpoint. The
    content-type and per-signal X-Databricks-UC-Table-Name headers are supplied by
    the static OTEL env vars and merged in by Claude Code.

    Defaults are baked in at generation time; each is overridable by environment so
    the same script works across a fleet (OTEL_UC_SECRET / DATABRICKS_PROFILE /
    DATABRICKS_HOST / DATABRICKS_BIN).
    """
    # The embedded python uses only double quotes so it survives single-quoted `-c`.
    py = (
        "import base64, json, os, sys, urllib.parse, urllib.request\n"
        'obj = json.load(sys.stdin)\n'
        'creds = json.loads(obj["effective_value"])\n'
        'data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode()\n'
        'url = os.environ["OTEL_TOKEN_HOST"].rstrip("/") + "/oidc/v1/token"\n'
        'req = urllib.request.Request(url, data=data)\n'
        'basic = base64.b64encode((creds["client_id"] + ":" + creds["client_secret"]).encode()).decode()\n'
        'req.add_header("Authorization", "Basic " + basic)\n'
        'req.add_header("Content-Type", "application/x-www-form-urlencoded")\n'
        'with urllib.request.urlopen(req, timeout=30) as r:\n'
        '    token = json.load(r)["access_token"]\n'
        'sys.stdout.write(json.dumps({"Authorization": "Bearer " + token}))\n'
    )
    return f"""#!/usr/bin/env bash
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
# Claude Code otelHeadersHelper: prints a JSON object of HTTP headers for OTLP.
set -euo pipefail

# Baked defaults (override via environment for fleet reuse).
SECRET_FULL_NAME="${{OTEL_UC_SECRET:-{secret_full_name}}}"
PROFILE="${{DATABRICKS_PROFILE:-{profile}}}"
DATABRICKS_BIN="${{DATABRICKS_BIN:-{databricks_bin}}}"
export OTEL_TOKEN_HOST="${{DATABRICKS_HOST:-{host}}}"

# 1. Read the SP OAuth credentials from the UC secret (CLI handles the dev's auth).
# 2. Mint an M2M bearer token for the SP and emit it as the Authorization header.
"$DATABRICKS_BIN" api get \\
  "/api/2.1/unity-catalog/secrets/${{SECRET_FULL_NAME}}?include_value=true" \\
  --profile "$PROFILE" \\
  | python3 -c '{py}'
"""


def _context_suffix(endpoint_name: str, small_context: bool) -> str:
    """`[1m]` for large-context families, unless small context is requested."""
    if small_context:
        return ""
    return "[1m]" if any(fam in endpoint_name for fam in LARGE_CONTEXT_FAMILIES) else ""


def _api_key_helper(host: str, profile: str, databricks_bin: str) -> str:
    """U2M OAuth token helper: honors $DATABRICKS_BEARER, else mints a fresh token.

    --force-refresh avoids hourly 403s from cached near-expiry tokens. Output is
    ONLY the token (no trailing newline), as Claude Code v2.1.227+ requires.
    """
    extract = 'python3 -c \'import json,sys; sys.stdout.write(json.load(sys.stdin)["access_token"])\''
    mint = (
        f"{databricks_bin} auth token --host {host} --profile {profile} "
        f"--force-refresh --output json | {extract}"
    )
    return f'if [ -n "${{DATABRICKS_BEARER:-}}" ]; then printf %s "$DATABRICKS_BEARER"; else {mint}; fi'


class ClaudeCodeGenerator(AgentGenerator):
    name = "claude-code"
    help = "Generate managed-settings.json for MDM deployment of Claude Code."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--skip-api-discovery",
            action="store_true",
            help=(
                "Skip live discovery of each model service's supported_api_types. By "
                "default the generator queries the workspace and includes only endpoints "
                "that expose the Anthropic API; --skip-api-discovery falls back to the "
                "--fallback-schema heuristic (for offline/--tf-output-json use)."
            ),
        )
        parser.add_argument(
            "--fallback-schema",
            default="anthropic",
            help="Schema assumed Anthropic-capable when discovery is skipped (default: anthropic).",
        )
        parser.add_argument(
            "--default-tier",
            default="sonnet",
            choices=list(TIER_PREFERENCES),
            help="Tier Claude Code starts on (default: sonnet).",
        )
        parser.add_argument(
            "--lock-models",
            default="catalog",
            choices=["catalog", "aliases", "none"],
            help=(
                "Model governance: 'catalog' = enforce, allow every deployed endpoint "
                "(power users can switch versions); 'aliases' = enforce, allow only the "
                "versionless aliases; 'none' = no enforcement. Default: catalog."
            ),
        )
        parser.add_argument(
            "--small-context",
            action="store_true",
            help=(
                "Use each model's native context window. By default the opus and sonnet "
                "families get the [1m] (1M-token) suffix; this flag omits it."
            ),
        )
        parser.add_argument(
            "--allow-websearch",
            action="store_true",
            help="Keep the built-in WebSearch tool (denied by default; it can't reach api.anthropic.com through the gateway).",
        )
        parser.add_argument(
            "--declare-capabilities",
            action="store_true",
            help=(
                "Also emit per-tier display-name and SUPPORTED_CAPABILITIES env vars. "
                "Off by default: it's a drift-prone surface (model capabilities and "
                "Claude Code's capability enum both change on Anthropic's schedule, not "
                "ours). Enable only if reasoning-effort/thinking toggles don't light up "
                "on their own; gateway model discovery is the drift-free alternative."
            ),
        )
        parser.add_argument(
            "--api-key-ttl-ms",
            type=int,
            default=900000,
            help="apiKeyHelper cache TTL in ms (default: 900000 = 15 min).",
        )
        parser.add_argument(
            "--databricks-bin",
            default="databricks",
            help="Path to the databricks CLI used in apiKeyHelper (default: databricks; set an absolute path for launchd/MDM contexts with a minimal PATH).",
        )
        parser.add_argument(
            "--required-min-version",
            default=None,
            help="Optional requiredMinimumVersion to enforce a Claude Code floor.",
        )
        parser.add_argument(
            "--ssl-cert-file",
            default=None,
            help="Optional SSL_CERT_FILE / NODE_EXTRA_CA_CERTS path (per-machine cert bundle).",
        )
        # ---- model picker ----
        parser.add_argument(
            "--model-picker",
            action="store_true",
            help=(
                "Emit a modelPicker so the interactive /model picker lists every "
                "Anthropic-capable endpoint (aliases first, then version pins), not just "
                "the four tier slots. Off by default. Requires Claude Code v2.1.242+."
            ),
        )
        parser.add_argument(
            "--model-picker-append",
            action="store_true",
            help=(
                "With --model-picker, keep Claude Code's built-in tier rows and append "
                "these (replaceBuiltInOptions=false). Default replaces them so the picker "
                "shows exactly Default + your catalog."
            ),
        )
        # ---- telemetry (OpenTelemetry) ----
        parser.add_argument(
            "--telemetry",
            choices=["auto", "on", "off"],
            default="auto",
            help=(
                "Emit the OTEL telemetry env + otelHeadersHelper. 'auto' (default) enables it "
                "iff the Terraform 'telemetry' output is present; 'on' requires it; 'off' skips."
            ),
        )
        parser.add_argument(
            "--otel-log-content",
            action="store_true",
            help=(
                "Also log prompts, tool details/content, and raw API bodies "
                "(the OTEL_LOG_* vars). Privacy-sensitive; OFF by default so only "
                "metrics, non-content logs, and traces are exported. (Traces themselves "
                "need no flag — CLAUDE_CODE_ENHANCED_TELEMETRY_BETA is set automatically "
                "when a traces table exists.)"
            ),
        )
        parser.add_argument(
            "--otel-metric-interval-ms",
            type=int,
            default=60000,
            help="OTEL_METRIC_EXPORT_INTERVAL in ms (default: 60000 = 60s).",
        )
        parser.add_argument(
            "--otel-logs-interval-ms",
            type=int,
            default=5000,
            help="OTEL_LOGS_EXPORT_INTERVAL in ms (default: 5000 = 5s).",
        )
        parser.add_argument(
            "--otel-headers-helper-debounce-ms",
            type=int,
            default=900000,
            help="CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS: token refresh interval (default: 900000 = 15 min).",
        )
        # ---- deployment model ----
        parser.add_argument(
            "--user-config",
            action="store_true",
            help=(
                "Emit a per-user, non-enforced bundle (settings.json + the helper "
                f"scripts) for {USER_CONFIG_DIR}/, instead of the default per-OS managed "
                "bundle. The helper paths inside settings.json are baked as absolute "
                f"{USER_CONFIG_DIR} paths, so generate and install on the same machine "
                "(use `make claude-code-install-local`). Ignores --platforms; "
                "requiredMinimumVersion is dropped (it is managed-only)."
            ),
        )
        # ---- platforms ----
        parser.add_argument(
            "--platforms",
            default=",".join(PLATFORMS),
            help=(
                "Comma-separated OSes to emit a self-contained bundle for (any of: "
                f"{', '.join(PLATFORMS)}). Default: all three. Each bundle is written to "
                "claude-code/<platform>/ with a managed-settings.json whose helper/hook "
                "paths point at that OS's ClaudeCode install dir, plus the (identical) "
                "helper scripts."
            ),
        )
        # ---- hook telemetry (custom reporting events via Zerobus REST) ----
        parser.add_argument(
            "--hook-telemetry",
            choices=["auto", "on", "off"],
            default="auto",
            help=(
                "Emit the emit_hook_events.sh reporting hook + a 'hooks' block that streams "
                "custom events (agent-usage, reliability, governance, adoption) to Zerobus "
                "REST. 'auto' (default) enables it iff the Terraform telemetry.hook_events "
                "table is present; 'on' requires it; 'off' skips. The Zerobus endpoint is "
                "auto-derived from workspace metadata (or --zerobus-endpoint / the TF output) "
                "and baked in, but is NOT required — the hooks ship in the baseline and stay "
                "dormant until ZEROBUS_ENDPOINT is known."
            ),
        )
        parser.add_argument(
            "--hook-categories",
            default=",".join(HOOK_CATEGORIES),
            help=(
                "Comma-separated reporting categories to wire up (any of: "
                f"{', '.join(HOOK_CATEGORIES)}). Only the hook events for the selected "
                "categories are registered. Default: all four."
            ),
        )
        parser.add_argument(
            "--hook-doc-patterns",
            default=r"TESTING\.md",
            help=(
                "grep -E alternation of file BASENAMES whose Read counts as a workflow "
                r"adoption event (the 'adoption' category). Default: TESTING\.md. Generalize "
                "to whatever docs a customer wants to track, e.g. 'TESTING\\.md|CONTRIBUTING\\.md'."
            ),
        )
        parser.add_argument(
            "--hook-log-paths",
            action="store_true",
            help=(
                "Include full file paths in adoption events (default: basename only). "
                "Paths can be sensitive; off by default, matching the OTEL content gate."
            ),
        )
        parser.add_argument(
            "--hook-token-ttl-seconds",
            type=int,
            default=600,
            help="Refresh-hint TTL for the cached Zerobus bearer, in seconds (default: 600).",
        )
        parser.add_argument(
            "--zerobus-endpoint",
            default=None,
            help=(
                "Override the Zerobus REST base URL. Precedence: this flag > the Terraform "
                "telemetry.hook_events endpoint > auto-derivation from workspace metadata "
                "(x-databricks-org-id header + UC metastore region; skipped with "
                "--skip-api-discovery). Format: https://<workspace-id>.zerobus.<region>.cloud.databricks.com"
            ),
        )

    def _resolve_tier(self, endpoints: dict[str, Endpoint], tier: str) -> Endpoint | None:
        for pref in TIER_PREFERENCES[tier]:
            if pref in endpoints:
                return endpoints[pref]
        return None

    def _select_anthropic_capable(self, ctx: GatewayContext, args: argparse.Namespace) -> list[Endpoint]:
        """Endpoints Claude Code can use: those exposing the Anthropic API.

        Discovered live per workspace (an endpoint's api types depend on the
        underlying model and the workspace), or approximated by schema when
        discovery is skipped.
        """
        candidates = ctx.endpoints
        if not candidates:
            raise SystemExit("No endpoints found in the Terraform outputs.")

        if args.skip_api_discovery:
            eps = [e for e in candidates if e.schema == args.fallback_schema]
            print(f"[claude-code] discovery skipped; using schema '{args.fallback_schema}' "
                  f"({len(eps)} endpoints).", file=sys.stderr)
        else:
            print(f"[claude-code] discovering supported_api_types for {len(candidates)} endpoints...",
                  file=sys.stderr)
            api_types = discover_api_types([e.full_name for e in candidates], args.profile)
            eps = [e for e in candidates if ANTHROPIC_API_TYPE in api_types.get(e.full_name, [])]
            skipped = sorted({e.schema for e in candidates} - {e.schema for e in eps})
            print(f"[claude-code] {len(eps)}/{len(candidates)} endpoints expose {ANTHROPIC_API_TYPE}"
                  + (f"; schemas without it: {', '.join(skipped)}" if skipped else ""),
                  file=sys.stderr)

        if not eps:
            raise SystemExit(
                f"No endpoints expose the Anthropic API ({ANTHROPIC_API_TYPE}) in this workspace, "
                "so Claude Code cannot route through this gateway."
            )
        return eps

    def _telemetry_env(self, ctx: GatewayContext, args: argparse.Namespace,
                       env: dict[str, str]) -> str | None:
        """Add the OTEL export env (platform-independent) and return the helper script.

        Mutates `env` with the OTLP exporter vars; returns the otel-headers-helper.sh
        content (its `otelHeadersHelper` path is set per-platform by the caller), or
        None when telemetry is off / not deployed. Splits the header set: the sensitive
        Authorization comes from the helper (minted per developer), while content-type
        and per-signal UC table names live in static env, merged by Claude Code.
        """
        if args.telemetry == "off":
            return None
        tel = ctx.telemetry
        if tel is None:
            if args.telemetry == "on":
                raise SystemExit(
                    "--telemetry on, but the Terraform 'telemetry' output is absent. "
                    "Set telemetry_enabled = true and apply, or use --telemetry off."
                )
            return None  # auto + not deployed
        signals = [s for s in ("metrics", "logs", "traces") if s in tel.tables]
        if not signals:
            return None

        base = f"{ctx.host}{OTEL_INGEST_PATH}"
        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        for sig in signals:
            env[OTEL_EXPORTER_ENV[sig]] = "otlp"
            env[OTEL_ENDPOINT_ENV[sig]] = f"{base}/{OTEL_SIGNAL_PATHS[sig]}"
            # A per-signal *_HEADERS var replaces (not merges with) the generic
            # OTEL_EXPORTER_OTLP_HEADERS per the OTEL spec, so include content-type
            # alongside the per-signal table name. Authorization is added by the
            # otelHeadersHelper (which applies to http/protobuf regardless).
            env[OTEL_HEADERS_ENV[sig]] = (
                f"content-type=application/x-protobuf,X-Databricks-UC-Table-Name={tel.tables[sig]}"
            )
        if "metrics" in signals:
            env["OTEL_METRIC_EXPORT_INTERVAL"] = str(args.otel_metric_interval_ms)
        if "logs" in signals:
            env["OTEL_LOGS_EXPORT_INTERVAL"] = str(args.otel_logs_interval_ms)
        # Trace/span export is gated behind the enhanced-telemetry beta: OTEL_TRACES_EXPORTER
        # alone is silently inert without it. It's also the prerequisite for content logging.
        # Enable it whenever traces are exported (or content logging is on), independent of
        # --otel-log-content — which now only adds the privacy-sensitive OTEL_LOG_* vars.
        if "traces" in signals or args.otel_log_content:
            env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
        if args.otel_log_content:
            for key in OTEL_CONTENT_ENV:
                env[key] = "1"
        env["CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS"] = str(args.otel_headers_helper_debounce_ms)

        return _otel_headers_helper_script(
            host=ctx.host,
            profile=args.__dict__.get("profile", "DEFAULT"),
            secret_full_name=tel.secret_full_name,
            databricks_bin=args.databricks_bin,
        )

    def _hook_parts(self, ctx: GatewayContext, args: argparse.Namespace) -> tuple[str | None, list[str]]:
        """Return (emit_hook_events.sh content, enabled categories) — platform-independent.

        The 'hooks' block (with the per-platform script path) is built by the caller.
        Reuses the telemetry service principal + UC secret (the hook mints the same
        M2M bearer the OTEL helper does) and streams events to Zerobus REST. No SDK
        or gRPC is deployed to developer machines — the hook is a curl of a JSON array.
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
        # Resolve the Zerobus endpoint baked into the hook. Precedence:
        #   1. --zerobus-endpoint (explicit override)
        #   2. Terraform telemetry.hook_events.endpoint (operator set telemetry_zerobus_endpoint)
        #   3. auto-derived from workspace metadata (org-id header + metastore region)
        # Auto-derivation makes live workspace calls, so it is skipped in offline mode
        # (--skip-api-discovery), matching how endpoint discovery is gated elsewhere.
        # The endpoint is NOT required: the hooks ship in the baseline regardless and
        # no-op until ZEROBUS_ENDPOINT is known (baked here, or set at runtime).
        endpoint = args.zerobus_endpoint or (he.get("endpoint") if isinstance(he, dict) else "") or ""
        if not endpoint and not args.skip_api_discovery:
            endpoint = _derive_zerobus_endpoint(
                ctx.host, args.__dict__.get("profile", "DEFAULT"), args.databricks_bin
            ) or ""
            if endpoint:
                print(
                    f"[claude-code] hook telemetry: derived Zerobus endpoint {endpoint} "
                    "from workspace metadata (x-databricks-org-id header + UC metastore region).",
                    file=sys.stderr,
                )
        if not endpoint:
            print(
                "[claude-code] hook telemetry: wiring the reporting hooks into "
                "managed-settings.json with NO Zerobus endpoint (auto-derivation "
                "unavailable) — they stay dormant (no-op) until ZEROBUS_ENDPOINT is set "
                "(telemetry_zerobus_endpoint, --zerobus-endpoint, or the env var).",
                file=sys.stderr,
            )

        categories = [c.strip() for c in args.hook_categories.split(",") if c.strip()]
        unknown = [c for c in categories if c not in HOOK_CATEGORIES]
        if unknown:
            raise SystemExit(
                f"--hook-categories has unknown value(s): {', '.join(unknown)}. "
                f"Valid: {', '.join(HOOK_CATEGORIES)}."
            )
        if not categories:
            return None, []

        script = _hook_dispatcher_script(
            endpoint=endpoint,
            table=table,
            secret_full_name=tel.secret_full_name,
            host=ctx.host,
            profile=args.__dict__.get("profile", "DEFAULT"),
            databricks_bin=args.databricks_bin,
            categories=categories,
            doc_patterns=args.hook_doc_patterns,
            log_paths=args.hook_log_paths,
            token_ttl=args.hook_token_ttl_seconds,
        )
        print(f"[claude-code] hook telemetry: {len(categories)} categor"
              f"{'y' if len(categories) == 1 else 'ies'} -> {table}", file=sys.stderr)
        return script, categories

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        # --platforms drives the managed per-OS bundles only; user mode emits a
        # single claude-code/user/ bundle for the local machine, so skip it there.
        if not args.user_config:
            platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
            unknown = [p for p in platforms if p not in PLATFORM_INSTALL_DIRS]
            if unknown:
                raise SystemExit(
                    f"--platforms has unknown value(s): {', '.join(unknown)}. "
                    f"Valid: {', '.join(PLATFORMS)}."
                )
            if not platforms:
                raise SystemExit("--platforms selected no platforms.")

        eps = self._select_anthropic_capable(ctx, args)
        by_name = {e.name: e for e in eps}

        # Resolve each tier to a concrete endpoint (three-level UC name).
        tiers = {t: self._resolve_tier(by_name, t) for t in TIER_PREFERENCES}

        env: dict[str, str] = {
            "ANTHROPIC_BASE_URL": f"{ctx.host}/ai-gateway/anthropic",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": str(args.api_key_ttl_ms),
            # Gateways typically reject pre-release beta headers/fields.
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            # Keep telemetry/version pings off restricted egress networks.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        for tier, ep in tiers.items():
            if ep is None:
                continue
            key = tier.upper()
            # The pin derives from our own Terraform endpoints (data we own) -> cheap
            # to regenerate as models roll. Capabilities/display-names mirror facts we
            # don't own, so they're opt-in only.
            env[f"ANTHROPIC_DEFAULT_{key}_MODEL"] = ep.full_name + _context_suffix(ep.name, args.small_context)
            if args.declare_capabilities:
                env[f"ANTHROPIC_DEFAULT_{key}_MODEL_NAME"] = f"{TIER_DISPLAY[tier]} (Databricks)"
                env[f"ANTHROPIC_DEFAULT_{key}_MODEL_SUPPORTED_CAPABILITIES"] = TIER_CAPABILITIES[tier]

        if args.ssl_cert_file:
            env["SSL_CERT_FILE"] = args.ssl_cert_file
            env["NODE_EXTRA_CA_CERTS"] = args.ssl_cert_file

        settings: dict = {
            "env": env,
            "apiKeyHelper": _api_key_helper(ctx.host, args.__dict__.get("profile", "DEFAULT"), args.databricks_bin),
        }

        # Default model: the chosen tier's endpoint, else the schema's first.
        default_ep = tiers.get(args.default_tier) or eps[0]
        settings["model"] = default_ep.full_name + _context_suffix(default_ep.name, args.small_context)

        # Model governance. Apply the same context suffix so enforceAvailableModels
        # matches the pinned/default model strings exactly.
        if args.lock_models != "none":
            if args.lock_models == "aliases":
                pool = [e for e in eps if e.is_alias]
            else:  # catalog
                pool = list(eps)
            allowed = [e.full_name + _context_suffix(e.name, args.small_context) for e in pool]
            settings["availableModels"] = sorted(set(allowed))
            settings["enforceAvailableModels"] = True

        # Model picker: list every usable endpoint in the interactive /model picker
        # (availableModels only enforces an allow-list; it doesn't add picker rows).
        # Row schema is { model, label?, description? }; aliases first, then pins.
        if args.model_picker:
            picker_pool = sorted(eps, key=lambda e: (not e.is_alias, e.schema, e.name))
            settings["modelPicker"] = {
                "options": [
                    {
                        "model": e.full_name + _context_suffix(e.name, args.small_context),
                        "label": e.name,
                        "description": f"{e.schema} · {e.foundation_model}".rstrip(" ·"),
                    }
                    for e in picker_pool
                ],
                "replaceBuiltInOptions": not args.model_picker_append,
            }

        if not args.allow_websearch:
            settings["permissions"] = {"deny": ["WebSearch"]}

        if args.required_min_version:
            settings["requiredMinimumVersion"] = args.required_min_version

        # Telemetry + hook telemetry contribute platform-independent parts: env vars
        # (mutated in place) and the two helper scripts (identical across platforms).
        # Only the paths managed-settings.json references differ per OS, so we build
        # the base settings once and stamp the per-platform paths in the loop below.
        otel_script = self._telemetry_env(ctx, args, env)
        hook_script, hook_categories = self._hook_parts(ctx, args)

        # --- user (local, non-managed) mode: a single claude-code/user/ bundle ----
        # settings.json for ~/.claude, with helper paths baked as absolute ~/.claude
        # paths (same-machine install). requiredMinimumVersion is managed-only, so
        # drop it here.
        if args.user_config:
            user_dir = os.path.expanduser(USER_CONFIG_DIR)
            user_settings = copy.deepcopy(settings)
            if user_settings.pop("requiredMinimumVersion", None) is not None:
                print("[claude-code] --user-config: dropping requiredMinimumVersion "
                      "(it is managed-only and has no effect in user settings.json).",
                      file=sys.stderr)
            if otel_script is not None:
                user_settings["otelHeadersHelper"] = f"{user_dir}/{OTEL_HELPER_FILENAME}"
            if hook_script is not None:
                user_settings["hooks"] = _hook_settings_block(
                    f"{user_dir}/{HOOK_SCRIPT_FILENAME}", hook_categories
                )
            files: dict[str, str] = {
                "claude-code/user/settings.json": json.dumps(user_settings, indent=2) + "\n",
            }
            if otel_script is not None:
                files[f"claude-code/user/{OTEL_HELPER_FILENAME}"] = otel_script
            if hook_script is not None:
                files[f"claude-code/user/{HOOK_SCRIPT_FILENAME}"] = hook_script
            return files

        files: dict[str, str] = {}
        for platform in platforms:
            install_dir = PLATFORM_INSTALL_DIRS[platform]
            plat_settings = copy.deepcopy(settings)
            if otel_script is not None:
                plat_settings["otelHeadersHelper"] = _platform_path(
                    install_dir, OTEL_HELPER_FILENAME, platform
                )
            if hook_script is not None:
                plat_settings["hooks"] = _hook_settings_block(
                    _platform_path(install_dir, HOOK_SCRIPT_FILENAME, platform), hook_categories
                )
            files[f"claude-code/{platform}/managed-settings.json"] = (
                json.dumps(plat_settings, indent=2) + "\n"
            )
            if otel_script is not None:
                files[f"claude-code/{platform}/{OTEL_HELPER_FILENAME}"] = otel_script
            if hook_script is not None:
                files[f"claude-code/{platform}/{HOOK_SCRIPT_FILENAME}"] = hook_script
        return files

    def install_notes(self, args: argparse.Namespace) -> str:
        if args.user_config:
            lines = [
                "USER-CONFIG mode (--user-config): per-user, non-enforced. One bundle was",
                "written to claude-code/user/ (settings.json + any helper scripts). Deploy it:",
                f"  Copy settings.json (+ helper scripts) -> {USER_CONFIG_DIR}/",
                "  (or run: make claude-code-install-local)",
                "",
                f"The helper paths inside settings.json are baked as absolute {USER_CONFIG_DIR}",
                "paths, so generate and install on the same machine.",
                "",
                "Each developer authenticates once:",
                "  databricks auth login --host <workspace-url> --profile <profile>",
                "",
                "Verify inside Claude Code with /status:",
                "  'Anthropic base URL' -> the gateway address; 'Setting sources' -> User settings.",
            ]
            if args.telemetry != "off" or args.hook_telemetry != "off":
                lines += [
                    "",
                    f"Any helper scripts (otel-headers-helper.sh / emit_hook_events.sh) install to",
                    f"  {USER_CONFIG_DIR}/ beside settings.json (the install script sets them executable).",
                    "  They need python3 + the databricks CLI on PATH (emit_hook_events.sh also jq + curl),",
                    "  and READ_SECRET on the telemetry UC secret.",
                ]
            return "\n".join(lines)

        platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORM_INSTALL_DIRS]
        lines = [
            "Per-platform bundles were written to claude-code/<platform>/. Deploy each",
            "OS's managed-settings.json (push via MDM) to that OS's ClaudeCode path,",
            "and copy the helper scripts from the SAME bundle into the SAME directory",
            "(the paths inside managed-settings.json already point there):",
        ]
        for p in platforms:
            lines.append(f"  {p:8}: {INSTALL_PATHS[p]}")
            lines.append(f"  {'':8}  (+ helper scripts in {PLATFORM_INSTALL_DIRS[p]})")
        lines += [
            "",
            "Each developer authenticates once:",
            "  databricks auth login --host <workspace-url> --profile <profile>",
            "",
            "Verify inside Claude Code with /status:",
            "  'Anthropic base URL' -> the gateway address; 'Setting sources' -> Enterprise managed settings.",
        ]
        if args.telemetry != "off":
            lines += [
                "",
                "Telemetry (otel-headers-helper.sh ships in each bundle):",
                "  chmod +x it after deploy; the managed-settings.json 'otelHeadersHelper' points at it.",
                "  Requires python3 + the databricks CLI on PATH, and the developer must hold",
                "  READ_SECRET on the telemetry UC secret (grant a group via telemetry_reader_groups).",
                "  Verify OTLP export health in /status; errors also surface with --debug.",
            ]
        if args.hook_telemetry != "off":
            lines += [
                "",
                "Hook telemetry (emit_hook_events.sh ships in each bundle):",
                "  chmod +x it after deploy; the managed-settings.json 'hooks' block points at it.",
                "  Same deps as the OTEL helper (python3 + databricks CLI + READ_SECRET on the",
                "  UC secret), plus jq + curl. Reuses the telemetry service principal; events",
                "  land in the telemetry.hook_events table via Zerobus REST. Report-only —",
                "  it never blocks a tool call. Confirm the Zerobus REST API is available in",
                "  the customer's workspace region before rollout.",
                "  Windows runs the .sh helper/hook via Claude Code's shell (Git Bash) — verify",
                "  the C:\\ paths resolve there before a Windows rollout.",
            ]
        return "\n".join(lines)
