"""opencode config generator for the Databricks Unity AI Gateway.

Turns the deployed model services (from Terraform outputs) into a single
`opencode.json` that routes opencode through `<host>/ai-gateway/mlflow/v1`
(the MLflow serving route — the real model-inference surface) with a
launch-minted bearer token.

Why the OpenAI-compatible provider
-----------------------------------
opencode is built on the Vercel AI SDK. It reaches a custom provider through an
npm package; the `@ai-sdk/openai-compatible` package POSTs to
`<baseURL>/chat/completions`. We point `baseURL` at `/ai-gateway/mlflow/v1`, so
opencode lands on the gateway's `mlflow/v1/chat/completions` route. This mirrors
Codex's `/ai-gateway/mlflow/v1` + `/responses`. The broad
`mlflow/v1/chat/completions` surface exposes GPT, Gemini, Claude, and the open
models, so the default `--api-type` filter is that surface. Narrow to
`openai/v1/chat/completions` for OpenAI-native only.

Auth — a launch-minted bearer
------------------------------
opencode's `apiKey` uses opencode's `{env:VAR}` substitution, so the config
reads a bearer from an environment variable (default `$DATABRICKS_BEARER`). The
opencode config has no command/helper hook, unlike Claude Code's `apiKeyHelper`
or Codex's inline auth command, so it cannot mint or refresh the token itself.
The launcher must export a fresh token before it starts opencode. A U2M token
lives about one hour, so a long session needs a re-mint. Mint one with:
`databricks auth token --host <host> --profile <profile> --force-refresh`.

Managed vs. user deployment
---------------------------
opencode reads managed config LAST, and managed config overrides user config
(verified in opencode source packages/opencode/src/config/managed.ts +
config.ts). Two managed layers exist:

  - The per-OS managed config DIR: `/etc/opencode/opencode.json` (Linux),
    `/Library/Application Support/opencode/opencode.json` (macOS),
    `%ProgramData%\\opencode\\opencode.json` (Windows).
  - macOS Managed Preferences: an MDM `.mobileconfig` for the plist domain
    `ai.opencode.managed`, delivered to
    `/Library/Managed Preferences/ai.opencode.managed.plist`. This layer
    overrides everything — the true hard-lock on macOS.

Default (managed) mode emits both: the OS-independent `opencode.json` for the
managed dir, plus a macOS Configuration Profile (`.mobileconfig`) that carries
the same config keys for the hard-lock layer. Non-macOS fleets rely on the
managed dir file only. `--user-config` instead emits a single per-user
`opencode.json` (for `~/.config/opencode/opencode.json`) with no enforcement —
useful for laptops without root.

This is a CONFIG-ONLY baseline: no plugin, no client telemetry, and no hooks.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import sys
import uuid

from agents.base import AgentGenerator
from gateway import Endpoint, GatewayContext, discover_api_types

# opencode is built on the Vercel AI SDK. The OpenAI-compatible provider POSTs to
# <baseURL>/chat/completions, so we route through the MLflow serving surface
# (mlflow/v1/chat/completions is the broad chat-completions surface — a superset of
# openai/v1/chat/completions) and filter endpoints on it by default.
DEFAULT_API_TYPE = "mlflow/v1/chat/completions"
NARROW_API_TYPE = "openai/v1/chat/completions"

# Gateway route opencode posts to. The MLflow serving route is the real inference
# surface; the SDK appends /chat/completions, so <baseURL>/chat/completions =
# mlflow/v1/chat/completions.
DEFAULT_GATEWAY_PATH = "/ai-gateway/mlflow/v1"

# The npm package the provider loads. @ai-sdk/openai-compatible POSTs to
# <baseURL>/chat/completions.
PROVIDER_NPM = "@ai-sdk/openai-compatible"
PROVIDER_DISPLAY_NAME = "Databricks Unity AI Gateway"

# Preferred default model, by endpoint leaf name (opencode's OpenAI-compatible
# provider is GPT-oriented). First match wins; falls back to the first discovered
# endpoint if none are present.
DEFAULT_MODEL_PREFERENCES = ["gpt", "gpt-sol", "gpt-5-6-sol", "gpt-5-5", "gpt-5-6-luna"]

# Environment variable the config reads the bearer token from (opencode {env:VAR}).
DEFAULT_API_KEY_ENV = "DATABRICKS_BEARER"

# The opencode config JSON schema URL.
CONFIG_SCHEMA_URL = "https://opencode.ai/config.json"

# --- macOS Managed Preferences (the hard-lock layer) ------------------------
# The MDM Configuration Profile carries the config keys under the plist domain
# ai.opencode.managed. opencode reads this from
# /Library/Managed Preferences/ai.opencode.managed.plist, and it overrides
# everything (managed dir file included).
MOBILECONFIG_FILENAME = "ai.opencode.managed.mobileconfig"
MANAGED_PREF_DOMAIN = "ai.opencode.managed"
# Stable profile identifier so a redeploy replaces the same profile in place.
PROFILE_IDENTIFIER = "ai.opencode.managed.profile"

# Where opencode reads its per-user config (documentation only; nothing is written
# there by the generator).
USER_CONFIG_PATH = "~/.config/opencode/opencode.json"


def _mobileconfig(config: dict, provider: str) -> str:
    """Render a macOS Configuration Profile (.mobileconfig) for the managed domain.

    The profile carries a single Custom Settings payload whose PayloadType is the
    preference domain (ai.opencode.managed), with the opencode config keys ($schema,
    provider, model, enabled_providers) as plist values (nested dicts are valid
    plist). This is the macOS hard-lock layer — an MDM (Jamf-style "Application &
    Custom Settings" / Custom Settings payload) delivers it to
    /Library/Managed Preferences/ai.opencode.managed.plist. The PayloadIdentifier is
    stable so a redeploy replaces the same profile; each PayloadUUID is generated.
    Non-macOS fleets rely on the managed dir opencode.json only.
    """
    payload = {
        "PayloadType": MANAGED_PREF_DOMAIN,
        "PayloadIdentifier": MANAGED_PREF_DOMAIN,
        "PayloadUUID": str(uuid.uuid4()),
        "PayloadVersion": 1,
        "PayloadEnabled": True,
        "PayloadDisplayName": "opencode managed configuration",
    }
    # Carry the same config keys the managed dir opencode.json uses.
    payload.update(config)

    profile = {
        "PayloadContent": [payload],
        "PayloadType": "Configuration",
        "PayloadIdentifier": PROFILE_IDENTIFIER,
        "PayloadUUID": str(uuid.uuid4()),
        "PayloadVersion": 1,
        "PayloadScope": "System",
        "PayloadDisplayName": "unity-gateway-setup opencode managed config",
        "PayloadDescription": (
            f"Locks opencode to the '{provider}' Databricks Unity AI Gateway provider "
            "(overrides user config). Generated by unity-gateway-setup."
        ),
        "PayloadOrganization": "unity-gateway-setup",
    }
    return plistlib.dumps(profile, sort_keys=False).decode("utf-8")


class OpenCodeGenerator(AgentGenerator):
    name = "opencode"
    help = "Generate an opencode.json that routes through the Unity AI Gateway."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--api-type",
            default=DEFAULT_API_TYPE,
            help=(
                "supported_api_types value an endpoint must expose to be included. "
                f"Default '{DEFAULT_API_TYPE}' (the broad chat-completions surface served "
                "by the MLflow route — covers GPT, Gemini, Claude, and open models). Narrow "
                f"to '{NARROW_API_TYPE}' for OpenAI-native chat completions only."
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
            help="Schema assumed chat-completions-capable when discovery is skipped (default: openai).",
        )
        parser.add_argument(
            "--default-model",
            default=None,
            help=(
                "Model opencode starts on: an endpoint leaf name (e.g. 'gpt', 'gpt-5-6-sol') "
                "or a full three-level UC name. Default: the flagship 'gpt' alias if present, "
                "else the first discovered endpoint."
            ),
        )
        parser.add_argument(
            "--provider-name",
            default="databricks",
            help="Provider id: the key under `provider`, the `model` prefix, and the `enabled_providers` entry (default: databricks).",
        )
        parser.add_argument(
            "--gateway-path",
            default=DEFAULT_GATEWAY_PATH,
            help=(
                "Gateway route base appended to the host for baseURL; the SDK appends "
                f"/chat/completions (default: {DEFAULT_GATEWAY_PATH}). Override to route "
                "elsewhere."
            ),
        )
        parser.add_argument(
            "--api-key-env",
            default=DEFAULT_API_KEY_ENV,
            help=(
                "Environment variable the config reads the bearer token from, via opencode's "
                f"{{env:VAR}} substitution (default: {DEFAULT_API_KEY_ENV}). The launcher must "
                "export a fresh Databricks OAuth token into it (opencode has no auth helper)."
            ),
        )
        parser.add_argument(
            "--databricks-bin",
            default="databricks",
            help="Path to the databricks CLI used for api-type discovery (default: databricks; use an absolute path for minimal-PATH contexts).",
        )
        # ---- deployment model ----
        parser.add_argument(
            "--user-config",
            action="store_true",
            help=(
                "Emit a single per-user, non-enforced opencode.json (for "
                f"{USER_CONFIG_PATH}) instead of the default managed bundle. Default (managed) "
                "emits the OS-independent opencode.json for the per-OS managed config dir plus "
                "a macOS Configuration Profile (.mobileconfig) for the hard-lock managed "
                "preferences layer."
            ),
        )

    def _select(self, ctx: GatewayContext, args: argparse.Namespace) -> list[Endpoint]:
        """Endpoints opencode can use: those exposing the chosen chat-completions api type."""
        candidates = ctx.endpoints
        if not candidates:
            raise SystemExit("No endpoints found in the Terraform outputs.")

        if args.skip_api_discovery:
            eps = [e for e in candidates if e.schema == args.fallback_schema]
            print(f"[opencode] discovery skipped; using schema '{args.fallback_schema}' "
                  f"({len(eps)} endpoints).", file=sys.stderr)
        else:
            print(f"[opencode] discovering supported_api_types for {len(candidates)} endpoints...",
                  file=sys.stderr)
            api_types = discover_api_types([e.full_name for e in candidates], args.profile,
                                           databricks_bin=args.databricks_bin)
            eps = [e for e in candidates if args.api_type in api_types.get(e.full_name, [])]
            skipped = sorted({e.schema for e in candidates} - {e.schema for e in eps})
            print(f"[opencode] {len(eps)}/{len(candidates)} endpoints expose {args.api_type}"
                  + (f"; schemas without it: {', '.join(skipped)}" if skipped else ""),
                  file=sys.stderr)

        if not eps:
            raise SystemExit(
                f"No endpoints expose '{args.api_type}' in this workspace, so opencode cannot "
                "route through this gateway. Try --api-type mlflow/v1/chat/completions, or "
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
                f"--default-model '{requested}' is not among the {len(eps)} chat-completions-capable "
                f"endpoints. Available leaves: {', '.join(sorted(by_name))}."
            )
        for pref in DEFAULT_MODEL_PREFERENCES:
            if pref in by_name:
                return by_name[pref]
        return sorted(eps, key=lambda e: (e.schema, e.name))[0]

    def _build_config(self, provider: str, base_url: str, api_key_env: str,
                      models: dict[str, dict], default_full_name: str) -> dict:
        """The opencode config object shared by managed and user mode.

        Model IDs (the `models` map keys and the `model` value) are the endpoint FULL
        UC names (catalog.schema.endpoint) — the same string Codex uses. `model` is
        prefixed with the provider id. `enabled_providers` locks opencode to this
        provider only.
        """
        return {
            "$schema": CONFIG_SCHEMA_URL,
            "provider": {
                provider: {
                    "npm": PROVIDER_NPM,
                    "name": PROVIDER_DISPLAY_NAME,
                    "options": {
                        "baseURL": base_url,
                        "apiKey": f"{{env:{api_key_env}}}",
                    },
                    "models": models,
                }
            },
            "model": f"{provider}/{default_full_name}",
            "enabled_providers": [provider],
        }

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        eps = self._select(ctx, args)
        default_ep = self._resolve_default(eps, args.default_model)
        provider = args.provider_name
        base_url = f"{ctx.host}{args.gateway_path}"
        managed = not args.user_config

        # Switchable models (aliases first, then version pins), keyed by full UC name.
        catalog = sorted(eps, key=lambda e: (not e.is_alias, e.schema, e.name))
        models = {e.full_name: {"name": e.name} for e in catalog}

        config = self._build_config(provider, base_url, args.api_key_env, models, default_ep.full_name)
        config_json = json.dumps(config, indent=2) + "\n"

        # Record what was actually emitted so install_notes reflects the real bundle.
        self._managed = managed

        if managed:
            return {
                "opencode/opencode.json": config_json,
                f"opencode/{MOBILECONFIG_FILENAME}": _mobileconfig(config, provider),
            }
        return {"opencode/opencode.json": config_json}

    def install_notes(self, args: argparse.Namespace) -> str:
        provider = args.provider_name
        api_key_env = args.api_key_env
        if args.user_config:
            return "\n".join([
                "USER-CONFIG mode (--user-config): per-user, non-enforced. Deploy per developer:",
                f"  Copy opencode.json -> {USER_CONFIG_PATH}",
                "",
                "opencode has no auth helper, so the launcher must export a fresh bearer token",
                f"into ${api_key_env} before it starts opencode (opencode reads it via {{env:{api_key_env}}}).",
                "A U2M token lives about one hour, so a long session needs a re-mint. Mint one with:",
                "  export " + api_key_env + "=\"$(databricks auth token --host <workspace-url> "
                "--profile <profile> --force-refresh | python3 -c 'import json,sys; "
                "print(json.load(sys.stdin)[\"access_token\"])')\"",
                "",
                "Each developer authenticates once:",
                "  databricks auth login --host <workspace-url> --profile <profile>",
            ])

        # Managed mode (default).
        return "\n".join([
            "MANAGED mode (default): enforced. Two files were written to codex-style bundle opencode/:",
            "  - opencode.json      : the OS-independent managed config (overrides user config).",
            f"  - {MOBILECONFIG_FILENAME} : a macOS Configuration Profile (the hard-lock layer).",
            "",
            "opencode reads managed config LAST and it overrides user config. Deploy it to the",
            "per-OS managed config dir on each machine (via MDM/config-mgmt):",
            "  - Linux : /etc/opencode/opencode.json",
            "  - macOS : /Library/Application Support/opencode/opencode.json",
            "  - Windows : %ProgramData%\\opencode\\opencode.json",
            "  Root-owned (chown root:root or root:wheel, chmod 644).",
            "",
            "macOS hard-lock: an MDM delivers " + MOBILECONFIG_FILENAME + " to",
            "  /Library/Managed Preferences/ai.opencode.managed.plist (plist domain",
            "  ai.opencode.managed), which overrides everything. Non-macOS fleets rely on the",
            "  managed dir opencode.json only.",
            "",
            "opencode has no auth helper, so the launcher must export a fresh bearer token into",
            f"${api_key_env} before it starts opencode (opencode reads it via {{env:{api_key_env}}}).",
            "A U2M token lives about one hour, so a long session needs a re-mint. Mint one with:",
            "  databricks auth token --host <workspace-url> --profile <profile> --force-refresh",
            "",
            "Each developer authenticates once:",
            "  databricks auth login --host <workspace-url> --profile <profile>",
        ])
