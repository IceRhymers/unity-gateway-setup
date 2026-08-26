"""Claude Code managed-settings.json generator for the Databricks Unity AI Gateway.

Turns the deployed anthropic model services (from Terraform outputs) into an
opinionated, MDM-deployable managed-settings.json that routes Claude Code
through `<host>/ai-gateway/anthropic` with U2M OAuth.

Conventions follow the internal "Onboarding Coding Agents - AI Gateway" playbook.
"""

from __future__ import annotations

import argparse
import json
import sys

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

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
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

        if not args.allow_websearch:
            settings["permissions"] = {"deny": ["WebSearch"]}

        if args.required_min_version:
            settings["requiredMinimumVersion"] = args.required_min_version

        content = json.dumps(settings, indent=2) + "\n"
        return {"claude-code/managed-settings.json": content}

    def install_notes(self, args: argparse.Namespace) -> str:
        lines = [
            "Deploy managed-settings.json to the OS-specific path (push via MDM):",
            f"  macOS   : {INSTALL_PATHS['macos']}",
            f"  Linux   : {INSTALL_PATHS['linux']}",
            f"  Windows : {INSTALL_PATHS['windows']}",
            "",
            "Each developer authenticates once:",
            "  databricks auth login --host <workspace-url> --profile <profile>",
            "",
            "Verify inside Claude Code with /status:",
            "  'Anthropic base URL' -> the gateway address; 'Setting sources' -> Enterprise managed settings.",
        ]
        return "\n".join(lines)
