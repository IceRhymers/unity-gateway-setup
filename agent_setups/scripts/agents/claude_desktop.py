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

Claude Desktop starts under launchd (macOS) with a minimal PATH, so the
credential helper resolves the Databricks CLI from an absolute-path candidate
list, never from $PATH. The config's credential.command is an absolute path that
must match where the helper is installed (see install.sh / the runbook).

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


def _family_tier(name: str) -> str | None:
    """Map an endpoint leaf name to its Anthropic family tier, if recognizable."""
    for fam in FAMILY_TIERS:
        if fam in name:
            return fam
    return None


# --- Credential helper: bash (macOS / Linux) --------------------------------
# The proven pure-bash helper. Prints ONLY the access token to stdout (no trailing
# newline), diagnostics to stderr. Resolves the CLI from an absolute-path list so
# it behaves identically under launchd. __PROFILE__ is baked at generation time and
# is env-overridable (DATABRICKS_PROFILE) so one script serves a fleet.
_CRED_HELPER_SH_TEMPLATE = r"""#!/usr/bin/env bash
# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
#
# Databricks OAuth credential helper (pure bash) for Claude Desktop.
#
# Fetches a fresh Databricks OAuth access token for the configured profile and
# prints it — and only it — to stdout. If the profile is not authenticated, the
# script runs `databricks auth login` interactively (browser SSO), routes its
# output to stderr, then retries the token fetch.
#
# Output contract:
#   stdout — the raw access_token, no trailing newline. Nothing else.
#   stderr — diagnostic messages, login subprocess output, errors.
#   exit 0 — success
#   exit 1 — token fetch / login failed
set -u
set -o pipefail

# Profile is baked at generation time; override with DATABRICKS_PROFILE.
profile="${DATABRICKS_PROFILE:-__PROFILE__}"

# Resolve an absolute path to the Databricks CLI. Honors $DATABRICKS_CLI first,
# then walks common install locations. We deliberately avoid $PATH so this script
# behaves identically under Claude Desktop, launchd, systemd, and other minimal-
# environment parents that do not inherit the user's interactive PATH.
resolve_cli() {
  if [ -n "${DATABRICKS_CLI:-}" ]; then
    if [ -x "$DATABRICKS_CLI" ]; then
      printf '%s' "$DATABRICKS_CLI"
      return 0
    fi
    echo "databricks-token: DATABRICKS_CLI=$DATABRICKS_CLI is not executable" >&2
    return 1
  fi
  for candidate in \
    /opt/homebrew/bin/databricks \
    /usr/local/bin/databricks \
    /usr/bin/databricks \
    "$HOME/.local/bin/databricks" \
    "$HOME/bin/databricks"
  do
    if [ -x "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  echo "databricks-token: databricks CLI not found. Set DATABRICKS_CLI to its absolute path." >&2
  return 1
}

cli="$(resolve_cli)" || exit 1

# Parse "access_token":"..." out of the JSON returned by `databricks auth token`.
# Pure sed — no jq dependency.
extract_access_token() {
  sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

fetch_token() {
  "$cli" auth token --profile "$profile" 2>/dev/null
}

response="$(fetch_token || true)"
token="$(printf '%s' "$response" | extract_access_token)"

if [ -z "$token" ]; then
  echo "databricks-token: not authenticated for profile '$profile', opening browser login..." >&2
  if ! "$cli" auth login --profile "$profile" >&2; then
    echo "databricks-token: databricks auth login failed for profile '$profile'" >&2
    exit 1
  fi
  response="$(fetch_token || true)"
  token="$(printf '%s' "$response" | extract_access_token)"
fi

if [ -z "$token" ]; then
  echo "databricks-token: still no access_token for profile '$profile' after login attempt" >&2
  exit 1
fi

# Bare token to stdout. No trailing newline.
printf '%s' "$token"
"""


# --- Credential helper: PowerShell (Windows) --------------------------------
# THEORETICAL: authored from the macOS bash helper. Test on Windows before a
# production rollout (see the runbook). Claude Desktop points credential.command
# at the .cmd shim below, which runs this .ps1.
_CRED_HELPER_PS1_TEMPLATE = r"""# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.
# databricks-token.ps1
#
# Databricks OAuth credential helper for Windows (pure PowerShell), for Claude
# Desktop. Fetches a fresh Databricks OAuth access token for the baked profile
# and prints it — and only it — to stdout. If the profile is not authenticated,
# it runs `databricks auth login` interactively, routes output to stderr, then
# retries.
#
# THEORETICAL: test on Windows before distribution.
#
# Output contract:
#   stdout  - the raw access_token, no trailing newline. Nothing else.
#   stderr  - diagnostic messages, login subprocess output, errors.
#   exit 0  - success
#   exit 1  - token fetch / login failed

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
if (Get-Variable -Name 'PSNativeCommandUseErrorActionPreference' -Scope Global -ErrorAction SilentlyContinue) {
    $Global:PSNativeCommandUseErrorActionPreference = $false
}
# Force raw UTF-8 (no BOM) on stdout so the bare-token contract reaches Claude
# Desktop byte-identical (a UTF-16 BOM would break Authorization headers).
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

# Profile is baked at generation time; override with $env:DATABRICKS_PROFILE.
$profile_name = if ($env:DATABRICKS_PROFILE) { $env:DATABRICKS_PROFILE } else { '__PROFILE__' }

# Resolve an absolute path to the Databricks CLI. Honors $env:DATABRICKS_CLI,
# then walks common install locations. We avoid PATH so this behaves identically
# when launched by Claude Desktop, scheduled tasks, or any minimal-environment parent.
function Resolve-DatabricksCli {
    if ($env:DATABRICKS_CLI) {
        if (Test-Path -LiteralPath $env:DATABRICKS_CLI -PathType Leaf) {
            return $env:DATABRICKS_CLI
        }
        [Console]::Error.WriteLine("databricks-token: DATABRICKS_CLI=$($env:DATABRICKS_CLI) is not a file")
        exit 1
    }
    $candidates = @(
        "$env:ProgramFiles\Databricks\databricks.exe",
        "$env:LOCALAPPDATA\Programs\Databricks\databricks.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\databricks.exe",
        "$env:USERPROFILE\.local\bin\databricks.exe",
        "$env:USERPROFILE\bin\databricks.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) {
            return $c
        }
    }
    [Console]::Error.WriteLine("databricks-token: databricks CLI not found. Set DATABRICKS_CLI to its absolute path.")
    exit 1
}

$cli = Resolve-DatabricksCli

function Get-TokenJson {
    param([string]$Profile)
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        $out = & $cli auth token --profile $Profile 2>$tmpErr
        if ($LASTEXITCODE -eq 0) { return ($out | Out-String) }
        return $null
    } finally {
        Remove-Item -LiteralPath $tmpErr -Force -ErrorAction SilentlyContinue
    }
}

function Get-AccessToken {
    param([string]$Json)
    if (-not $Json) { return $null }
    $m = [regex]::Match($Json, '"access_token"\s*:\s*"([^"]+)"')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

$json = Get-TokenJson -Profile $profile_name
$token = Get-AccessToken -Json $json

if (-not $token) {
    [Console]::Error.WriteLine("databricks-token: not authenticated for profile '$profile_name', opening browser login...")
    & $cli auth login --profile $profile_name *>&2
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("databricks-token: databricks auth login failed for profile '$profile_name'")
        exit 1
    }
    $json = Get-TokenJson -Profile $profile_name
    $token = Get-AccessToken -Json $json
}

if (-not $token) {
    [Console]::Error.WriteLine("databricks-token: still no access_token for profile '$profile_name' after login attempt")
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

            helper_files = _cred_helper_files(platform, install_dir, profile)

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
            "Each developer authenticates once (browser OAuth, cannot be pushed by MDM):",
            "  databricks auth login --host <workspace-url> --profile <profile>",
            "",
            "The credential helper needs the Databricks CLI installed (it resolves the CLI by",
            "absolute path, not $PATH, because Claude Desktop starts under launchd).",
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
