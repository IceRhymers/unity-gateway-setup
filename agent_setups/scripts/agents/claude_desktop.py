"""Claude Desktop config generator for the Unity AI Gateway.

This generator emits the parts of a Claude Desktop setup that MDM owns, and a
bootstrap script that delegates everything else to `ug`.

Division of labour
------------------
`ug` is the developer surface. It holds a workspace-published managed config
(authored with `ug setup`, pushed with `ug publish`) that decides the agents,
the models, the MCP servers, and the skills. `ug configure` writes the agent
config files and records the keys it owns in ~/.ucode/state.json, so `ug revert`
can unwind them. Model selection therefore belongs to `ug`, not to MDM: pushing
a model list from here would duplicate — and fight — the list `ug` publishes.

MDM owns only what `ug` cannot enforce:

  * Telemetry (the otlp block + its headers helper). The OTEL export target is a
    fleet decision tied to Unity Catalog tables, not a per-developer one.
  * Enterprise lockdown (disableClaudeAiSignIn, allowedEgressHosts,
    disabledBuiltinTools). A developer who signs in to Claude.ai bypasses the
    gateway completely, and only a managed profile closes that.

So this generator does NOT emit inference or models. It emits:

  1. claude-setup.json — the MDM-owned policy + telemetry config.
  2. ug-bootstrap-claude-desktop.sh — a headless script that runs
     `ug configure`, reads the workspace, base URL, profile, and Claude model
     pins back out of ~/.ucode/state.json, and splices an inference + models
     block into a complete config for the app to import.
  3. The helper scripts both reference by absolute path.

The generator still does not produce the .mobileconfig or .reg artifacts. The
Claude Desktop app exports those after an operator imports the JSON once.

The credential helper is a thin wrapper around `ucode auth-token` — ug's own
cross-platform token helper, and the same one Claude Code's apiKeyHelper and
Codex's auth command already use. It carries no auth logic: ug resolves the
workspace and profile from its own state, handles static PATs, retries
token-cache lock contention, and re-authenticates non-interactively on expiry.
So Claude Desktop authenticates as exactly the same identity, through the same
code path, as every ug-launched agent.

The wrapper exists only to strip the trailing newline ug prints and to resolve
the ucode binary by absolute path, because Claude Desktop starts under launchd
(macOS) with a minimal PATH.

Conventions follow the internal "Onboarding Coding Agents - AI Gateway" playbook
and the "DBX - Inference Configuration" customer document.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import urllib.parse

from agents.base import AgentGenerator
from agents.claude_code import (
    OTEL_INGEST_PATH,
    _otel_headers_helper_script,
)
from gateway import GatewayContext

# Claude Desktop config schema version (nested-object form). See the configuration
# changelog: version 2 is the importable JSON shape used by the in-app window.
SCHEMA_VERSION = 2

# Per-OS bundles. Each bundle is written to claude-desktop/<platform>/ with an
# importable claude-setup.json plus the helper scripts it references. The install
# directory below is where the helper scripts are placed (and what the JSON's
# credential.command / otlp.headersHelper point at). Windows uses a machine-wide
# ProgramData path so the same absolute path works for every user on the device.
PLATFORMS = ("macos", "windows", "linux")
DEFAULT_PLATFORMS = ("macos", "windows")
PLATFORM_INSTALL_DIRS = {
    "macos": "/Library/Application Support/ClaudeDesktop",
    "windows": r"C:\ProgramData\ClaudeDesktop",
    "linux": "/etc/claude-desktop",
}

# The MDM-owned config filename (policy + telemetry only).
CONFIG_FILENAME = "claude-setup.json"

# The headless bootstrap script, and the complete config it writes. The bootstrap
# merges the MDM-owned config above with an inference + models block it derives
# from ug's own state, so the merged file is what the app imports.
BOOTSTRAP_FILENAME = "ug-bootstrap-claude-desktop.sh"
MERGED_CONFIG_FILENAME = "claude-setup.merged.json"

# Where ug records the workspace it is configured against, the Databricks CLI
# profile it authenticates with, the per-agent gateway base URLs, and the Claude
# model pins it resolved. Both the bootstrap and the credential helper read it, so
# Claude Desktop tracks whatever `ug configure` last decided.
UG_STATE_PATH = "$HOME/.ucode/state.json"

# Credential-helper filenames per OS. Windows needs a .cmd shim because Claude
# Desktop runs an executable and a .ps1 is not directly runnable; the shim runs
# PowerShell against the .ps1. The credential.command in the JSON points at the
# .sh (macOS/Linux) or the .cmd (Windows).
CRED_HELPER_SH = "databricks-token.sh"
CRED_HELPER_PS1 = "databricks-token.ps1"
CRED_HELPER_CMD = "databricks-token.cmd"

# OTEL headers-helper filenames per OS (same .cmd-shim rule on Windows).
OTEL_HELPER_SH = "otel-headers-helper.sh"
OTEL_HELPER_PS1 = "otel-headers-helper.ps1"
OTEL_HELPER_CMD = "otel-headers-helper.cmd"


def _platform_path(install_dir: str, filename: str, platform: str) -> str:
    """Join an install dir + filename with the platform's path separator."""
    sep = "\\" if platform == "windows" else "/"
    return f"{install_dir}{sep}{filename}"


# Baked-value guards. profile, host, and the traces table are substituted into the
# bash / PowerShell helper templates (and the JSON). Databricks constrains these in
# practice, but validate before baking so a hand-passed value cannot inject shell or
# PowerShell syntax (defense-in-depth).
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_UC_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_HOST_UNSAFE_CHARS = "'\"`$;\\ "


def _validate_bakeables(profile: str, host: str, traces_table: str | None) -> None:
    """Reject a profile / host / table that is unsafe to bake into a script template."""
    if not _PROFILE_RE.match(profile):
        raise SystemExit(
            f"Refusing to bake an unsafe profile name into the helper scripts: {profile!r}. "
            "Databricks profile names use letters, digits, '_', '.', and '-'."
        )
    parsed = urllib.parse.urlparse(host)
    if parsed.scheme not in ("http", "https") or not parsed.netloc \
            or any(c in host for c in _HOST_UNSAFE_CHARS):
        raise SystemExit(
            f"Refusing to bake an unsafe workspace host into the config and scripts: {host!r}. "
            "Expected an http(s) URL with no shell metacharacters."
        )
    if traces_table is not None and not _UC_TABLE_RE.match(traces_table):
        raise SystemExit(
            f"Refusing to bake an unsafe UC table name into the OTEL helper: {traces_table!r}. "
            "Expected a three-level <catalog>.<schema>.<table> identifier."
        )


# --- Credential helper: bash (macOS / Linux) --------------------------------
# A thin wrapper around `ucode auth-token`, which is ug's own token helper (a
# hidden command: `@app.command("auth-token", hidden=True)`). It is the same helper
# Claude Code's apiKeyHelper and Codex's auth command already use, so Claude Desktop
# authenticates through exactly one code path as every ug-launched agent.
#
# Delegating means we do NOT reimplement any of this, all of which lives in ug's
# get_databricks_token:
#   * the DATABRICKS_BEARER CI short-circuit,
#   * resolving the profile from the host when none is given,
#   * static-PAT profiles (including the use_pat flag saved in ug's state),
#   * token-cache lock contention, retried with jittered backoff — this matters,
#     because Claude Desktop re-runs the helper whenever ttlSec expires while
#     ug-launched agents hit the same cache,
#   * non-interactive re-auth (`databricks auth login --no-browser`) on expiry.
#
# `ucode auth-token` also reads the workspace and profile from ug's own state, so
# nothing needs to be baked here. __PROFILE__ is passed only as an explicit
# fallback for a device whose ug state is not yet written.
#
# The wrapper exists for two reasons only:
#   1. `ucode auth-token` prints the token WITH a trailing newline
#      (cli.py: sys.stdout.write(token + "\n")). Claude Desktop's credential
#      contract wants the bare token, so we strip it.
#   2. Claude Desktop starts under launchd with a minimal PATH, so the binary is
#      resolved from an absolute-path candidate list.
_CRED_HELPER_SH_TEMPLATE = r"""#!/usr/bin/env sh
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
#
# Databricks credential helper for Claude Desktop.
#
# This is a THIN WRAPPER around `ucode auth-token`, ug's own cross-platform token
# helper. All token logic lives there. Do not add any here.
#
# POSIX sh, so it runs under dash as well as bash. No bashisms, and no
# `set -o pipefail` (dash rejects it, and there is no pipeline that needs it).
#
# Output contract:
#   stdout - the raw bearer token, no trailing newline. Nothing else.
#   stderr - diagnostics from ucode, plus our own errors.
#   exit 0 - success
#   exit 1 - ucode not found, or ucode auth-token failed
set -u

# Resolve the ucode/ug binary WITHOUT trusting $PATH. Claude Desktop starts under
# launchd with a minimal environment that does not inherit the user's PATH. ug
# resolves its own absolute path for exactly this reason (see _ucode_binary), but
# the path baked into a generated config must survive on its own.
resolve_ucode() {
  if [ -n "${UCODE_BIN:-}" ]; then
    if [ -x "$UCODE_BIN" ]; then
      printf '%s' "$UCODE_BIN"
      return 0
    fi
    echo "databricks-token: UCODE_BIN=$UCODE_BIN is not executable" >&2
    return 1
  fi
  for candidate in \
    "$HOME/.local/bin/ucode" \
    "$HOME/.local/bin/ug" \
    /opt/homebrew/bin/ucode \
    /opt/homebrew/bin/ug \
    /usr/local/bin/ucode \
    /usr/local/bin/ug \
    /usr/bin/ucode \
    /usr/bin/ug
  do
    if [ -x "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  echo "databricks-token: ucode/ug not found. Install ug, or set UCODE_BIN to its absolute path." >&2
  return 1
}

ucode="$(resolve_ucode)" || exit 1

# `ucode auth-token` defaults --host and --profile to ug's saved state, so the
# common case needs no arguments at all. __PROFILE__ is passed only when ug has no
# saved profile yet, so a fresh device still authenticates.
set -- auth-token
if [ -n "${DATABRICKS_PROFILE:-}" ]; then
  set -- "$@" --profile "$DATABRICKS_PROFILE"
elif ! [ -r "${UCODE_STATE:-$HOME/.ucode/state.json}" ]; then
  set -- "$@" --profile "__PROFILE__"
  echo "databricks-token: no ug state yet, passing the baked profile '__PROFILE__'" >&2
fi

# Capture stdout so the trailing newline can be stripped. ucode writes the token
# as `token + "\n"`; Claude Desktop wants the bare token.
token="$("$ucode" "$@")" || {
  echo "databricks-token: ucode auth-token failed" >&2
  exit 1
}

if [ -z "$token" ]; then
  echo "databricks-token: ucode auth-token returned an empty token" >&2
  exit 1
fi

# Bare token to stdout. No trailing newline. $(...) already stripped it.
printf '%s' "$token"
"""


# --- Credential helper: PowerShell (Windows) --------------------------------
# Also a thin wrapper around `ucode auth-token`. ug ships ONE cross-platform token
# helper precisely so this file needs no auth logic: no CLI discovery, no JSON
# parsing, no login retry. That removes the whole untested PowerShell auth path the
# earlier version carried.
#
# Claude Desktop points credential.command at the .cmd shim below, which runs this
# .ps1. The .ps1 exists only to strip ucode's trailing newline and force UTF-8.
_CRED_HELPER_PS1_TEMPLATE = r"""# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
# databricks-token.ps1
#
# Databricks credential helper for Claude Desktop on Windows.
#
# A THIN WRAPPER around `ucode auth-token`. All token logic lives in ug. Do not add
# any here.
#
# Output contract:
#   stdout - the raw bearer token, no trailing newline. Nothing else.
#   stderr - diagnostics from ucode, plus our own errors.
#   exit 0 - success
#   exit 1 - ucode not found, or ucode auth-token failed

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
if (Get-Variable -Name 'PSNativeCommandUseErrorActionPreference' -Scope Global -ErrorAction SilentlyContinue) {
    $Global:PSNativeCommandUseErrorActionPreference = $false
}
# Force raw UTF-8 (no BOM) on stdout so the bare-token contract reaches Claude
# Desktop byte-identical (a UTF-16 BOM would break Authorization headers).
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

# Resolve the ucode/ug executable without relying on PATH.
function Resolve-Ucode {
    if ($env:UCODE_BIN) {
        if (Test-Path -LiteralPath $env:UCODE_BIN -PathType Leaf) { return $env:UCODE_BIN }
        [Console]::Error.WriteLine("databricks-token: UCODE_BIN=$($env:UCODE_BIN) is not a file")
        exit 1
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\ucode\ucode.exe",
        "$env:LOCALAPPDATA\uv\tools\ucode\Scripts\ucode.exe",
        "$env:LOCALAPPDATA\uv\tools\ucode\Scripts\ug.exe",
        "$env:USERPROFILE\.local\bin\ucode.exe",
        "$env:USERPROFILE\.local\bin\ug.exe",
        "$env:ProgramFiles\ucode\ucode.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return $c }
    }
    $onPath = Get-Command ucode -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    [Console]::Error.WriteLine("databricks-token: ucode/ug not found. Install ug, or set UCODE_BIN to its absolute path.")
    exit 1
}
$ucode = Resolve-Ucode

# `ucode auth-token` defaults --host and --profile to ug's saved state, so the
# common case needs no arguments. '__PROFILE__' is passed only when ug has no saved
# state yet, so a fresh device still authenticates.
$ugArgs = @('auth-token')
if ($env:DATABRICKS_PROFILE) {
    $ugArgs += @('--profile', $env:DATABRICKS_PROFILE)
} else {
    $statePath = if ($env:UCODE_STATE) { $env:UCODE_STATE } else { Join-Path $env:USERPROFILE '.ucode\state.json' }
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        $ugArgs += @('--profile', '__PROFILE__')
        [Console]::Error.WriteLine("databricks-token: no ug state yet, passing the baked profile '__PROFILE__'")
    }
}

$token = (& $ucode @ugArgs | Out-String)
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine("databricks-token: ucode auth-token failed")
    exit 1
}
# ucode writes the token as `token + "\n"`; Claude Desktop wants the bare token.
$token = $token.Trim()
if (-not $token) {
    [Console]::Error.WriteLine("databricks-token: ucode auth-token returned an empty token")
    exit 1
}

# Bare token to stdout. No trailing newline.
[Console]::Out.Write($token)
exit 0
"""


def _cmd_shim(ps1_filename: str, purpose: str) -> str:
    """A .cmd shim that runs a sibling .ps1 and forwards its stdout unchanged.

    Claude Desktop runs an executable for credential.command / otlp.headersHelper;
    a .ps1 is not directly runnable, so the config points at this shim. `%~dp0`
    resolves to the shim's own directory, so the .ps1 must sit beside it.
    """
    return (
        "@echo off\r\n"
        "REM Generated by unity-gateway-setup (agent_setups). Do not edit by hand.\r\n"
        f"REM Claude Desktop {purpose} shim: runs the PowerShell helper beside it and\r\n"
        "REM forwards its stdout (bare token / JSON headers) unchanged.\r\n"
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f'-File "%~dp0{ps1_filename}"\r\n'
    )


# --- OTEL headers helper: PowerShell (Windows) ------------------------------
# THEORETICAL Windows port of _otel_headers_helper_script (the bash helper).
# Reads the OTEL SP credentials from the UC secret (as the developer, who holds
# READ_SECRET), mints a short-lived M2M token DOWN-SCOPED to the OTEL UC tables
# via an OAuth authorization_details block, and prints {"Authorization":"Bearer <t>"}.
# Test on Windows before a production rollout.
_OTEL_HELPER_PS1_TEMPLATE = r"""# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
# otel-headers-helper.ps1
#
# Claude Desktop otlpHeadersHelper (Windows): prints a JSON object of HTTP headers
# for OTLP export — {"Authorization": "Bearer <token>"}.
#
# THEORETICAL: test on Windows before distribution.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

# Baked defaults (override via environment for fleet reuse).
$secretFullName = if ($env:OTEL_UC_SECRET) { $env:OTEL_UC_SECRET } else { '__SECRET__' }
$profileName    = if ($env:DATABRICKS_PROFILE) { $env:DATABRICKS_PROFILE } else { '__PROFILE__' }
$tokenHost      = if ($env:DATABRICKS_HOST) { $env:DATABRICKS_HOST } else { '__HOST__' }
# The OTEL UC tables the export writes to; the token is down-scoped to just these.
$ucTablesCsv    = if ($env:OTEL_UC_TABLES) { $env:OTEL_UC_TABLES } else { '__TABLES__' }

# Resolve the Databricks CLI without relying on PATH.
function Resolve-DatabricksCli {
    if ($env:DATABRICKS_CLI -and (Test-Path -LiteralPath $env:DATABRICKS_CLI -PathType Leaf)) {
        return $env:DATABRICKS_CLI
    }
    $candidates = @(
        "$env:ProgramFiles\Databricks\databricks.exe",
        "$env:LOCALAPPDATA\Programs\Databricks\databricks.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\databricks.exe",
        "$env:USERPROFILE\.local\bin\databricks.exe",
        "$env:USERPROFILE\bin\databricks.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return $c }
    }
    throw "databricks CLI not found. Set DATABRICKS_CLI to its absolute path."
}
$cli = Resolve-DatabricksCli

# 1. Read the SP OAuth credentials from the UC secret (CLI handles the dev's auth).
$secretJson = & $cli api get "/api/2.1/unity-catalog/secrets/$secretFullName?include_value=true" --profile $profileName
$secret = $secretJson | ConvertFrom-Json
$creds  = $secret.effective_value | ConvertFrom-Json

# 2. Build authorization_details so the bearer can only write the OTEL tables.
$tables = @($ucTablesCsv -split ',' | Where-Object { $_ })
$parts  = $tables[0] -split '\.'
$ad = @(
    @{ type = 'unity_catalog_privileges'; privileges = @('USE CATALOG'); object_type = 'CATALOG'; object_full_path = $parts[0] },
    @{ type = 'unity_catalog_privileges'; privileges = @('USE SCHEMA');  object_type = 'SCHEMA';  object_full_path = ($parts[0..1] -join '.') }
)
foreach ($t in $tables) {
    $ad += @{ type = 'unity_catalog_privileges'; privileges = @('SELECT', 'MODIFY'); object_type = 'TABLE'; object_full_path = $t }
}

# 3. Client-credentials against the workspace OIDC token endpoint.
$body = @{
    grant_type            = 'client_credentials'
    scope                 = 'all-apis'
    authorization_details = ($ad | ConvertTo-Json -Depth 6 -Compress)
}
$basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($creds.client_id):$($creds.client_secret)"))
$resp = Invoke-RestMethod -Method Post -Uri ($tokenHost.TrimEnd('/') + '/oidc/v1/token') `
    -Headers @{ Authorization = "Basic $basic" } `
    -ContentType 'application/x-www-form-urlencoded' -Body $body
$headers = @{ Authorization = "Bearer $($resp.access_token)" }
[Console]::Out.Write(($headers | ConvertTo-Json -Compress))
"""


# --- Headless bootstrap: bash (macOS / Linux) -------------------------------
# Runs `ug configure` for Claude Code, then reads the workspace, profile, gateway
# base URL, and Claude model pins back out of ~/.ucode/state.json and splices an
# inference + models block into the MDM-owned config. The merged file is what the
# operator (or a device-management script) imports into Claude Desktop.
#
# Claude Desktop has no ug target of its own yet. It shares Claude Code's gateway
# base URL (…/ai-gateway/anthropic) and its Claude model pins, so `--agents claude`
# is what populates the state this script reads. When ug grows a custom model
# surface, this script is what it replaces.
_BOOTSTRAP_SH_TEMPLATE = r"""#!/usr/bin/env sh
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
#
# ug-bootstrap-claude-desktop.sh — configure Claude Desktop from ug, headlessly.
#
# POSIX sh, deliberately: the runbook and install-claude-desktop-local.sh both
# invoke this as `sh <script>`, and /bin/sh is dash on Debian and Ubuntu. Keep it
# free of bashisms — no `set -o pipefail`, no `[[`, no `local`.
#
# MDM owns policy and telemetry (claude-setup.json). ug owns the workspace, the
# identity, and the models. This script joins the two: it runs `ug configure`,
# reads what ug decided, and writes a complete config for the app to import.
#
# Usage:
#   ug-bootstrap-claude-desktop.sh [OPTIONS]
#
# Options:
#   --profile <name>    Databricks CLI profile to configure ug against.
#                       Default: whatever ug already uses, else $DATABRICKS_PROFILE.
#   --config <path>     MDM-owned policy+telemetry JSON to merge.
#                       Default: claude-setup.json beside this script.
#   --out <path>        Where to write the merged config.
#                       Default: claude-setup.merged.json beside --config.
#   --default-tier <t>  Family listed first, i.e. the app default (default: sonnet).
#   --small-context     Do not request 1M context for the opus/sonnet families.
#   --use-pat           Pass --use-pat to ug (PAT from ~/.databrickscfg, no browser).
#                       Use this for CI and any truly non-interactive run.
#   --skip-configure    Do not run `ug configure`; only read existing ug state.
#   --dry-run           Print the merged config to stdout, write nothing.
#   -h, --help          Show this message.
#
# Exit codes:
#   0 success / --dry-run
#   1 usage error
#   2 ug not found
#   3 `ug configure` failed
#   4 ug state unusable (no workspace, base URL, or Claude models)
#   5 MDM config missing or unreadable, or the merged write failed
#
# No `pipefail`: dash does not support it, and no pipeline here needs it.
set -eu

_self_dir="$(cd "$(dirname "$0")" && pwd)"

PROFILE="${DATABRICKS_PROFILE:-}"
CONFIG="${_self_dir}/__CONFIG_FILENAME__"
OUT=""
DEFAULT_TIER="sonnet"
SMALL_CONTEXT=0
USE_PAT=0
SKIP_CONFIGURE=0
DRY_RUN=0

_info() { printf '[ug-bootstrap] %s\n' "$*" >&2; }
_fatal() { _c="$1"; shift; printf '[ug-bootstrap] FATAL: %s\n' "$*" >&2; exit "$_c"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)        shift; PROFILE="${1:?--profile requires a value}" ;;
    --config)         shift; CONFIG="${1:?--config requires a value}" ;;
    --out)            shift; OUT="${1:?--out requires a value}" ;;
    --default-tier)   shift; DEFAULT_TIER="${1:?--default-tier requires a value}" ;;
    --small-context)  SMALL_CONTEXT=1 ;;
    --use-pat)        USE_PAT=1 ;;
    --skip-configure) SKIP_CONFIGURE=1 ;;
    --dry-run)        DRY_RUN=1 ;;
    -h|--help)        sed -n '3,34p' "$0" >&2; exit 1 ;;
    *)                _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

[ -n "$OUT" ] || OUT="$(dirname "$CONFIG")/__MERGED_FILENAME__"

# Validate arguments BEFORE probing the environment, so a usage mistake reports the
# usage mistake — not whatever happens to be missing on this machine.
if [ "$USE_PAT" = "1" ] && [ -z "$PROFILE" ]; then
  _fatal 1 "--use-pat requires --profile (ug's --use-pat requires --profiles)."
fi

# python3 is required here (unlike the credential helper) because this script
# merges JSON. Claude Desktop itself never runs this script.
command -v python3 >/dev/null 2>&1 || _fatal 2 "python3 is required to merge the config."

# Resolve ug without trusting $PATH — this may run from a device-management
# payload with a minimal environment.
resolve_ug() {
  if [ -n "${UG_BIN:-}" ] && [ -x "${UG_BIN}" ]; then printf '%s' "${UG_BIN}"; return 0; fi
  for c in \
    "$HOME/.local/bin/ug" \
    /opt/homebrew/bin/ug \
    /usr/local/bin/ug \
    /usr/bin/ug
  do
    [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  command -v ug 2>/dev/null && return 0
  return 1
}
UG_STATE="${UCODE_STATE:-$HOME/.ucode/state.json}"

# --- 1. Let ug configure the workspace, identity, and models -----------------
# ug is only needed when we actually configure; --skip-configure reads state alone.
if [ "$SKIP_CONFIGURE" = "0" ]; then
  UG="$(resolve_ug)" || _fatal 2 "ug not found. Install it, or set UG_BIN to its absolute path."
  _info "using ug at ${UG}"
  set -- configure --agents claude --skip-validate --skip-upgrade --skip-unavailable --verbose low
  if [ -n "$PROFILE" ]; then
    set -- "$@" --profiles "$PROFILE"
    [ "$USE_PAT" = "1" ] && set -- "$@" --use-pat
  fi
  _info "running: ug $*"
  "$UG" "$@" || _fatal 3 "ug configure failed."
else
  _info "--skip-configure: reading existing ug state only"
fi

[ -r "$UG_STATE" ] || _fatal 4 "ug state not readable at ${UG_STATE}. Run \`ug configure\` first."
[ -r "$CONFIG" ] || _fatal 5 "MDM config not readable at ${CONFIG}."

# --- 2. Merge ug's decisions into the MDM-owned policy+telemetry config ------
# Reads ug state, builds the inference + models blocks, and splices them in. The
# MDM-owned keys (otlp, authentication, workspace) are preserved untouched.
python3 - "$UG_STATE" "$CONFIG" "$OUT" "$DEFAULT_TIER" "$SMALL_CONTEXT" "$DRY_RUN" <<'PYEOF' || exit $?
import json, os, sys, tempfile

state_path, cfg_path, out_path, default_tier, small_ctx, dry_run = sys.argv[1:7]
small_ctx = small_ctx == "1"
dry_run = dry_run == "1"

def die(code, msg):
    sys.stderr.write("[ug-bootstrap] FATAL: %s\n" % msg)
    sys.exit(int(code))

try:
    with open(state_path) as fh:
        state = json.load(fh)
except Exception as exc:
    die(4, "cannot parse ug state %s: %s" % (state_path, exc))

workspace = state.get("current_workspace")
if not workspace:
    die(4, "ug state has no current_workspace. Run `ug configure`.")
ws = (state.get("workspaces") or {}).get(workspace) or {}

base_url = (ws.get("base_urls") or {}).get("claude")
if not base_url:
    die(4, "ug state has no base_urls.claude for %s. Run `ug configure --agents claude`." % workspace)

# ug records the Claude pins as {tier: three-level UC name}. Claude Desktop only
# accepts models whose name contains "claude", so drop anything else rather than
# letting the app reject the whole import.
pins = ws.get("claude_models") or {}
models = {}
for tier, name in pins.items():
    if not name:
        continue
    if "claude" not in str(name).lower():
        sys.stderr.write(
            "[ug-bootstrap] skipping %s pin %r: Claude Desktop only accepts names "
            "containing 'claude'.\n" % (tier, name)
        )
        continue
    models[tier] = name
if not models:
    die(4, "ug state lists no usable Claude model pins for %s." % workspace)

# Families that get 1M context, matching the Claude Code generator.
LARGE = ("opus", "sonnet")
# The tiers Claude Desktop recognizes for anthropicFamilyTier.
KNOWN = ("opus", "sonnet", "haiku")

def entry(tier, name):
    one_m = (not small_ctx) and tier in LARGE
    item = {
        "name": name,
        "supports1m": one_m,
        "prefer1m": one_m,
        # These are ug's per-family pins, so each is that family's default.
        "isFamilyDefault": True,
    }
    if tier in KNOWN:
        item["anthropicFamilyTier"] = tier
    return item

# Default tier first — Claude Desktop treats the first entry as the default.
ordered = sorted(models.items(), key=lambda kv: (kv[0] != default_tier, kv[0]))
model_list = [entry(t, n) for t, n in ordered]

try:
    with open(cfg_path) as fh:
        cfg = json.load(fh)
except Exception as exc:
    die(5, "cannot parse MDM config %s: %s" % (cfg_path, exc))

# The MDM config's telemetry endpoint was stamped from the Terraform workspace. ug
# may be configured against a different one. That combination sends traces to one
# workspace and inference to another, which is almost never intended, so say so
# loudly rather than emitting a config that silently splits the two.
otlp_endpoint = (cfg.get("otlp") or {}).get("endpoint") or ""
if otlp_endpoint:
    def host_of(url):
        rest = url.split("://", 1)[-1]
        return rest.split("/", 1)[0].lower()
    if host_of(otlp_endpoint) != host_of(workspace):
        sys.stderr.write(
            "[ug-bootstrap] WARNING: telemetry and inference point at different "
            "workspaces.\n"
            "[ug-bootstrap]   otlp endpoint (from MDM config): %s\n"
            "[ug-bootstrap]   ug workspace (inference):        %s\n"
            "[ug-bootstrap] Regenerate the bundle against the workspace ug uses, or "
            "point ug at the workspace the telemetry tables live in.\n"
            % (host_of(otlp_endpoint), host_of(workspace))
        )

# Splice in the ug-derived halves. Everything else in cfg is MDM-owned.
cfg["inference"] = {
    "provider": "gateway",
    "baseUrl": base_url,
    "credential": {
        "kind": "helper-script",
        "command": "__CRED_COMMAND__",
        "ttlSec": __TTL_SEC__,
        "timeoutSec": __TIMEOUT_SEC__,
    },
}
cfg["models"] = {"discoveryEnabled": False, "list": model_list}
cfg["chatSurface"] = {"enabled": True}
cfg["extensions"] = {"enabled": True}

rendered = json.dumps(cfg, indent=2) + "\n"
sys.stderr.write(
    "[ug-bootstrap] workspace=%s\n[ug-bootstrap] baseUrl=%s\n[ug-bootstrap] models=%s\n"
    % (workspace, base_url, ", ".join("%s=%s" % (t, n) for t, n in ordered))
)

if dry_run:
    sys.stdout.write(rendered)
    sys.exit(0)

# Atomic write, so a partial file never gets imported.
out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
try:
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".claude-setup.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(rendered)
        os.replace(tmp, out_path)
    except Exception:
        os.unlink(tmp)
        raise
except Exception as exc:
    die(5, "cannot write %s: %s" % (out_path, exc))
sys.stderr.write("[ug-bootstrap] wrote %s\n" % out_path)
PYEOF

if [ "$DRY_RUN" = "0" ]; then
  printf '\nImport this file into Claude Desktop:\n  %s\n' "$OUT"
  printf 'Help -> Troubleshooting -> Enable Developer Mode, then\n'
  printf 'Developer -> Configure third-party inference -> import.\n\n'
fi
"""


def _bootstrap_files(platform: str, install_dir: str, cred_command: str,
                     ttl_sec: int, timeout_sec: int) -> dict[str, str]:
    """Return the headless bootstrap script for a platform.

    Only macOS and Linux get one. `ug configure` is the same command on Windows, but
    this script is bash; a Windows port is a follow-up (see the runbook).
    """
    if platform == "windows":
        return {}
    script = (
        _BOOTSTRAP_SH_TEMPLATE
        .replace("__CONFIG_FILENAME__", CONFIG_FILENAME)
        .replace("__MERGED_FILENAME__", MERGED_CONFIG_FILENAME)
        .replace("__CRED_COMMAND__", cred_command)
        .replace("__TTL_SEC__", str(ttl_sec))
        .replace("__TIMEOUT_SEC__", str(timeout_sec))
    )
    return {BOOTSTRAP_FILENAME: script}


def _cred_helper_files(platform: str, install_dir: str, profile: str) -> dict[str, str]:
    """Return the credential-helper file(s) for a platform, plus the command path.

    macOS/Linux: one databricks-token.sh (the credential.command target).
    Windows: databricks-token.ps1 (logic) + databricks-token.cmd (the command target).
    """
    if platform == "windows":
        return {
            CRED_HELPER_PS1: _CRED_HELPER_PS1_TEMPLATE.replace("__PROFILE__", profile),
            CRED_HELPER_CMD: _cmd_shim(CRED_HELPER_PS1, "credential helper"),
        }
    return {CRED_HELPER_SH: _CRED_HELPER_SH_TEMPLATE.replace("__PROFILE__", profile)}


def _cred_command_filename(platform: str) -> str:
    """The helper file the JSON's credential.command points at, for a platform."""
    return CRED_HELPER_CMD if platform == "windows" else CRED_HELPER_SH


def _otel_helper_files(platform: str, host: str, profile: str, secret_full_name: str,
                       databricks_bin: str, otel_tables: list[str]) -> dict[str, str]:
    """Return the OTEL headers-helper file(s) for a platform, plus the helper path.

    macOS/Linux: reuse the proven bash helper from claude_code.
    Windows: a PowerShell helper (theoretical) + a .cmd shim (the headersHelper target).
    """
    if platform == "windows":
        ps1 = (
            _OTEL_HELPER_PS1_TEMPLATE
            .replace("__SECRET__", secret_full_name)
            .replace("__PROFILE__", profile)
            .replace("__HOST__", host)
            .replace("__TABLES__", ",".join(otel_tables))
        )
        return {
            OTEL_HELPER_PS1: ps1,
            OTEL_HELPER_CMD: _cmd_shim(OTEL_HELPER_PS1, "otlpHeadersHelper"),
        }
    return {
        OTEL_HELPER_SH: _otel_headers_helper_script(
            host=host,
            profile=profile,
            secret_full_name=secret_full_name,
            databricks_bin=databricks_bin,
            otel_tables=otel_tables,
        )
    }


def _otel_helper_filename(platform: str) -> str:
    """The helper file the JSON's otlp.headersHelper points at, for a platform."""
    return OTEL_HELPER_CMD if platform == "windows" else OTEL_HELPER_SH


class ClaudeDesktopGenerator(AgentGenerator):
    name = "claude-desktop"
    help = "Generate an importable Claude Desktop third-party inference config for MDM deployment."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--platforms",
            default=",".join(DEFAULT_PLATFORMS),
            help=(
                "Comma-separated OSes to emit a bundle for (any of: "
                f"{', '.join(PLATFORMS)}). Default: {', '.join(DEFAULT_PLATFORMS)}. Each bundle "
                "is written to claude-desktop/<platform>/ with claude-setup.json plus the helper "
                "scripts whose absolute paths the JSON references."
            ),
        )
        parser.add_argument(
            "--install-dir-macos",
            default=PLATFORM_INSTALL_DIRS["macos"],
            help=f"Absolute macOS dir where helpers are installed (default: {PLATFORM_INSTALL_DIRS['macos']}).",
        )
        parser.add_argument(
            "--install-dir-windows",
            default=PLATFORM_INSTALL_DIRS["windows"],
            help=f"Absolute Windows dir where helpers are installed (default: {PLATFORM_INSTALL_DIRS['windows']}).",
        )
        parser.add_argument(
            "--install-dir-linux",
            default=PLATFORM_INSTALL_DIRS["linux"],
            help=f"Absolute Linux dir where helpers are installed (default: {PLATFORM_INSTALL_DIRS['linux']}).",
        )
        parser.add_argument(
            "--credential-ttl-sec",
            type=int,
            default=500,
            help=(
                "credential.ttlSec: how long Claude Desktop caches the helper's token "
                "(default: 500). Baked into the bootstrap, which writes the credential block."
            ),
        )
        parser.add_argument(
            "--credential-timeout-sec",
            type=int,
            default=120,
            help=(
                "credential.timeoutSec: how long the helper may run before timing out "
                "(default: 120). Baked into the bootstrap, which writes the credential block."
            ),
        )
        parser.add_argument(
            "--allow-websearch",
            action="store_true",
            help=(
                "Keep the built-in WebSearch tool. Off by default (disabledBuiltinTools: "
                "[\"WebSearch\"]) because the gateway provides web search as an MCP service."
            ),
        )
        parser.add_argument(
            "--egress-hosts",
            default="*",
            help="Comma-separated workspace.allowedEgressHosts (default: * — allow all).",
        )
        parser.add_argument(
            "--allow-claude-ai-signin",
            action="store_true",
            help=(
                "Keep the Claude.ai sign-in option. Off by default "
                "(authentication.disableClaudeAiSignIn: true) so the app uses the gateway only."
            ),
        )
        parser.add_argument(
            "--databricks-bin",
            default="databricks",
            help="Databricks CLI referenced by the bash OTEL helper (default: databricks).",
        )
        # ---- telemetry (OpenTelemetry / OTLP) ----
        parser.add_argument(
            "--telemetry",
            choices=["auto", "on", "off"],
            default="auto",
            help=(
                "Emit the otlp block + otel-headers-helper. 'auto' (default) enables it iff the "
                "Terraform 'telemetry' output has a traces table; 'on' requires it; 'off' skips. "
                "Claude Desktop telemetry is trace-centric with a single headers set, so this "
                "wires TRACES to the traces table (metrics/logs are not per-signal routable here)."
            ),
        )
        parser.add_argument(
            "--otel-log-content",
            action="store_true",
            help=(
                "Set otlp.contentCapture to capture prompts, responses, and tool details. "
                "Privacy-sensitive; OFF by default (traces without content)."
            ),
        )

    # ---- OTEL (traces only) ------------------------------------------------
    def _otel_traces_table(self, ctx: GatewayContext, args: argparse.Namespace) -> str | None:
        """The traces UC table to wire OTEL to, or None when telemetry is off/absent.

        Claude Desktop carries a single otlp.headers set, so it cannot route
        metrics/logs/traces to different UC tables the way Claude Code does. Its
        telemetry is trace-centric (otlpTracesEnabled), so we wire TRACES only.
        """
        if args.telemetry == "off":
            return None
        tel = ctx.telemetry
        traces = (tel.tables.get("traces") if tel else None)
        if not traces:
            if args.telemetry == "on":
                raise SystemExit(
                    "--telemetry on, but the Terraform 'telemetry' output has no traces table. "
                    "Set telemetry_enabled = true (with a traces table) and apply, or use --telemetry off."
                )
            return None  # auto + not deployed
        return traces

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
        unknown = [p for p in platforms if p not in PLATFORM_INSTALL_DIRS]
        if unknown:
            raise SystemExit(
                f"--platforms has unknown value(s): {', '.join(unknown)}. Valid: {', '.join(PLATFORMS)}."
            )
        if not platforms:
            raise SystemExit("--platforms selected no platforms.")

        profile = getattr(args, "profile", "DEFAULT")
        install_dirs = {
            "macos": args.install_dir_macos,
            "windows": args.install_dir_windows,
            "linux": args.install_dir_linux,
        }

        # The MDM-owned config: policy + telemetry only. No inference block and no
        # model list — ug owns the workspace, the identity, and the models, and the
        # generated bootstrap script splices those in from ~/.ucode/state.json.
        #
        # The base is identical across platforms except otlp.headersHelper, which is
        # an absolute path stamped per OS below.
        base: dict = {
            "$schemaVersion": SCHEMA_VERSION,
            "workspace": {},
            "authentication": {},
        }
        if not args.allow_websearch:
            base["workspace"]["disabledBuiltinTools"] = ["WebSearch"]
        egress = [h.strip() for h in args.egress_hosts.split(",") if h.strip()]
        if egress:
            base["workspace"]["allowedEgressHosts"] = egress
        if not args.allow_claude_ai_signin:
            base["authentication"]["disableClaudeAiSignIn"] = True

        traces_table = self._otel_traces_table(ctx, args)
        _validate_bakeables(profile, ctx.host, traces_table)
        if traces_table:
            # Static headers route the export to the traces UC table; the sensitive
            # Authorization header comes from the headersHelper (dedicated telemetry SP,
            # down-scoped to this table). authMode "none" means the app does not add the
            # inference credential — only the helper's headers are sent.
            base["otlp"] = {
                "endpoint": f"{ctx.host}{OTEL_INGEST_PATH}",
                "protocol": "http/protobuf",
                "authMode": "none",
                "tracesEnabled": True,
                "headers": {"X-Databricks-UC-Table-Name": traces_table},
                "resourceAttributes": {"service.name": "claude-desktop"},
                # headersHelper stamped per-platform below.
            }
            if args.otel_log_content:
                base["otlp"]["contentCapture"] = [
                    "userPrompts", "assistantResponses", "toolDetails", "toolContent",
                ]
            print(f"[claude-desktop] telemetry: traces -> {traces_table} "
                  "(single headers set; metrics/logs are not per-signal routable here).",
                  file=sys.stderr)

        files: dict[str, str] = {}
        for platform in platforms:
            install_dir = install_dirs[platform]
            cfg = copy.deepcopy(base)

            # The credential helper's absolute path is not written into the MDM config
            # (there is no inference block there). It is baked into the bootstrap, which
            # writes the inference block that references it.
            cred_cmd = _platform_path(install_dir, _cred_command_filename(platform), platform)

            helper_files = _cred_helper_files(platform, install_dir, profile)
            helper_files.update(
                _bootstrap_files(
                    platform, install_dir, cred_cmd,
                    args.credential_ttl_sec, args.credential_timeout_sec,
                )
            )

            if traces_table:
                cfg["otlp"]["headersHelper"] = _platform_path(
                    install_dir, _otel_helper_filename(platform), platform
                )
                helper_files.update(
                    _otel_helper_files(
                        platform, ctx.host, profile,
                        ctx.telemetry.secret_full_name, args.databricks_bin, [traces_table],
                    )
                )

            files[f"claude-desktop/{platform}/{CONFIG_FILENAME}"] = json.dumps(cfg, indent=2) + "\n"
            for fname, content in helper_files.items():
                files[f"claude-desktop/{platform}/{fname}"] = content

        return files

    def install_notes(self, args: argparse.Namespace) -> str:
        platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORM_INSTALL_DIRS]
        install_dirs = {
            "macos": args.install_dir_macos,
            "windows": args.install_dir_windows,
            "linux": args.install_dir_linux,
        }
        lines = [
            "Per-platform bundles were written to claude-desktop/<platform>/.",
            "",
            "claude-setup.json carries POLICY + TELEMETRY only. It has no inference block and",
            "no model list: ug owns the workspace, the identity, and the models. The generated",
            "ug-bootstrap-claude-desktop.sh runs `ug configure`, reads what ug decided out of",
            "~/.ucode/state.json, and writes the complete config as claude-setup.merged.json.",
            "",
            "1. Install the helper scripts to that OS's helper directory (the absolute paths",
            "   baked into the bootstrap already point there):",
        ]
        for p in platforms:
            lines.append(f"     {p:8}: {install_dirs[p]}")
        lines += [
            "     macOS/Linux: install.sh places them (run as root, or with --target-root).",
            "     Windows: place databricks-token.cmd + .ps1 (and the OTEL pair) via Intune or",
            "     a machine-wide script (install.sh is POSIX and does not run on Windows).",
            "",
            "2. Run the bootstrap to produce the importable config (macOS/Linux):",
            "     sh ug-bootstrap-claude-desktop.sh --profile <profile>",
            "   Add --use-pat for a fully non-interactive run (CI, or a device-management",
            "   payload); it needs the PAT already present in ~/.databrickscfg. Without it, ug",
            "   runs a one-time browser OAuth login. Add --dry-run to inspect the merged JSON.",
            "   Windows has no bootstrap yet — run `ug configure --agents claude` and build the",
            "   inference + models block by hand (see the runbook).",
            "",
            "3. Import claude-setup.merged.json into Claude Desktop:",
            "     Help -> Troubleshooting -> Enable Developer Mode, then",
            "     Developer -> Configure third-party inference -> import.",
            "",
            "4. Test the connection, then export the MDM profile from the app",
            "   (.mobileconfig on macOS, .reg on Windows). The app produces the MDM artifacts;",
            "   this generator does not. Push ONLY the policy + telemetry keys to the fleet —",
            "   leave inference and models to ug on each device.",
            "",
            "The credential helper is a thin wrapper around `ucode auth-token` — ug's own",
            "token helper, and the same one Claude Code's apiKeyHelper and Codex already use.",
            "So Claude Desktop authenticates as the same identity, through the same code path,",
            "as every ug-launched agent. ug supplies the workspace, the profile, static-PAT",
            "support, token-cache lock retries, and non-interactive re-auth; the wrapper only",
            "strips ug's trailing newline and resolves the binary by absolute path (Claude",
            "Desktop starts under launchd with a minimal PATH). Set UCODE_BIN to override that",
            "path. It needs no jq and no python3.",
        ]
        if args.telemetry != "off":
            lines += [
                "",
                "Telemetry (otel-headers-helper ships in each bundle):",
                "  Claude Desktop carries a single otlp.headers set, so it routes TRACES to the",
                "  traces UC table only — it cannot split metrics/logs/traces per table the way",
                "  Claude Code does. The helper mints the dedicated telemetry SP token; the",
                "  developer must hold READ_SECRET on the telemetry UC secret. On macOS/Linux the",
                "  helper is bash; on Windows it is PowerShell behind a .cmd shim (THEORETICAL —",
                "  test before rollout).",
                "",
                "  Verify the otlp key names against the live Claude Desktop configuration",
                "  reference before a production rollout (the importable-JSON otlp shape can change).",
            ]
        return "\n".join(lines)
