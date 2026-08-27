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
import sys

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

# Preferred default model, by endpoint leaf name (Codex is GPT-oriented). First
# match wins; falls back to the first discovered endpoint if none are present.
DEFAULT_MODEL_PREFERENCES = ["gpt", "gpt-sol", "gpt-5-6-sol", "gpt-5-5", "gpt-5-6-luna"]

# Codex reasoning-effort levels (mirrors the CLI's own enum).
REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]

# Where Codex reads its config (documentation only; nothing is written there).
CODEX_HOME_DEFAULT = "~/.codex"


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

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        eps = self._select(ctx, args)
        default_ep = self._resolve_default(eps, args.default_model)
        provider = args.provider_name
        base_url = f"{ctx.host}{args.gateway_path}"
        auth_script = _auth_command_script(ctx.host, args.__dict__.get("profile", "DEFAULT"), args.databricks_bin)

        # Commented catalog of switchable models (aliases first, then version pins).
        catalog = sorted(eps, key=lambda e: (not e.is_alias, e.schema, e.name))
        catalog_lines = "\n".join(f"#   {e.full_name}" for e in catalog)

        lines = [
            "# Generated by unity-gateway-setup (agent_setups). Do not edit by hand.",
            "#",
            "# Routes the Codex CLI through the Databricks Unity AI Gateway",
            f"# ({args.gateway_path} + /responses = mlflow/v1/responses, wire_api=\"responses\").",
            "# Gateway routing + OAuth only — the",
            "# ChatGPT desktop app (plugins, computer-use, marketplaces) is a separate,",
            "# orthogonal install and is intentionally NOT reproduced here.",
            "#",
            f"# Deploy to $CODEX_HOME/config.toml (default {CODEX_HOME_DEFAULT}/config.toml), OR keep",
            f"# your existing config.toml and drop this in as $CODEX_HOME/{provider}.config.toml,",
            f"# then launch with:  codex -p {provider}",
            "#",
            f"# Each developer authenticates once: databricks auth login --profile <profile>.",
            "#",
            f"# Switchable gateway models exposing {args.api_type} (use `codex -m <full-name>`):",
            catalog_lines,
            "",
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
            "",
        ]
        content = "\n".join(lines)
        return {"codex/config.toml": content}

    def install_notes(self, args: argparse.Namespace) -> str:
        provider = args.provider_name
        return "\n".join([
            "Codex has no OS-level managed-config path (unlike Claude Code). Deploy per user:",
            f"  Full config     : copy config.toml -> $CODEX_HOME/config.toml (default {CODEX_HOME_DEFAULT}/config.toml)",
            f"  Non-destructive : copy config.toml -> $CODEX_HOME/{provider}.config.toml, then `codex -p {provider}`",
            "    (layers the gateway provider on top of an existing (e.g. ChatGPT-app) config.toml)",
            "",
            "Each developer authenticates once:",
            "  databricks auth login --host <workspace-url> --profile <profile>",
            "",
            "Requires python3 + the databricks CLI on PATH (the auth command uses both).",
            "Verify with:  codex doctor    (checks config, auth, and runtime health)",
            "The ChatGPT desktop app is optional and orthogonal — not needed for CLI gateway use.",
        ])
