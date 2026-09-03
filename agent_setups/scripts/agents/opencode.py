"""opencode config generator for the Databricks Unity AI Gateway.

Turns the deployed model services (from Terraform outputs) into an `opencode.json`
that routes opencode through the gateway with NATIVE model dialects, plus a pushed
auth plugin that mints a fresh Databricks OAuth token on every request.

Three native providers
----------------------
opencode is built on the Vercel AI SDK. Each model family speaks its own dialect
to its own gateway route, so the generator emits one provider per family that has
deployed endpoints:

  - databricks-anthropic  @ai-sdk/anthropic  <host>/ai-gateway/anthropic/v1
  - databricks-google     @ai-sdk/google     <host>/ai-gateway/gemini/v1beta
  - databricks-oss        @ai-sdk/openai     <host>/ai-gateway/mlflow/v1

Endpoints bucket into families by their `schema`: `anthropic` -> anthropic,
`gemini`/`google` -> google, every other schema (e.g. `openai`) -> oss. This
matches the native surface ucode produces, and it needs no live api-type
discovery because the schema is the family.

Auth — a pushed plugin, not an env var
--------------------------------------
The generator emits `databricks-auth.ts`, an opencode plugin. A `chat.headers`
hook injects `Authorization: Bearer <token>` on every request to the
databricks-* providers. The plugin mints the token with the Databricks CLI
(`databricks auth token`), which refreshes access tokens silently from its cached
OAuth session. The plugin runs `databricks auth login` only when no valid session
exists, so routine access-token expiry needs no interactive step. This replaces
the old `{env:VAR}` bearer: the config carries no launch-minted token, and a long
session never serves a stale one. It matches Claude Code's `apiKeyHelper`.

The managed config references the plugin so opencode loads it. opencode
auto-discovers plugins only under project/global `.opencode` dirs, not the
managed config dir, so the reference is explicit via the `plugin` key.

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

Default (managed) mode emits three files: the OS-independent `opencode.json` for
the managed dir, the `databricks-auth.ts` plugin, and a macOS Configuration
Profile (`.mobileconfig`) that carries the same config keys for the hard-lock
layer. The managed dir file references the plugin by a relative path
(`./databricks-auth.ts`). This path resolves next to the config on every OS. The
`.mobileconfig` references it by the absolute macOS managed path, because a plist
spec resolves relative to `/Library/Managed Preferences`, not the managed dir.
`--user-config` instead emits a per-user `opencode.json` (for
`~/.config/opencode/opencode.json`) plus the plugin, with no enforcement. The
user-mode config references the plugin by an absolute path. opencode then loads
the plugin regardless of the launch cwd. An absolute path also cannot be
displaced when another global config file resolves a relative `plugin` entry.
The local installer rewrites this absolute path to the resolved target dir.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
import uuid
from dataclasses import dataclass

from agents.base import AgentGenerator
from gateway import Endpoint, GatewayContext

# The opencode config JSON schema URL.
CONFIG_SCHEMA_URL = "https://opencode.ai/config.json"

# The plugin file the generator emits and the config references.
PLUGIN_FILENAME = "databricks-auth.ts"
# Relative reference used by the managed-dir opencode.json and the user-mode
# config: opencode resolves a path spec relative to the config file that declares
# it, so this resolves to the plugin dropped alongside the config, on every OS.
PLUGIN_REF_RELATIVE = f"./{PLUGIN_FILENAME}"
# The macOS managed config dir, where install.sh drops both files. The
# .mobileconfig must reference the plugin here by absolute path (a plist spec
# resolves relative to /Library/Managed Preferences, not the managed dir).
MACOS_MANAGED_DIR = "/Library/Application Support/opencode"
PLUGIN_REF_MACOS_ABS = f"{MACOS_MANAGED_DIR}/{PLUGIN_FILENAME}"


def _user_plugin_ref() -> str:
    """Return the user-mode plugin reference: an absolute path, XDG-aware.

    User mode bakes an absolute path. opencode then loads the plugin regardless of
    the launch cwd. An absolute path also cannot be displaced when another global
    config file resolves a relative `plugin` entry. This is a best-effort default
    for a manual copy. The local installer rewrites it to the true target dir.
    This reads XDG_CONFIG_HOME at call time, so it honors the environment at
    generation, not at import.
    """
    user_config_dir = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(user_config_dir, "opencode", PLUGIN_FILENAME)

# Preferred default model, by endpoint leaf name (aliases first). First match
# wins; falls back to the first discovered endpoint if none are present.
DEFAULT_MODEL_PREFERENCES = [
    "kimi-k3",
    "claude-sonnet", "claude-sonnet-4-5", "claude-opus",
    "gpt", "gpt-5-6-sol", "gpt-5-5",
    "gemini", "gemini-2-5-pro",
]

# --- macOS Managed Preferences (the hard-lock layer) ------------------------
MOBILECONFIG_FILENAME = "ai.opencode.managed.mobileconfig"
MANAGED_PREF_DOMAIN = "ai.opencode.managed"
# Stable profile identifier so a redeploy replaces the same profile in place.
PROFILE_IDENTIFIER = "ai.opencode.managed.profile"

# Where opencode reads its per-user config (documentation only).
USER_CONFIG_PATH = "~/.config/opencode/opencode.json"


@dataclass(frozen=True)
class Family:
    """One native model family: its opencode provider id, npm package, and route."""

    provider: str        # opencode provider id, e.g. "databricks-anthropic"
    npm: str             # AI SDK package the provider loads
    route: str           # gateway route base appended to the host for baseURL
    display: str         # human-readable provider name
    model_overlay: dict  # per-model config overlay merged onto every model entry


# @ai-sdk/anthropic injects `eager_input_streaming: true` on tool defs; the
# gateway's strict validator rejects it. opencode's per-call providerOptions read
# `models.<m>.options`, so we disable tool streaming per model to opt out.
_ANTHROPIC = Family(
    provider="databricks-anthropic",
    npm="@ai-sdk/anthropic",
    route="/ai-gateway/anthropic/v1",
    display="Databricks Unity AI Gateway (Anthropic)",
    model_overlay={"options": {"toolStreaming": False}},
)
_GOOGLE = Family(
    provider="databricks-google",
    npm="@ai-sdk/google",
    route="/ai-gateway/gemini/v1beta",
    display="Databricks Unity AI Gateway (Google)",
    model_overlay={},
)
_OSS = Family(
    provider="databricks-oss",
    npm="@ai-sdk/openai",
    route="/ai-gateway/mlflow/v1",
    display="Databricks Unity AI Gateway (OSS)",
    model_overlay={},
)


def _family_for(schema: str) -> Family:
    """Map an endpoint schema to its native family. Unknown schemas route to OSS."""
    if schema == "anthropic":
        return _ANTHROPIC
    if schema in ("gemini", "google"):
        return _GOOGLE
    return _OSS


# The auth plugin source. `__DATABRICKS_HOST__` and `__DATABRICKS_PROFILE__` are
# filled in at generation time. It uses only globals (Buffer, process, the
# injected Bun `$`), so it loads from an absolute path with no npm install.
_PLUGIN_TEMPLATE = r'''// databricks-auth.ts — GENERATED by unity-gateway-setup. Do not edit by hand.
//
// opencode plugin: injects a fresh Databricks OAuth bearer on every request to
// the databricks-* gateway providers. The Databricks CLI (`databricks auth
// token`) mints the token and refreshes access tokens silently from its cached
// OAuth session, so routine expiry needs no interactive step. The plugin runs
// `databricks auth login` (a browser flow) only when no valid session exists.
//
// This replaces a static bearer in the config: the config carries no token, and
// a long session never serves a stale one.

// Workspace + CLI profile are baked in at generation time. Environment variables
// override them for a developer who runs a different profile.
const HOST = process.env.DATABRICKS_HOST || "__DATABRICKS_HOST__"
const PROFILE = process.env.DATABRICKS_CONFIG_PROFILE || "__DATABRICKS_PROFILE__"

// enabled_providers already locks opencode to the databricks-* providers; this
// prefix match is a safety belt so the token never reaches another provider.
const PROVIDER_PREFIX = "databricks-"

// Refresh a cached token this long before it actually expires.
const REFRESH_SKEW_MS = 5 * 60 * 1000
// Fallback lifetime when the CLI returns no expiry and the token carries no exp.
const FALLBACK_TTL_MS = 50 * 60 * 1000

let cached // { token: string, expiresAt: number }
let inflight // Promise<{ token, expiresAt }> | undefined
// Try an interactive login at most once per opencode run.
let loginAttempted = false

function jwtExpiryMs(token) {
  const parts = token.split(".")
  if (parts.length < 2) return undefined
  try {
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"))
    if (typeof payload.exp === "number") return payload.exp * 1000
  } catch {
    // not a JWT; fall through
  }
  return undefined
}

function parseToken(stdout) {
  const data = JSON.parse(stdout)
  const token = data.access_token
  if (typeof token !== "string" || token.length === 0) {
    throw new Error("databricks auth token returned no access_token")
  }
  let expiresAt = 0
  if (typeof data.expiry === "string") {
    const parsed = Date.parse(data.expiry)
    if (!Number.isNaN(parsed)) expiresAt = parsed
  }
  if (expiresAt === 0) expiresAt = jwtExpiryMs(token) ?? Date.now() + FALLBACK_TTL_MS
  return { token, expiresAt }
}

async function runToken($) {
  const res = await $`databricks auth token --host ${HOST} --profile ${PROFILE} --output json`
    .quiet()
    .nothrow()
  return {
    ok: res.exitCode === 0,
    stdout: res.stdout.toString(),
    stderr: res.stderr.toString(),
  }
}

async function mint($) {
  let res = await runToken($)
  if (!res.ok && !loginAttempted) {
    // No valid session: run the browser login once, then retry. Routine
    // access-token expiry never reaches here — the CLI refreshes silently.
    loginAttempted = true
    await $`databricks auth login --host ${HOST} --profile ${PROFILE}`.quiet().nothrow()
    res = await runToken($)
  }
  if (!res.ok) {
    throw new Error(
      `databricks auth token failed. Run: databricks auth login --host ${HOST} --profile ${PROFILE}\n${res.stderr}`,
    )
  }
  return parseToken(res.stdout)
}

async function getToken($) {
  if (cached && Date.now() < cached.expiresAt - REFRESH_SKEW_MS) return cached.token
  if (!inflight) {
    inflight = mint($).finally(() => {
      inflight = undefined
    })
  }
  cached = await inflight
  return cached.token
}

export default {
  id: "databricks-auth",
  async server({ $ }) {
    // Front-load auth to session start: if there is no valid session this opens
    // the browser now, not in the middle of the first message. A valid session
    // returns immediately.
    try {
      await getToken($)
    } catch {
      // Leave it to the per-request path to surface the error.
    }
    return {
      "chat.headers": async (input, output) => {
        const providerID = input?.model?.providerID
        if (!providerID || !providerID.startsWith(PROVIDER_PREFIX)) return
        const token = await getToken($)
        output.headers["Authorization"] = `Bearer ${token}`
      },
    }
  },
}
'''


def _render_plugin(host: str, profile: str) -> str:
    """Fill the workspace host and CLI profile into the plugin source."""
    return (
        _PLUGIN_TEMPLATE
        .replace("__DATABRICKS_HOST__", host)
        .replace("__DATABRICKS_PROFILE__", profile)
    )


def _mobileconfig(config: dict, providers: list[str]) -> str:
    """Render a macOS Configuration Profile (.mobileconfig) for the managed domain.

    The profile carries a single Custom Settings payload whose PayloadType is the
    preference domain (ai.opencode.managed), with the opencode config keys as plist
    values (nested dicts and arrays are valid plist). This is the macOS hard-lock
    layer — an MDM (Jamf-style Custom Settings payload) delivers it to
    /Library/Managed Preferences/ai.opencode.managed.plist. The PayloadIdentifier
    is stable so a redeploy replaces the same profile; each PayloadUUID is
    generated. Non-macOS fleets rely on the managed dir opencode.json only.
    """
    payload = {
        "PayloadType": MANAGED_PREF_DOMAIN,
        "PayloadIdentifier": MANAGED_PREF_DOMAIN,
        "PayloadUUID": str(uuid.uuid4()),
        "PayloadVersion": 1,
        "PayloadEnabled": True,
        "PayloadDisplayName": "opencode managed configuration",
    }
    # Carry the same config keys the managed dir opencode.json uses, but reference
    # the plugin by its absolute macOS path (a plist spec resolves relative to
    # /Library/Managed Preferences, not the managed dir).
    payload.update(config)
    payload["plugin"] = [PLUGIN_REF_MACOS_ABS]

    profile = {
        "PayloadContent": [payload],
        "PayloadType": "Configuration",
        "PayloadIdentifier": PROFILE_IDENTIFIER,
        "PayloadUUID": str(uuid.uuid4()),
        "PayloadVersion": 1,
        "PayloadScope": "System",
        "PayloadDisplayName": "unity-gateway-setup opencode managed config",
        "PayloadDescription": (
            f"Locks opencode to the Databricks Unity AI Gateway providers "
            f"({', '.join(providers)}) and loads the databricks-auth plugin "
            "(overrides user config). Generated by unity-gateway-setup."
        ),
        "PayloadOrganization": "unity-gateway-setup",
    }
    return plistlib.dumps(profile, sort_keys=False).decode("utf-8")


# --- Cross-agent hardening knobs NOT wired here (custom CA + version floor) --
# Claude Code exposes --ssl-cert-file and --required-min-version. opencode has no
# config equivalent this generator can emit, so neither flag is offered:
#   * Custom CA / TLS: opencode's documented mechanism is the NODE_EXTRA_CA_CERTS
#     ENVIRONMENT variable, exported before launch (see opencode docs/network). The
#     opencode config schema (Config.Info) has no env-injection key, and Node reads
#     NODE_EXTRA_CA_CERTS at startup, so the auth plugin cannot set it after boot.
#     Set it in the launch environment instead.
#   * Version floor: the opencode config schema has no minimum-version / version-lock
#     key (autoupdate controls updates, not a floor). Not faked.
class OpenCodeGenerator(AgentGenerator):
    name = "opencode"
    help = "Generate an opencode.json (native providers + auth plugin) for the Unity AI Gateway."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--default-model",
            default=None,
            help=(
                "Model opencode starts on: an endpoint leaf name (e.g. 'claude-sonnet', "
                "'gpt') or a full three-level UC name. Default: a preferred alias if "
                "present, else the first discovered endpoint."
            ),
        )
        parser.add_argument(
            "--auth-profile",
            default=None,
            help=(
                "Databricks CLI profile the auth plugin uses to mint tokens. Default: the "
                "--profile value. Every developer must have this profile in ~/.databrickscfg."
            ),
        )
        # ---- deployment model ----
        parser.add_argument(
            "--user-config",
            action="store_true",
            help=(
                "Emit a per-user, non-enforced opencode.json (for "
                f"{USER_CONFIG_PATH}) plus the plugin, instead of the default managed "
                "bundle. Default (managed) emits the OS-independent opencode.json for the "
                "per-OS managed config dir, the plugin, and a macOS Configuration Profile "
                "(.mobileconfig) for the hard-lock managed preferences layer."
            ),
        )

    def _select(self, ctx: GatewayContext) -> list[Endpoint]:
        """Every deployed endpoint; each buckets into a native family by schema."""
        if not ctx.endpoints:
            raise SystemExit("No endpoints found in the Terraform outputs.")
        return list(ctx.endpoints)

    def _resolve_default(self, eps: list[Endpoint], requested: str | None) -> Endpoint:
        by_name = {e.name: e for e in eps}
        by_full = {e.full_name: e for e in eps}
        if requested:
            if requested in by_full:
                return by_full[requested]
            if requested in by_name:
                return by_name[requested]
            raise SystemExit(
                f"--default-model '{requested}' is not among the {len(eps)} endpoints. "
                f"Available leaves: {', '.join(sorted(by_name))}."
            )
        for pref in DEFAULT_MODEL_PREFERENCES:
            if pref in by_name:
                return by_name[pref]
        return sorted(eps, key=lambda e: (e.schema, e.name))[0]

    def _build_providers(self, ctx: GatewayContext, eps: list[Endpoint]) -> dict[str, dict]:
        """Build one provider per native family that has deployed endpoints.

        Model IDs (the `models` map keys) are the endpoint FULL UC names
        (catalog.schema.endpoint) — the same string Codex uses. Aliases sort first,
        then version pins. Each model carries its family overlay (e.g. Anthropic
        disables tool streaming). apiKey is a placeholder: the plugin injects a
        fresh `Authorization: Bearer` on every request, and the gateway
        authenticates on that header.
        """
        by_provider: dict[str, list[Endpoint]] = {}
        family_by_provider: dict[str, Family] = {}
        for ep in eps:
            fam = _family_for(ep.schema)
            by_provider.setdefault(fam.provider, []).append(ep)
            family_by_provider[fam.provider] = fam

        providers: dict[str, dict] = {}
        for provider_id in sorted(by_provider):
            fam = family_by_provider[provider_id]
            catalog = sorted(by_provider[provider_id], key=lambda e: (not e.is_alias, e.name))
            models: dict[str, dict] = {}
            for ep in catalog:
                overlay = {"name": ep.name}
                overlay.update(fam.model_overlay)
                models[ep.full_name] = overlay
            providers[provider_id] = {
                "npm": fam.npm,
                "name": fam.display,
                "options": {
                    "baseURL": f"{ctx.host}{fam.route}",
                    "apiKey": "databricks-managed",
                },
                "models": models,
            }
        return providers

    def _build_config(self, providers: dict[str, dict], default_ep: Endpoint,
                      plugin_ref: str) -> dict:
        """The opencode config object shared by managed and user mode.

        `enabled_providers` locks opencode to the databricks-* providers. `plugin`
        loads the auth plugin. `model` is the default endpoint prefixed with its
        family provider id.
        """
        default_provider = _family_for(default_ep.schema).provider
        return {
            "$schema": CONFIG_SCHEMA_URL,
            "provider": providers,
            "model": f"{default_provider}/{default_ep.full_name}",
            "enabled_providers": sorted(providers),
            "plugin": [plugin_ref],
        }

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        eps = self._select(ctx)
        default_ep = self._resolve_default(eps, args.default_model)
        providers = self._build_providers(ctx, eps)
        managed = not args.user_config
        auth_profile = args.auth_profile or args.profile

        # Managed mode references the plugin by a relative path (resolves next to
        # the config on every OS). User mode references it by an absolute path, so
        # opencode loads it regardless of the launch cwd and no other global config
        # file can displace it via relative resolution.
        plugin_ref = PLUGIN_REF_RELATIVE if managed else _user_plugin_ref()
        config = self._build_config(providers, default_ep, plugin_ref)
        config_json = json.dumps(config, indent=2) + "\n"
        plugin_source = _render_plugin(ctx.host, auth_profile)

        # Record what was actually emitted so install_notes reflects the real bundle.
        self._managed = managed
        self._providers = sorted(providers)

        files = {
            "opencode/opencode.json": config_json,
            f"opencode/{PLUGIN_FILENAME}": plugin_source,
        }
        if managed:
            files[f"opencode/{MOBILECONFIG_FILENAME}"] = _mobileconfig(config, sorted(providers))
        return files

    def install_notes(self, args: argparse.Namespace) -> str:
        auth_profile = args.auth_profile or args.profile
        if args.user_config:
            return "\n".join([
                "USER-CONFIG mode (--user-config): per-user, non-enforced. Deploy per developer:",
                f"  Copy opencode.json + {PLUGIN_FILENAME} -> ~/.config/opencode/",
                "  (or run: make opencode-install-local)",
                "",
                f"The {PLUGIN_FILENAME} plugin mints a fresh Databricks token on every request",
                "via the Databricks CLI, so no environment variable or launcher step is needed.",
                "The CLI refreshes access tokens silently, so routine expiry needs no login.",
                "",
                "Each developer authenticates once (the plugin also auto-runs this if needed):",
                f"  databricks auth login --host <workspace-url> --profile {auth_profile}",
            ])

        # Managed mode (default).
        return "\n".join([
            "MANAGED mode (default): enforced. Three files were written to the bundle opencode/:",
            "  - opencode.json           : the OS-independent managed config (overrides user config).",
            f"  - {PLUGIN_FILENAME}      : the auth plugin (mints a fresh token per request).",
            f"  - {MOBILECONFIG_FILENAME} : a macOS Configuration Profile (the hard-lock layer).",
            "",
            "opencode reads managed config LAST and it overrides user config. Deploy opencode.json",
            f"AND {PLUGIN_FILENAME} to the per-OS managed config dir on each machine (via MDM):",
            "  - Linux : /etc/opencode/",
            "  - macOS : /Library/Application Support/opencode/",
            "  - Windows : %ProgramData%\\opencode\\",
            "  Root-owned (chown root:root or root:wheel, chmod 644). The config references the",
            f"  plugin by a relative path, so keep {PLUGIN_FILENAME} beside opencode.json.",
            "",
            "macOS hard-lock: an MDM delivers " + MOBILECONFIG_FILENAME + " to",
            "  /Library/Managed Preferences/ai.opencode.managed.plist (plist domain",
            f"  ai.opencode.managed), which overrides everything. It references {PLUGIN_FILENAME}",
            f"  by its absolute path ({PLUGIN_REF_MACOS_ABS}), so the plugin must be at that path.",
            "  Non-macOS fleets rely on the managed dir opencode.json only.",
            "",
            f"The {PLUGIN_FILENAME} plugin mints a fresh Databricks token on every request via the",
            "Databricks CLI, so no environment variable or launcher step is needed. The CLI",
            "refreshes access tokens silently, so routine expiry needs no login.",
            "",
            "Each developer authenticates once (the plugin also auto-runs this if needed):",
            f"  databricks auth login --host <workspace-url> --profile {auth_profile}",
        ])
