"""Claude Desktop third-party inference config generator for the Unity AI Gateway.

Turns the deployed anthropic model services (from Terraform outputs) into an
importable Claude Desktop configuration (schema version 2, the nested-object
form) plus the OAuth credential-helper scripts it needs.

Unlike Claude Code (which reads a managed-settings.json that MDM places on disk),
Claude Desktop reads an operator-imported config. The deploy flow is:

  1. Run this generator to emit a per-OS bundle.
  2. Import the JSON into Claude Desktop (Developer -> Configure third-party
     inference), then test the connection.
  3. Export the OS-native MDM profile (.mobileconfig / .reg) from the app.

So this generator does NOT emit the flat MDM plist/registry keys — the app
exports those. It emits the importable JSON and the helper scripts the config
references by absolute path.

Authentication goes through ug. The credential helper is a thin wrapper around
`ug auth-token`, ug's own cross-platform token helper and the same one Claude
Code's apiKeyHelper and Codex's auth command use. A developer runs `ug configure`
once, and every surface after that — the terminal agents and this desktop app —
draws its token from that single place. ug owns the token logic (static PATs,
token-cache lock contention, non-interactive re-auth); this generator owns the
inference config, the models, and the telemetry, because ug has no Claude Desktop
target and cannot discover models for it.

Claude Desktop starts under launchd (macOS) with a minimal PATH, so the
credential helper resolves ug from an absolute-path candidate list, never from
$PATH. The config's credential.command is an absolute path that must match where
the helper is installed (see install.sh / the runbook).

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
    ANTHROPIC_API_TYPE,
    LARGE_CONTEXT_FAMILIES,
    OTEL_INGEST_PATH,
    _otel_headers_helper_script,
)
from gateway import Endpoint, GatewayContext, discover_api_types

# Claude Desktop config schema version (nested-object form). See the configuration
# changelog: version 2 is the importable JSON shape used by the in-app window.
SCHEMA_VERSION = 2

# The anthropicFamilyTier values Claude Desktop recognizes for a model entry.
KNOWN_FAMILY_TIERS = ("opus", "sonnet", "haiku")
FAMILY_TIERS = ("opus", "sonnet", "haiku", "fable")

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

# The config filename the operator imports.
CONFIG_FILENAME = "claude-setup.json"

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

# --- MDM-triggered SSO bootstrap (macOS / Linux) -----------------------------
# The MDM pushes these two files. The LaunchAgent runs the script at each user
# login, inside the user's GUI session, so `ug configure` can open a browser for
# single sign-on. The script guards itself, so a browser opens only when the
# developer is not already authenticated.
SSO_BOOTSTRAP_SH = "ug-sso-bootstrap.sh"
LAUNCHAGENT_PLIST = "ug-sso-bootstrap.plist"

# Where the LaunchAgent plist belongs. /Library/LaunchAgents is machine-wide, so
# the agent loads for every user who logs in. A per-user ~/Library/LaunchAgents
# copy would need placing per account, which MDM cannot do in one push.
LAUNCHAGENT_DIR = "/Library/LaunchAgents"

# uv is a PREREQUISITE, not a payload. It installs per-user (~/.local/bin/uv) and it
# self-updates (`uv self update`), so packaging it would go stale and fight its own
# updater — the same reason ug is not packaged either. IT owns it as part of the macOS
# baseline, alongside databricks, python3, jq, and curl.
#
# The bootstrap still resolves it by absolute path, because a LaunchAgent inherits a
# minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin). These are the places uv actually lands:
# the official installer, Homebrew, and a machine-wide placement by IT.
UV_CANDIDATE_PATHS = (
    "$HOME/.local/bin/uv",
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
)

# What the bootstrap installs when ug is absent. Unpinned by default, which matches
# `ug upgrade` (it runs `uv tool install --reinstall` against the same URL) and this
# repo's treatment of ug as a self-updating tool. Pass --ug-ref to pin a fleet.
UG_GIT_URL = "git+https://github.com/databricks/ucode"

# The default reverse-DNS label for the LaunchAgent. Override with
# --launchagent-label. The plist filename follows the label in the runbook, but
# the bundle always writes LAUNCHAGENT_PLIST so install.sh can find it.
DEFAULT_LAUNCHAGENT_LABEL = "com.databricks.ug-sso-claude-desktop"


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


# A git ref is baked into the bootstrap's `uv tool install "<url>@<ref>"` argument.
# Keep it to characters git accepts in a ref, so a hand-passed value cannot inject
# shell syntax.
_UG_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _validate_ug_ref(ug_ref: str | None) -> None:
    """Reject a git ref that is unsafe to bake into the bootstrap script."""
    if ug_ref is None:
        return
    if not _UG_REF_RE.match(ug_ref) or ".." in ug_ref:
        raise SystemExit(
            f"Refusing to bake an unsafe ug git ref: {ug_ref!r}. Use letters, digits, "
            "'.', '_', '/', and '-', with no '..' sequence."
        )


def _validate_launchagent_label(label: str) -> None:
    """Reject a LaunchAgent label that is unsafe to bake into a plist or a filename."""
    if not _LABEL_RE.match(label):
        raise SystemExit(
            f"Refusing to bake an unsafe LaunchAgent label: {label!r}. Use a reverse-DNS "
            "string of letters, digits, '.', '_', and '-', starting with a letter or digit."
        )


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


def _family_tier(name: str) -> str | None:
    """Map an endpoint leaf name to its Anthropic family tier, if recognizable."""
    for fam in FAMILY_TIERS:
        if fam in name:
            return fam
    return None


# --- Credential helper: POSIX sh (macOS / Linux) ----------------------------
# Delegates to `ug auth-token` so ug is the single source of Databricks tokens
# across every surface. Prints ONLY the bearer token to stdout (no trailing
# newline), diagnostics to stderr. Resolves ug from an absolute-path list so it
# behaves identically under launchd. __HOST__ is baked at generation time to pin
# the token to the workspace inference.baseUrl points at.
_CRED_HELPER_SH_TEMPLATE = r"""#!/usr/bin/env sh
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
#
# Databricks credential helper for Claude Desktop.
#
# A THIN WRAPPER around `ug auth-token` — ug's own cross-platform token helper,
# and the same one Claude Code's apiKeyHelper and Codex's auth command use. A
# developer authenticates ONCE with `ug configure`; every surface after that —
# the terminal agents and this desktop app — draws its token from that one place.
#
# All token logic lives in ug. Do not add any here. ug handles the
# DATABRICKS_BEARER short-circuit, host-to-profile resolution, static-PAT
# profiles, token-cache lock contention (retried with jittered backoff), and
# non-interactive re-auth when a session expires.
#
# POSIX sh, so it runs under dash as well as bash. No bashisms.
#
# Output contract:
#   stdout - the raw bearer token, no trailing newline. Nothing else.
#   stderr - diagnostics from ug, plus our own errors.
#   exit 0 - success
#   exit 1 - ug not found, or `ug auth-token` failed
set -u

# Give ug a PATH it can work with. This is load-bearing, not hygiene.
#
# A launchd job gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, which holds none of the
# places developer tools install to. This script resolves ug and uv by absolute path,
# but ug then shells out to `databricks` BY BARE NAME, so PATH decides whether it
# finds it. Without this, ug reports "databricks was not found" and tries to install
# it with sudo, which cannot prompt from a launchd job.
#
# The SIP-protected system directories cannot receive a binary, so extending PATH is
# the only fix available.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

# The workspace this config routes to, baked at generation time. Passing it pins
# the token to the SAME workspace as inference.baseUrl — a developer with several
# workspaces configured in ug would otherwise get a token for whichever one ug
# selected last. ug resolves the matching CLI profile from this host.
host="__HOST__"

# Resolve the ug binary WITHOUT trusting $PATH. Claude Desktop starts under
# launchd with a minimal environment that does not inherit the user's PATH.
resolve_ug() {
  if [ -n "${UG_BIN:-}" ]; then
    if [ -x "$UG_BIN" ]; then
      printf '%s' "$UG_BIN"
      return 0
    fi
    echo "databricks-token: UG_BIN=$UG_BIN is not executable" >&2
    return 1
  fi
  for candidate in \
    "$HOME/.local/bin/ug" \
    "$HOME/.local/bin/ucode" \
    /opt/homebrew/bin/ug \
    /opt/homebrew/bin/ucode \
    /usr/local/bin/ug \
    /usr/local/bin/ucode \
    /usr/bin/ug \
    /usr/bin/ucode
  do
    if [ -x "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  echo "databricks-token: ug not found. Install ug and run 'ug configure', or set UG_BIN to its absolute path." >&2
  return 1
}

ug="$(resolve_ug)" || exit 1

# The profile is left to ug, which resolves it from the host above.
# $DATABRICKS_PROFILE overrides that for a developer with an unusual cfg.
set -- auth-token --host "$host"
if [ -n "${DATABRICKS_PROFILE:-}" ]; then
  set -- "$@" --profile "$DATABRICKS_PROFILE"
fi

# Capture stdout so the trailing newline can be stripped: ug writes the token as
# `token + "\n"`, and Claude Desktop's credential contract wants the bare token.
token="$("$ug" "$@")" || {
  echo "databricks-token: 'ug auth-token' failed for host $host." >&2
  echo "databricks-token: run 'ug configure' to authenticate this workspace." >&2
  exit 1
}

if [ -z "$token" ]; then
  echo "databricks-token: 'ug auth-token' returned an empty token" >&2
  exit 1
fi

# Bare token to stdout. No trailing newline. $(...) already stripped it.
printf '%s' "$token"
"""


# --- Credential helper: PowerShell (Windows) --------------------------------
# Also delegates to `ug auth-token`, so this port carries no auth logic of its
# own — ug is one binary that behaves the same on Windows. Still test on Windows
# before a production rollout (see the runbook). Claude Desktop points
# credential.command at the .cmd shim below, which runs this .ps1.
_CRED_HELPER_PS1_TEMPLATE = r"""# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
# databricks-token.ps1
#
# Databricks credential helper for Claude Desktop on Windows.
#
# A THIN WRAPPER around `ug auth-token`. ug ships ONE cross-platform token helper
# precisely so this file needs no auth logic: no CLI discovery, no JSON parsing,
# no login retry. That is also why this port carries far less Windows-specific
# risk than a hand-written OAuth path would.
#
# Output contract:
#   stdout - the raw bearer token, no trailing newline. Nothing else.
#   stderr - diagnostics from ug, plus our own errors.
#   exit 0 - success
#   exit 1 - ug not found, or `ug auth-token` failed

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
if (Get-Variable -Name 'PSNativeCommandUseErrorActionPreference' -Scope Global -ErrorAction SilentlyContinue) {
    $Global:PSNativeCommandUseErrorActionPreference = $false
}
# Force raw UTF-8 (no BOM) on stdout so the bare-token contract reaches Claude
# Desktop byte-identical (a UTF-16 BOM would break Authorization headers).
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

# The workspace this config routes to, baked at generation time, so the token is
# pinned to the same workspace as inference.baseUrl.
$workspaceHost = '__HOST__'

# Give ug a PATH it can work with, for the same reason the POSIX helper does: ug
# shells out to `databricks` by bare name, and a launchd-style minimal environment
# does not include the directories developer tools install to.
$env:PATH = @(
    "$env:LOCALAPPDATA\Programs\Databricks",
    "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
    "$env:USERPROFILE\.local\bin",
    "$env:ProgramFiles\Databricks",
    $env:PATH
) -join ';'

# Resolve the ug executable without relying on PATH.
function Resolve-Ug {
    if ($env:UG_BIN) {
        if (Test-Path -LiteralPath $env:UG_BIN -PathType Leaf) { return $env:UG_BIN }
        [Console]::Error.WriteLine("databricks-token: UG_BIN=$($env:UG_BIN) is not a file")
        exit 1
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\ug\ug.exe",
        "$env:LOCALAPPDATA\uv\tools\ucode\Scripts\ug.exe",
        "$env:LOCALAPPDATA\uv\tools\ucode\Scripts\ucode.exe",
        "$env:USERPROFILE\.local\bin\ug.exe",
        "$env:USERPROFILE\.local\bin\ucode.exe",
        "$env:ProgramFiles\ug\ug.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return $c }
    }
    $onPath = Get-Command ug -ErrorAction SilentlyContinue
    if (-not $onPath) { $onPath = Get-Command ucode -ErrorAction SilentlyContinue }
    if ($onPath) { return $onPath.Source }
    [Console]::Error.WriteLine("databricks-token: ug not found. Install ug and run 'ug configure', or set UG_BIN to its absolute path.")
    exit 1
}
$ug = Resolve-Ug

# The profile is left to ug, which resolves it from the host above.
$ugArgs = @('auth-token', '--host', $workspaceHost)
if ($env:DATABRICKS_PROFILE) {
    $ugArgs += @('--profile', $env:DATABRICKS_PROFILE)
}

$token = (& $ug @ugArgs | Out-String)
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine("databricks-token: 'ug auth-token' failed for host $workspaceHost.")
    [Console]::Error.WriteLine("databricks-token: run 'ug configure' to authenticate this workspace.")
    exit 1
}
# ug writes the token as `token + "\n"`; Claude Desktop wants the bare token.
$token = $token.Trim()
if (-not $token) {
    [Console]::Error.WriteLine("databricks-token: 'ug auth-token' returned an empty token")
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


# --- MDM-triggered SSO bootstrap: POSIX sh -----------------------------------
# Run by the LaunchAgent at each user login, inside the user's GUI session.
#
# The guard is the whole point. `ug configure` sets force_login whenever
# --use-pat is absent, so it runs `databricks auth login` UNCONDITIONALLY and a
# browser opens on every invocation. Without the probe below, a login-triggered
# agent would open a browser at every single login.
#
# `ug auth-token` is a safe probe: it never opens a browser, never waits for
# input, and its internal `auth login --no-browser` retry is bounded at 30s.
#
# We pass --workspaces (a bare URL), never --profiles. --profiles requires the
# named profile to exist in ~/.databrickscfg already and raises when it does not,
# which is exactly the state of a freshly imaged device.
_SSO_BOOTSTRAP_SH_TEMPLATE = r"""#!/usr/bin/env sh
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
#
# ug-sso-bootstrap.sh — bring this device's ug authentication up, once.
#
# The MDM pushes this script and a LaunchAgent that runs it at user login. It
# runs in the user's GUI session, so `ug configure` can open a browser for single
# sign-on. It uses NO personal access token.
#
# It guards itself: when the developer is already authenticated it exits silently
# and opens no browser. So it is safe to run at every login.
#
# Exit codes are advisory only. The LaunchAgent does not retry, so a failure just
# means the next login tries again. That makes the flow self-healing.
set -u

# Give ug a PATH it can work with. This is load-bearing, not hygiene.
#
# A launchd job gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, which holds none of the
# places developer tools install to. This script resolves ug and uv by absolute path,
# but ug then shells out to `databricks` BY BARE NAME, so PATH decides whether it
# finds it. Without this, ug reports "databricks was not found" and tries to install
# it with sudo, which cannot prompt from a launchd job.
#
# The SIP-protected system directories cannot receive a binary, so extending PATH is
# the only fix available.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

host="__HOST__"
log="$HOME/Library/Logs/ug-sso-bootstrap.log"

# Keep the log in the user's own Logs directory. A world-writable location such
# as /tmp would let another local account pre-create the path.
mkdir -p "$(dirname "$log")" 2>/dev/null || true

_log() {
  printf '%s ug-sso-bootstrap: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >>"$log" 2>/dev/null
}

# Resolve ug without trusting $PATH. A LaunchAgent starts with a minimal
# environment that does not inherit the user's interactive PATH.
resolve_ug() {
  if [ -n "${UG_BIN:-}" ] && [ -x "${UG_BIN:-}" ]; then
    printf '%s' "$UG_BIN"
    return 0
  fi
  for candidate in \
    "$HOME/.local/bin/ug" \
    "$HOME/.local/bin/ucode" \
    /opt/homebrew/bin/ug \
    /opt/homebrew/bin/ucode \
    /usr/local/bin/ug \
    /usr/local/bin/ucode \
    /usr/bin/ug \
    /usr/bin/ucode
  do
    if [ -x "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# Resolve uv without trusting $PATH either: a LaunchAgent inherits a minimal one.
# uv is a prerequisite that IT owns, so this only looks for it. It never installs it.
# The order follows where uv actually lands: the official installer first, then
# Homebrew, then a machine-wide placement.
resolve_uv() {
  for candidate in \
    "${UV_BIN:-}" \
    "$HOME/.local/bin/uv" \
    /opt/homebrew/bin/uv \
    /usr/local/bin/uv
  do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# Install ug for THIS user. The LaunchAgent runs in the user's session, so
# `uv tool install` lands in their home rather than root's. That is the whole reason
# the install is deferred to login instead of running in a package script.
install_ug() {
  _iu_uv="$(resolve_uv)" || {
    _log "ug absent, and no uv to install it with. uv is a prerequisite: IT deploys it"
    _log "  as part of the macOS baseline. Install uv, or set UV_BIN, or install ug."
    return 1
  }
  _log "ug absent; installing with ${_iu_uv} (this needs network access)"
  if "${_iu_uv}" tool install "__UG_REQUIREMENT__" >>"$log" 2>&1; then
    _log "ug install finished"
    return 0
  fi
  _log "ug install failed. The next login will try again."
  return 1
}

ug="$(resolve_ug || true)"
if [ -z "$ug" ]; then
  # ug is absent. Install it, then resolve again. A failure is not fatal: the next
  # login retries, so a transient network outage costs one login.
  install_ug || exit 0
  ug="$(resolve_ug)" || {
    _log "ug still not found after install. The next login will try again."
    exit 0
  }
fi

# --- The guard ---------------------------------------------------------------
# Already authenticated? Then exit without opening a browser.
if "$ug" auth-token --host "$host" >/dev/null 2>&1; then
  _log "already authenticated for $host; no browser needed"
  exit 0
fi

_log "not authenticated for $host; starting ug configure (a browser will open)"

# --workspaces takes a bare URL and sets the workspace up from nothing, so this
# works on a freshly imaged device with no ~/.databrickscfg. ug waits up to 300
# seconds for the browser login.
"$ug" configure \
  --workspaces "$host" \
  --agents claude \
  --skip-validate \
  --skip-upgrade \
  --verbose low >>"$log" 2>&1

if "$ug" auth-token --host "$host" >/dev/null 2>&1; then
  _log "authentication complete for $host"
  exit 0
fi

_log "authentication did not complete; the next login will try again"
exit 0
"""


# --- MDM-triggered SSO bootstrap: LaunchAgent plist --------------------------
# LimitLoadToSessionType=Aqua confines the agent to a GUI login session, so it
# never fires for an ssh or background session where a browser cannot open.
# RunAtLoad fires it once per login; there is no KeepAlive, so a dismissed login
# is retried at the next login rather than immediately.
#
# The script handles its own logging, so no StandardOutPath is set here. A
# launchd log path cannot expand $HOME, and a fixed world-writable path would be
# a symlink risk.
_LAUNCHAGENT_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Generated by unity-gateway-setup (agent_setups). Do not edit by hand. -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>__LABEL__</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>__SCRIPT_PATH__</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>
</dict>
</plist>
"""

# A LaunchAgent label is a reverse-DNS string. Keep it to characters launchd and
# the filesystem both accept, so the plist filename can follow the label.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _ug_requirement(ug_ref: str | None) -> str:
    """The `uv tool install` argument for ug, optionally pinned to a git ref."""
    return f"{UG_GIT_URL}@{ug_ref}" if ug_ref else UG_GIT_URL


def _sso_bootstrap_files(install_dir: str, host: str, label: str,
                         ug_ref: str | None = None) -> dict[str, str]:
    """Return the SSO bootstrap script and its LaunchAgent plist.

    macOS and Linux only. The plist is a macOS artifact; a Linux fleet would use a
    systemd user unit instead, so the caller decides whether to include it.
    """
    script_path = f"{install_dir}/{SSO_BOOTSTRAP_SH}"
    return {
        SSO_BOOTSTRAP_SH: (
            _SSO_BOOTSTRAP_SH_TEMPLATE
            .replace("__HOST__", host)
            .replace("__UG_REQUIREMENT__", _ug_requirement(ug_ref))
        ),
        LAUNCHAGENT_PLIST: (
            _LAUNCHAGENT_PLIST_TEMPLATE
            .replace("__LABEL__", label)
            .replace("__SCRIPT_PATH__", script_path)
        ),
    }


def _cred_helper_files(platform: str, install_dir: str, host: str) -> dict[str, str]:
    """Return the credential-helper file(s) for a platform, plus the command path.

    macOS/Linux: one databricks-token.sh (the credential.command target).
    Windows: databricks-token.ps1 (logic) + databricks-token.cmd (the command target).

    The workspace host is baked in, not the profile: `ug auth-token --host` lets ug
    resolve the matching CLI profile itself, which keeps the token pinned to the
    same workspace as inference.baseUrl even when ug knows several.
    """
    if platform == "windows":
        return {
            CRED_HELPER_PS1: _CRED_HELPER_PS1_TEMPLATE.replace("__HOST__", host),
            CRED_HELPER_CMD: _cmd_shim(CRED_HELPER_PS1, "credential helper"),
        }
    return {CRED_HELPER_SH: _CRED_HELPER_SH_TEMPLATE.replace("__HOST__", host)}


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
            choices=list(FAMILY_TIERS),
            help="Family whose default model is listed first (Claude Desktop's default). Default: sonnet.",
        )
        parser.add_argument(
            "--small-context",
            action="store_true",
            help=(
                "Do not request 1M context. By default the opus and sonnet families are "
                "listed with supports1m/prefer1m true; this flag lists them at native context."
            ),
        )
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
            help="credential.ttlSec: how long Claude Desktop caches the helper's token (default: 500).",
        )
        parser.add_argument(
            "--credential-timeout-sec",
            type=int,
            default=120,
            help="credential.timeoutSec: how long the helper may run before timing out (default: 120).",
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
            "--launchagent-label",
            default=DEFAULT_LAUNCHAGENT_LABEL,
            help=(
                "Reverse-DNS Label for the SSO-bootstrap LaunchAgent (default: "
                f"{DEFAULT_LAUNCHAGENT_LABEL}). Change it to your own organisation's prefix."
            ),
        )
        parser.add_argument(
            "--ug-ref",
            default=None,
            help=(
                "Pin the ug version the SSO bootstrap installs, as a git ref (tag, branch, "
                "or commit). Unpinned by default, which matches `ug upgrade`. Pin it when a "
                "fleet needs a determinate ug version."
            ),
        )
        parser.add_argument(
            "--no-sso-bootstrap",
            action="store_true",
            help=(
                "Do not emit ug-sso-bootstrap.sh or its LaunchAgent. By default the macOS "
                "bundle carries both, so the MDM can trigger the one-time browser SSO "
                "login without a developer configuring anything. Pass this when your MDM "
                "already runs `ug configure` some other way."
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

    # ---- model discovery (shared shape with claude-code) -------------------
    def _select_anthropic_capable(self, ctx: GatewayContext, args: argparse.Namespace) -> list[Endpoint]:
        candidates = ctx.endpoints
        if not candidates:
            raise SystemExit("No endpoints found in the Terraform outputs.")

        if args.skip_api_discovery:
            eps = [e for e in candidates if e.schema == args.fallback_schema]
            print(f"[claude-desktop] discovery skipped; using schema '{args.fallback_schema}' "
                  f"({len(eps)} endpoints).", file=sys.stderr)
        else:
            print(f"[claude-desktop] discovering supported_api_types for {len(candidates)} endpoints...",
                  file=sys.stderr)
            api_types = discover_api_types([e.full_name for e in candidates], args.profile)
            eps = [e for e in candidates if ANTHROPIC_API_TYPE in api_types.get(e.full_name, [])]
            skipped = sorted({e.schema for e in candidates} - {e.schema for e in eps})
            print(f"[claude-desktop] {len(eps)}/{len(candidates)} endpoints expose {ANTHROPIC_API_TYPE}"
                  + (f"; schemas without it: {', '.join(skipped)}" if skipped else ""),
                  file=sys.stderr)

        if not eps:
            raise SystemExit(
                f"No endpoints expose the Anthropic API ({ANTHROPIC_API_TYPE}) in this workspace, "
                "so Claude Desktop cannot route through this gateway."
            )

        # Claude Desktop rejects any model whose name does not contain "claude". So we
        # keep only the Claude endpoints (the app's own anthropic surface) and drop any
        # other anthropic-API-capable model. Filter on the leaf name (the full_name
        # carries it too).
        claude_eps = [e for e in eps if "claude" in e.name.lower()]
        dropped = [e.name for e in eps if e not in claude_eps]
        if dropped:
            print(f"[claude-desktop] dropping {len(dropped)} non-Claude model(s) "
                  f"(Claude Desktop only accepts names containing 'claude'): {', '.join(sorted(dropped))}.",
                  file=sys.stderr)
        if not claude_eps:
            raise SystemExit(
                "No Claude models are available on this gateway. Claude Desktop only accepts "
                "models whose name contains 'claude', so it cannot be configured here."
            )
        return claude_eps

    def _model_entries(self, eps: list[Endpoint], args: argparse.Namespace) -> list[dict]:
        """Build models.list, default-tier endpoint first (Claude Desktop's default).

        Each entry uses the three-level UC full_name. supports1m/prefer1m follow the
        opus/sonnet families (unless --small-context). The versionless alias of a
        family is marked isFamilyDefault. anthropicFamilyTier is set only for the
        tiers Claude Desktop recognizes.
        """
        large = not args.small_context

        def entry(ep: Endpoint) -> dict:
            tier = _family_tier(ep.name)
            one_m = large and tier in LARGE_CONTEXT_FAMILIES
            item: dict = {
                "name": ep.full_name,
                "supports1m": one_m,
                "prefer1m": one_m,
                "isFamilyDefault": ep.is_alias,
            }
            if tier in KNOWN_FAMILY_TIERS:
                item["anthropicFamilyTier"] = tier
            return item

        # Order: the default-tier's alias (or first default-tier endpoint) first, so
        # Claude Desktop's "first entry is default" picks the intended model.
        def sort_key(ep: Endpoint) -> tuple:
            tier = _family_tier(ep.name)
            return (
                tier != args.default_tier,   # default tier first
                not ep.is_alias,             # alias before version pins
                ep.schema,
                ep.name,
            )

        return [entry(e) for e in sorted(eps, key=sort_key)]

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

        eps = self._select_anthropic_capable(ctx, args)
        model_list = self._model_entries(eps, args)

        # The base config is identical across platforms except the absolute helper
        # paths (credential.command, otlp.headersHelper), which are stamped per OS.
        base: dict = {
            "$schemaVersion": SCHEMA_VERSION,
            "inference": {
                "provider": "gateway",
                "baseUrl": f"{ctx.host}/ai-gateway/anthropic",
                "credential": {
                    "kind": "helper-script",
                    # command stamped per-platform below.
                    "ttlSec": args.credential_ttl_sec,
                    "timeoutSec": args.credential_timeout_sec,
                },
            },
            "chatSurface": {"enabled": True},
            "extensions": {"enabled": True},
            "models": {"discoveryEnabled": False, "list": model_list},
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
        if not args.no_sso_bootstrap:
            _validate_launchagent_label(args.launchagent_label)
            _validate_ug_ref(getattr(args, "ug_ref", None))
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

            cred_cmd = _platform_path(install_dir, _cred_command_filename(platform), platform)
            cfg["inference"]["credential"]["command"] = cred_cmd

            helper_files = _cred_helper_files(platform, install_dir, ctx.host)

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

            # The MDM-triggered SSO bootstrap. The script is POSIX sh, so macOS and
            # Linux both take it. The LaunchAgent is a macOS artifact; a Linux fleet
            # needs a systemd user unit, and Windows a scheduled task. Neither is
            # generated yet, so only macOS gets a trigger.
            if not args.no_sso_bootstrap and platform in ("macos", "linux"):
                sso = _sso_bootstrap_files(
                    install_dir, ctx.host, args.launchagent_label,
                    getattr(args, "ug_ref", None),
                )
                if platform == "linux":
                    sso.pop(LAUNCHAGENT_PLIST, None)
                helper_files.update(sso)

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
            "Per-platform bundles were written to claude-desktop/<platform>/. Claude Desktop",
            "reads an operator-imported config, so the deploy flow is import-then-export:",
            "",
            "1. Install the helper scripts from each bundle to that OS's helper directory",
            "   (the absolute paths inside claude-setup.json already point there):",
        ]
        for p in platforms:
            lines.append(f"     {p:8}: {install_dirs[p]}")
        lines += [
            "     macOS/Linux: install.sh places them (run as root, or with --target-root).",
            "     Windows: place databricks-token.cmd + .ps1 (and the OTEL pair) via Intune or",
            "     a machine-wide script (install.sh is POSIX and does not run on Windows).",
            "",
            "2. Import the JSON into Claude Desktop:",
            "     Help -> Troubleshooting -> Enable Developer Mode, then",
            "     Developer -> Configure third-party inference -> import claude-setup.json.",
            "",
            "3. Test the connection, then export the MDM profile from the app",
            "   (.mobileconfig on macOS, .reg on Windows). The app produces the MDM artifacts;",
            "   this generator does not.",
            "",
            "Each developer authenticates once, through ug (browser OAuth, cannot be",
            "pushed by MDM):",
            "  ug configure --profiles <profile>",
            "",
            "That single step serves every surface. The credential helper delegates to",
            "`ug auth-token`, the same helper Claude Code's apiKeyHelper and Codex use, so",
            "the terminal agents and this desktop app draw their token from one place.",
            "ug must be installed; the helper resolves it by absolute path, not $PATH,",
            "because Claude Desktop starts under launchd. Set UG_BIN to override that path.",
            "The baked --host pins the token to this workspace, so a developer with several",
            "workspaces in ug still gets a token for the one this config routes to.",
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
