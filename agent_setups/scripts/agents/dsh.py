"""DeepSeek Harness (dsh) config generator for the Databricks Unity AI Gateway.

Turns the deployed DeepSeek model services (from Terraform outputs) into a DSH
home-level `cordis.patch.yml` that routes DSH's native DeepSeek adapter through
the gateway, plus a token-refresh plugin that mints a fresh Databricks OAuth
token and stores it in the DSH credential store on a timer.

DSH is not configured by a single JSON file. It is a Cordis composition
----------------------------------------------------------------------
A running `dsh` is a plugin tree composed at boot from a profile's bundles, then
the user's home-level `$DSH_HOME/cordis.patch.yml` (default `~/.dsh`). A patch
either replaces one entry's whole `config` by id, or inserts new plugin rows.
This generator emits that home-level patch, so it overlays the shipped `dsh-base`
bundle without forking any package.

Three seams do the work
-----------------------
  - Routing : the shipped `@deepseek-ai/dsh-llm-deepseek` adapter already
    supports an OpenAI-compatible gateway named by `baseURL`. The patch points
    `baseURL` at `<host>/ai-gateway/mlflow/v1` (the adapter appends
    `/chat/completions`) and sets the default model to a deployed DeepSeek
    endpoint's full UC name, which passes straight through to the wire.
  - Auth : the adapter resolves its `apiKeyEnv` reference per request through
    `ctx.credentials`, so a rotated token reaches the next request with no
    restart. The `databricks-token-refresh.mjs` plugin mints a token with the
    Databricks CLI and writes it to that reference on a timer. This mirrors
    opencode's per-request minting, but through DSH's credential seam.
  - Gateway safety : DSH's DeepSeek adapter adds proprietary top-level request
    fields (`dsh_plugin_packages`, `dsh_session_log`) that an OpenAI-compatible
    gateway may reject. The patch disables the two plugins that add them, and
    defaults thinking off (a value the gateway is most likely to accept).

Why a stored token, not an environment variable
-----------------------------------------------
DSH's `credentials-local` layers the launch environment (read-only, wins) over
the writable `$DSH_HOME/.credentials.yaml` file, and the launch environment is a
frozen snapshot. A variable exported after startup is never seen. So the plugin
cannot refresh through `process.env`; it writes the token to the file layer with
`ctx.credentials.set`. The `apiKeyEnv` reference therefore must NOT be set in the
launch environment, or the read-only launch layer would shadow the stored value.
The generator uses a dedicated reference name (`DATABRICKS_GATEWAY_TOKEN`) to
avoid colliding with a developer's own `DEEPSEEK_API_KEY`.

Telemetry is a documented follow-up
-----------------------------------
DSH ships `@deepseek-ai/dsh-session-telemetry-otel`, but its `exporter.headers`
is a static map read at boot, so it cannot carry a refreshing SP-M2M bearer for
the Databricks OTEL collector. Wiring it correctly needs a header-refresh
mechanism, which belongs in its own pass. This generator leaves telemetry as the
shipped default (feedback-gated) and does not touch that row.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from agents.base import AgentGenerator
from gateway import Endpoint, GatewayContext

# The gateway's OpenAI-compatible route. The DeepSeek adapter POSTs to
# `<baseURL>/chat/completions`, so this is the same route opencode's OSS
# provider uses.
GATEWAY_OSS_ROUTE = "/ai-gateway/mlflow/v1"

# The DSH LLM route the DeepSeek adapter owns. A request selects it with this
# provider id; the model id passes through to the wire.
DEEPSEEK_PROVIDER = "deepseek-official"

# The credential reference the adapter resolves for the bearer. A dedicated name
# (not DEEPSEEK_API_KEY) so it does not collide with a developer's own key and is
# not shadowed by a read-only launch-environment value.
CREDENTIAL_REF = "DATABRICKS_GATEWAY_TOKEN"

# The files the generator emits (relative to the out dir).
PATCH_FILENAME = "cordis.patch.yml"
PLUGIN_FILENAME = "databricks-token-refresh.mjs"
# The patch references the plugin by a relative path. `cordis-plugin-include`
# anchors a relative `name` in an `insert` row to the patch file's own directory,
# so this resolves to the plugin dropped beside the patch in `$DSH_HOME`.
PLUGIN_REF_RELATIVE = f"./{PLUGIN_FILENAME}"

# Where DSH reads the home-level patch (documentation only).
DSH_HOME_DEFAULT = "~/.dsh"

# Preferred default model, by endpoint leaf name. First match wins; falls back to
# the first discovered DeepSeek endpoint. Flash first: the economical default,
# matching dsh-base's own `deepseek-v4-flash` default.
DEFAULT_MODEL_PREFERENCES = ["deepseek-v4-flash", "deepseek-v4-pro"]


# The token-refresh plugin source. `__DEFAULT_HOST__`, `__DEFAULT_PROFILE__`,
# `__CREDENTIAL_REF__`, `__REFRESH_SKEW_MS__`, and `__FALLBACK_TTL_MS__` are
# filled in at generation time. Plain ESM JavaScript (no build step) so DSH loads
# it from an absolute path with no npm install and no TypeScript loader.
_PLUGIN_TEMPLATE = r'''// databricks-token-refresh.mjs — GENERATED by unity-gateway-setup. Do not edit by hand.
//
// A DeepSeek Harness (Cordis) plugin. It mints a fresh Databricks OAuth token
// with the Databricks CLI (`databricks auth token`) and stores it in the DSH
// credential reference the DeepSeek adapter resolves for its bearer. It refreshes
// on a timer before the token expires, so a long session never serves a stale
// token, and the CLI refreshes access tokens silently from its cached OAuth
// session. It mirrors opencode's per-request minting, through DSH's credential
// seam.
//
// Why the credential store and not process.env: DSH's credentials-local treats
// the launch environment as a frozen, read-only snapshot that wins over the
// stored file. A variable exported after startup is never seen, and a write to a
// reference the launch environment supplies is refused. So the plugin writes the
// token to the file layer with ctx.credentials.set, and the reference name below
// must NOT be set in the launching shell.

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const run = promisify(execFile)

// Workspace + CLI profile are baked in at generation time. Environment variables
// override them for a developer who runs a different profile.
const HOST = process.env.DATABRICKS_HOST || '__DEFAULT_HOST__'
const PROFILE = process.env.DATABRICKS_CONFIG_PROFILE || '__DEFAULT_PROFILE__'
const CREDENTIAL_REF = '__CREDENTIAL_REF__'

// Refresh a stored token this long before it actually expires.
const REFRESH_SKEW_MS = __REFRESH_SKEW_MS__
// Fallback lifetime when the CLI returns no expiry and the token carries no exp.
const FALLBACK_TTL_MS = __FALLBACK_TTL_MS__
// Retry cadence after a failed mint (e.g. no valid CLI session yet).
const RETRY_MS = 60 * 1000

export const name = 'databricks-token-refresh'
// The credential seam the plugin writes to. Present in dsh-base.
export const inject = ['credentials']

function jwtExpiryMs(token) {
  const parts = token.split('.')
  if (parts.length < 2) return undefined
  try {
    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'))
    if (typeof payload.exp === 'number') return payload.exp * 1000
  } catch {
    // not a JWT; fall through
  }
  return undefined
}

function parseToken(stdout) {
  const data = JSON.parse(stdout)
  const token = data.access_token
  if (typeof token !== 'string' || token.length === 0) {
    throw new Error('databricks auth token returned no access_token')
  }
  let expiresAt = 0
  if (typeof data.expiry === 'string') {
    const parsed = Date.parse(data.expiry)
    if (!Number.isNaN(parsed)) expiresAt = parsed
  }
  if (expiresAt === 0) expiresAt = jwtExpiryMs(token) ?? Date.now() + FALLBACK_TTL_MS
  return { token, expiresAt }
}

async function mint() {
  const { stdout } = await run(
    'databricks',
    ['auth', 'token', '--host', HOST, '--profile', PROFILE, '--output', 'json'],
    { encoding: 'utf8' },
  )
  return parseToken(stdout)
}

export function apply(ctx) {
  let disposed = false
  let timer

  const clear = () => {
    if (timer) {
      clearTimeout(timer)
      timer = undefined
    }
  }

  const schedule = (ms) => {
    clear()
    // unref so the timer never keeps the process alive on its own.
    timer = setTimeout(tick, Math.max(1000, ms))
    if (typeof timer.unref === 'function') timer.unref()
  }

  const tick = async () => {
    if (disposed) return
    try {
      const { token, expiresAt } = await mint()
      // CredentialRef is a compile-time brand; the runtime value is the plain
      // reference name, so the string is accepted here.
      await ctx.credentials.set(CREDENTIAL_REF, token)
      ctx.logger?.info?.('databricks-token-refresh: stored gateway token')
      schedule(expiresAt - Date.now() - REFRESH_SKEW_MS)
    } catch (error) {
      ctx.logger?.warn?.(
        `databricks-token-refresh: mint failed; retrying in ${RETRY_MS / 1000}s. `
          + `Run: databricks auth login --host ${HOST} --profile ${PROFILE}`,
      )
      ctx.logger?.warn?.(String(error?.message ?? error))
      schedule(RETRY_MS)
    }
  }

  // Registration is effect-based: disposing the plugin fiber stops the timer.
  ctx.effect(() => {
    // Front-load the first mint so the token is present before the first request
    // when possible. tick() never throws, so a boot with no valid CLI session
    // logs guidance and retries rather than failing startup.
    void tick()
    return () => {
      disposed = true
      clear()
    }
  })
}
'''


def _render_plugin(host: str, profile: str, ref: str,
                   refresh_skew_ms: int, fallback_ttl_ms: int) -> str:
    """Fill the workspace host, CLI profile, and timing into the plugin source."""
    return (
        _PLUGIN_TEMPLATE
        .replace("__DEFAULT_HOST__", host)
        .replace("__DEFAULT_PROFILE__", profile)
        .replace("__CREDENTIAL_REF__", ref)
        .replace("__REFRESH_SKEW_MS__", str(refresh_skew_ms))
        .replace("__FALLBACK_TTL_MS__", str(fallback_ttl_ms))
    )


def _render_patch(host: str, profile: str, ref: str, model_full_name: str,
                  thinking: bool, tel_tables: dict[str, str] | None = None) -> str:
    """Render the DSH home-level cordis.patch.yml overlay.

    Each top-level list item is a `cordis-plugin-include` patch: an id-targeted
    config replacement, a `disabled` flip, or an `insert` of new rows. The base
    row's whole `config` is replaced, so every field kept is restated.

    `tel_tables` is the signal -> fq-table map from the Terraform telemetry output;
    used in the commented OTEL stub to show actual table names for the
    X-Databricks-UC-Table-Name header (uses a placeholder when absent).
    """
    base_url = f"{host}{GATEWAY_OSS_ROUTE}"
    # thinking off is the gateway-safe default: with `thinking: disabled` the
    # adapter never sends `reasoning_effort`, which an OpenAI-compatible gateway
    # may reject. `--thinking` opts into DeepSeek reasoning at high effort.
    if thinking:
        thinking_block = (
            "    thinking: enabled\n"
            "    reasoningEffort: high\n"
        )
    else:
        thinking_block = "    thinking: disabled\n"

    logs_table = (tel_tables or {}).get("logs", "<otel-logs-table>")

    return f"""# GENERATED by unity-gateway-setup. Do not edit by hand.
#
# DeepSeek Harness home-level patch: routes DSH's native DeepSeek adapter through
# the Databricks Unity AI Gateway. Install it as `$DSH_HOME/cordis.patch.yml`
# (default {DSH_HOME_DEFAULT}) beside {PLUGIN_FILENAME}. DSH applies it after the
# dsh-base bundle, replacing each targeted row's whole config.

# Route the DeepSeek adapter at the gateway's OpenAI-compatible endpoint. The
# adapter appends `/chat/completions`. The key resolves per request from the
# credential store, refreshed by the token plugin below.
- id: llm-deepseek
  config:
    baseURL: '{base_url}'
    apiKeyEnv: {ref}
{thinking_block}
# Start on a deployed DeepSeek gateway endpoint. The full UC name passes straight
# through to the wire as the model id.
- id: agent-default-model
  config:
    provider: {DEEPSEEK_PROVIDER}
    model: '{model_full_name}'

# Gateway safety: these two plugins add DeepSeek-proprietary top-level request
# fields (`dsh_plugin_packages`, `dsh_session_log`) that an OpenAI-compatible
# gateway may reject. Disable them so the request body stays standard.
- id: plugin-package-inventory-deepseek
  disabled: true
- id: session-log-deepseek
  disabled: true

# The token-refresh plugin: mints a Databricks OAuth token with the CLI and
# stores it in the `{ref}` credential reference on a timer. Referenced by a
# relative path, anchored to this patch file's directory.
- insert:
    - id: databricks-token-refresh
      name: '{PLUGIN_REF_RELATIVE}'

# --- OTEL telemetry (DORMANT — static-headers caveat) -----------------------
# @deepseek-ai/dsh-session-telemetry-otel's exporter.headers is a STATIC map
# read at boot: OTLPLogExporter is constructed once from config.exporter in
# packages/session/session-telemetry-otel/src/index.ts (deepseek-ai/deepseek-
# harness commit 7169660d330452d32c91bb2e4788a9b8c2f83a18). Headers cannot
# refresh per export; a token baked at boot will expire. Uncomment this row and
# supply a real minted token only after verifying a header-refresh seam exists in
# your installed dsh version. Content is off by default (mode FULL exports all
# session records; switch to FEEDBACK_ONLY or DISABLED to reduce data shared).
#
# IMPORTANT: YAML does NOT expand shell variables ($VAR syntax). The value of
# Authorization must be a literal minted token string, not a shell reference.
# Mint a short-lived M2M bearer for the telemetry SP (down-scoped to the OTEL UC
# tables) and substitute it in place of <MINTED-TOKEN-VALUE-HERE> before
# uncommenting. Rotate via a wrapper script or environment-file mechanism.
# The X-Databricks-UC-Table-Name header routes the export to the correct UC table
# (matches the OTEL logs table in the Terraform telemetry output).
#
# - id: session-telemetry-otel
#   config:
#     mode: FULL
#     exporter:
#       url: '{host}/api/2.0/otel/v1/logs'
#       headers:
#         Authorization: 'Bearer <MINTED-TOKEN-VALUE-HERE>'
#         X-Databricks-UC-Table-Name: '{logs_table}'
"""


@dataclass(frozen=True)
class _Selection:
    endpoints: list[Endpoint]
    default: Endpoint


# --- Cross-agent hardening knobs NOT wired here (custom CA, version floor, telemetry) --
# Claude Code exposes --ssl-cert-file and --required-min-version. DSH has no config
# equivalent this generator can emit, so neither flag is offered:
#   * Custom CA / TLS: DSH REFUSES NODE_EXTRA_CA_CERTS / SSL_CERT_FILE / SSL_CERT_DIR
#     in any .env or config layer — they are bootstrap-only (see app-boot's
#     BOOTSTRAP_NAMES: "they change what is trusted, not where traffic goes") — and
#     cordis.patch.yml cannot set process env. They must be exported before launch.
#   * Version floor: DSH ships only a package.json Node engines floor (node >=22.19),
#     an install-time runtime requirement, not a config-enforceable DSH self-version
#     lock. Not faked.
#   * OTEL (native, DORMANT): the session-telemetry-otel plugin ships as a COMMENTED
#     row in cordis.patch.yml (see _render_patch). exporter.headers is a static map
#     read at boot (deepseek-ai/deepseek-harness commit 7169660d); no per-export
#     header-refresh seam is confirmed. Set DATABRICKS_OTEL_TOKEN in the environment
#     and uncomment only after verifying the seam in your installed dsh version.
#   * Session hook events: DSH has no hook-event observer seam comparable to Claude
#     Code or Codex (no SubagentStart / PostToolUse / SessionEnd lifecycle events).
#     Custom event reporting (usage/governance/adoption) is documented-only. Wire it
#     when a Cordis lifecycle observer seam is confirmed upstream.
class DshGenerator(AgentGenerator):
    name = "dsh"
    help = "Generate a DeepSeek Harness (dsh) home patch + token plugin for the Unity AI Gateway."

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--default-model",
            default=None,
            help=(
                "Model DSH starts on: a DeepSeek endpoint leaf name (e.g. "
                "'deepseek-v4-flash') or a full three-level UC name. Default: a "
                "preferred DeepSeek alias if present, else the first discovered "
                "DeepSeek endpoint."
            ),
        )
        parser.add_argument(
            "--auth-profile",
            default=None,
            help=(
                "Databricks CLI profile the token plugin uses to mint tokens. Default: "
                "the --profile value. Every developer must have this profile in "
                "~/.databrickscfg."
            ),
        )
        parser.add_argument(
            "--thinking",
            action="store_true",
            help=(
                "Enable DeepSeek reasoning (thinking) at high effort. Default: off, "
                "which never sends `reasoning_effort` — the value an OpenAI-compatible "
                "gateway is most likely to accept."
            ),
        )
        parser.add_argument(
            "--refresh-skew-ms",
            type=int,
            default=5 * 60 * 1000,
            help="Refresh the stored token this many ms before it expires (default: 300000).",
        )
        parser.add_argument(
            "--fallback-ttl-ms",
            type=int,
            default=50 * 60 * 1000,
            help="Assumed token lifetime when neither the CLI nor the JWT reports one (default: 3000000).",
        )

    def _select(self, ctx: GatewayContext, requested: str | None) -> _Selection:
        """Every deployed DeepSeek endpoint, plus the resolved default."""
        deepseek = [
            e for e in ctx.endpoints
            if "deepseek" in e.name.lower() or "deepseek" in e.foundation_model.lower()
        ]
        if not deepseek:
            raise SystemExit(
                "No DeepSeek endpoints found in the Terraform outputs. The DeepSeek "
                "Harness adapter routes DeepSeek models; deploy a deepseek-* model "
                "service first (see terraform/infra open_models)."
            )
        by_name = {e.name: e for e in deepseek}
        by_full = {e.full_name: e for e in deepseek}
        if requested:
            if requested in by_full:
                default = by_full[requested]
            elif requested in by_name:
                default = by_name[requested]
            else:
                raise SystemExit(
                    f"--default-model '{requested}' is not among the "
                    f"{len(deepseek)} DeepSeek endpoints. Available leaves: "
                    f"{', '.join(sorted(by_name))}."
                )
        else:
            default = next(
                (by_name[p] for p in DEFAULT_MODEL_PREFERENCES if p in by_name),
                sorted(deepseek, key=lambda e: e.name)[0],
            )
        return _Selection(endpoints=sorted(deepseek, key=lambda e: e.name), default=default)

    def generate(self, ctx: GatewayContext, args: argparse.Namespace) -> dict[str, str]:
        selection = self._select(ctx, args.default_model)
        auth_profile = args.auth_profile or args.profile

        patch = _render_patch(
            host=ctx.host,
            profile=auth_profile,
            ref=CREDENTIAL_REF,
            model_full_name=selection.default.full_name,
            thinking=args.thinking,
            tel_tables=ctx.telemetry.tables if ctx.telemetry else {},
        )
        plugin = _render_plugin(
            host=ctx.host,
            profile=auth_profile,
            ref=CREDENTIAL_REF,
            refresh_skew_ms=args.refresh_skew_ms,
            fallback_ttl_ms=args.fallback_ttl_ms,
        )

        # Record what was emitted so install_notes reflects the real selection.
        self._default = selection.default
        self._count = len(selection.endpoints)

        return {
            f"dsh/{PATCH_FILENAME}": patch,
            f"dsh/{PLUGIN_FILENAME}": plugin,
        }

    def install_notes(self, args: argparse.Namespace) -> str:
        auth_profile = args.auth_profile or args.profile
        default = getattr(self, "_default", None)
        model_line = (
            f"  Default model: {default.full_name} (provider {DEEPSEEK_PROVIDER})."
            if default is not None else ""
        )
        return "\n".join([
            "DeepSeek Harness (dsh) — home-level patch + token plugin. Two files were written:",
            f"  - {PATCH_FILENAME} : the DSH home patch (routes the DeepSeek adapter at the gateway).",
            f"  - {PLUGIN_FILENAME} : the token plugin (mints a fresh Databricks token on a timer).",
            model_line,
            "",
            f"Install both into your DSH home ({DSH_HOME_DEFAULT}), side by side:",
            f"  - $DSH_HOME/{PATCH_FILENAME}",
            f"  - $DSH_HOME/{PLUGIN_FILENAME}",
            "  (or run: make dsh-install-local)",
            "",
            f"The plugin stores the token in the '{CREDENTIAL_REF}' credential reference. That",
            "variable must NOT be set in your launching shell, or DSH's read-only launch-",
            "environment layer would shadow the stored value.",
            "",
            "Each developer authenticates once (the CLI then refreshes silently):",
            f"  databricks auth login --host {args.host or '<workspace-url>'} --profile {auth_profile}",
            "",
            "Verify the composed tree before running:",
            "  dsh --profile headless --dump-config",
            "",
            "OTEL telemetry (DORMANT — static-headers caveat):",
            "  cordis.patch.yml includes a COMMENTED session-telemetry-otel row.",
            "  The exporter.headers field is a static map read at boot. A token baked",
            "  at boot will expire. IMPORTANT: YAML does NOT expand shell variables",
            "  ($VAR syntax) — you must substitute a real minted token value directly",
            "  into the header before uncommenting. To activate after verifying a",
            "  header-refresh seam exists in your dsh version:",
            "    1. Mint a short-lived M2M bearer for the telemetry SP (down-scoped to",
            "       the OTEL UC tables). YAML will not expand $VAR: substitute the",
            "       literal token string into cordis.patch.yml in place of",
            "       <MINTED-TOKEN-VALUE-HERE>.",
            "    2. Uncomment the session-telemetry-otel row in cordis.patch.yml.",
            "    3. Verify the exporter.url is <workspace>/api/2.0/otel/v1/logs.",
            "    4. The X-Databricks-UC-Table-Name header routes exports to the correct",
            "       UC table (from the Terraform telemetry output); check it matches.",
            "  Verify rows: dsh --profile headless --dump-config | grep telemetry",
            "",
            "Hook events (documented-only):",
            "  DSH has no SubagentStart / PostToolUse / SessionEnd lifecycle observer",
            "  comparable to Claude Code or Codex. Custom event reporting (usage,",
            "  governance, adoption) cannot be wired without a Cordis lifecycle seam.",
            "  Wire it when a lifecycle observer seam is confirmed upstream.",
        ])
