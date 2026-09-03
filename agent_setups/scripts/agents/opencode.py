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
import subprocess
import sys
import urllib.parse
import urllib.request
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

# --- Hook telemetry (Zerobus REST custom reporting events) ------------------
# Categories available for opencode: only 'usage' (tool_used via tool.execute.after)
# has an implemented producer in the TS plugin. governance and adoption have no
# confirmed hook seam in opencode (no shell-tool PreToolUse or Read-file event),
# so they are excluded until a seam is confirmed. A selection with no implemented
# producer would silently never emit; restrict HOOK_CATEGORIES to the ones wired.
# Hook names confirmed from anomalyco/opencode packages/plugin/src/index.ts
# (commit f12e14cf1640cbf0dfb6b1ff425b2daaef459eec):
#   tool.execute.after     — fires after every tool call with tool name + args
#   event (session.status idle) — normal session-completion signal; primary flush boundary
#   event (session.deleted)    — explicit session deletion; backup drain
HOOK_CATEGORIES = ("usage",)


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

__HOOK_TELEMETRY_IMPORTS__

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
    const _ret = {
      "chat.headers": async (input, output) => {
        const providerID = input?.model?.providerID
        if (!providerID || !providerID.startsWith(PROVIDER_PREFIX)) return
        const token = await getToken($)
        output.headers["Authorization"] = `Bearer ${token}`
      },
    }
    __HOOK_TELEMETRY__
    return _ret
  },
}
'''


# Node.js imports for the hook-telemetry addon; injected at the top of the plugin.
_PLUGIN_TELEMETRY_IMPORTS = (
    "import { appendFileSync, chmodSync, existsSync, mkdirSync, readdirSync,"
    " readFileSync, renameSync, unlinkSync } from 'node:fs'\n"
    "import { homedir, hostname } from 'node:os'\n"
    "import { join } from 'node:path'"
)

# The hook-telemetry code block injected into server() when hook telemetry is on.
# Sentinels replaced by _plugin_telemetry_block():
#   __ZB_ENDPOINT__, __ZB_TABLE__, __ZB_SECRET__, __ZB_CATEGORIES__,
#   __ZB_CONTENT__, __ZB_TOKEN_TTL_MS__
# Hook names confirmed from anomalyco/opencode packages/plugin/src/index.ts
# (commit f12e14cf1640cbf0dfb6b1ff425b2daaef459eec):
#   tool.execute.after  (input: { tool, sessionID, callID, args })
#   event (session.status idle)  — normal session-completion signal; primary flush boundary
#   event (session.deleted)      — explicit session deletion; backup drain
# Event hook dispatch confirmed in packages/opencode/src/plugin/index.ts (same commit):
#   hook["event"]?.({ event: { id, type, properties: event.data } })
#   session.status: event.properties.sessionID, event.properties.status.type === 'idle'
#   session.deleted: event.properties.sessionID (v1 schema; info.id as fallback)
_PLUGIN_TELEMETRY_BLOCK = r"""
  // --- Hook telemetry: Zerobus REST event reporter ---
  // Spool tool events locally (no network, no token on the hot path); AWAIT the flush
  // at session end. Mirrors the Claude Code hook emitter: same UC table, SP, auth.
  // Hook names confirmed: anomalyco/opencode packages/plugin/src/index.ts
  // commit f12e14cf1640cbf0dfb6b1ff425b2daaef459eec.
  // Event hook dispatch: hook["event"]?.({ event: { id, type, properties: event.data } })
  // session.status idle: event.properties.sessionID, event.properties.status.type === 'idle'
  // session.deleted:     event.properties.sessionID (v1 schema; info.id as fallback)

  // Read endpoint from runtime env first so a dormant offline-generated bundle can be
  // activated by setting ZEROBUS_ENDPOINT in the launch environment — no source edit.
  const ZB_ENDPOINT = process.env.ZEROBUS_ENDPOINT || '__ZB_ENDPOINT__'
  const ZB_TABLE = '__ZB_TABLE__'
  const ZB_SECRET = '__ZB_SECRET__'
  const ZB_CATEGORIES = '__ZB_CATEGORIES__'.split(',').filter(Boolean)
  const ZB_CONTENT = __ZB_CONTENT__
  const ZB_TOKEN_TTL_MS = __ZB_TOKEN_TTL_MS__
  const ZB_AGENT = 'opencode'

  // Second bearer token cache — distinct from the gateway token cache above.
  // Different principal (telemetry SP) and audience (zerobusDirectWriteApi).
  let _zbCached    // { token: string, expiresAt: number } | undefined
  let _zbInflight  // Promise<string | null> | undefined

  // Workspace user identity — resolved once per session, cached in memory.
  // Mirrors claude_code.py _ws_user: workspace email (from databricks CLI), not OS login.
  let _zbWsUser = null // string | null  (null = not yet resolved)
  const _zbGetWsUser = async () => {
    if (_zbWsUser !== null) return _zbWsUser
    try {
      const res = await $`databricks current-user me --profile ${PROFILE} -o json`.quiet().nothrow()
      if (res.exitCode === 0) {
        const data = JSON.parse(res.stdout.toString())
        _zbWsUser = data.userName ?? data.emails?.[0]?.value ?? process.env.USER ?? 'unknown'
        return _zbWsUser
      }
    } catch { /* non-fatal */ }
    _zbWsUser = process.env.USER ?? 'unknown'
    return _zbWsUser
  }

  const _zbSpoolDir = join(homedir(), '.cache', 'unity-gateway', 'opencode-spool')
  const _zbSpoolFile = (sid) => {
    const safe = (sid || 'unknown').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 64)
    return join(_zbSpoolDir, `${safe}.jsonl`)
  }

  const _zbMintToken = async () => {
    try {
      const res = await $`databricks api get "/api/2.1/unity-catalog/secrets/${ZB_SECRET}?include_value=true" --profile ${PROFILE}`
        .quiet().nothrow()
      if (res.exitCode !== 0) return null
      const sj = JSON.parse(res.stdout.toString())
      const creds = JSON.parse(sj.effective_value)
      const wsid = new URL(ZB_ENDPOINT).hostname.split('.')[0]
      const parts = ZB_TABLE.split('.')
      const ad = [
        { type: 'unity_catalog_privileges', privileges: ['USE CATALOG'], object_type: 'CATALOG', object_full_path: parts[0] },
        { type: 'unity_catalog_privileges', privileges: ['USE SCHEMA'], object_type: 'SCHEMA', object_full_path: parts.slice(0, 2).join('.') },
        { type: 'unity_catalog_privileges', privileges: ['SELECT', 'MODIFY'], object_type: 'TABLE', object_full_path: ZB_TABLE },
      ]
      const body = new URLSearchParams({
        grant_type: 'client_credentials',
        scope: 'all-apis',
        resource: `api://databricks/workspaces/${wsid}/zerobusDirectWriteApi`,
        authorization_details: JSON.stringify(ad),
      })
      const basic = Buffer.from(`${creds.client_id}:${creds.client_secret}`).toString('base64')
      const ac = new AbortController()
      const t = setTimeout(() => ac.abort(), 15_000)
      const r = await fetch(`${HOST.replace(/\/$/, '')}/oidc/v1/token`, {
        method: 'POST',
        headers: { Authorization: `Basic ${basic}`, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
        signal: ac.signal,
      }).finally(() => clearTimeout(t))
      if (!r.ok) return null
      return (await r.json()).access_token ?? null
    } catch { return null }
  }

  const _zbGetToken = async () => {
    if (_zbCached && Date.now() < _zbCached.expiresAt - REFRESH_SKEW_MS) return _zbCached.token
    if (!_zbInflight) {
      _zbInflight = _zbMintToken().finally(() => { _zbInflight = undefined })
    }
    const token = await _zbInflight
    if (token) { _zbCached = { token, expiresAt: Date.now() + ZB_TOKEN_TTL_MS }; return token }
    return _zbCached?.token ?? null
  }

  const _zbAppend = (sid, evt) => {
    if (!ZB_ENDPOINT) return
    try {
      mkdirSync(_zbSpoolDir, { recursive: true, mode: 0o700 })
      chmodSync(_zbSpoolDir, 0o700) // defensive: restrict if pre-existing dir was wider
      appendFileSync(_zbSpoolFile(sid), JSON.stringify(evt) + '\n', { mode: 0o600 })
    } catch { /* non-fatal */ }
  }

  const _zbFlush = async (sid) => {
    if (!ZB_ENDPOINT) return
    const sf = _zbSpoolFile(sid)
    try { if (!existsSync(sf)) return } catch { return }
    const sending = sf + '.sending.' + process.pid
    try { renameSync(sf, sending) } catch { return }
    try {
      const token = await _zbGetToken()
      if (!token) { try { appendFileSync(sf, readFileSync(sending)) } catch {} unlinkSync(sending); return }
      const lines = readFileSync(sending, 'utf8').trim().split('\n').filter(Boolean)
      if (!lines.length) { unlinkSync(sending); return }
      const batch = lines.map(l => JSON.parse(l))
      const ac2 = new AbortController()
      const t2 = setTimeout(() => ac2.abort(), 15_000)
      const r = await fetch(`${ZB_ENDPOINT.replace(/\/$/, '')}/zerobus/v1/tables/${ZB_TABLE}/insert`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(batch),
        signal: ac2.signal,
      }).finally(() => clearTimeout(t2))
      if (r.ok) { unlinkSync(sending) } else { try { appendFileSync(sf, readFileSync(sending)) } catch {} unlinkSync(sending) }
    } catch {
      try { appendFileSync(sf, readFileSync(sending)) } catch {}
      try { unlinkSync(sending) } catch {}
    }
  }

  const _zbSweep = async () => {
    try {
      if (!existsSync(_zbSpoolDir)) return
      // Recover interrupted flushes (finding 5): a .jsonl.sending.<pid> file left by a
      // prior process that exited mid-flush is renamed back to the base .jsonl so the next
      // flush retries it (at-least-once). Best-effort: if the owning process is still alive
      // the rename races with its unlinkSync, but both outcomes are safe (content is either
      // re-queued or already deleted). This pairs with finding 3 — see UPSTREAM LIMITATION
      // note below: a batch interrupted by immediate process exit is recovered here on the
      // next session start, not delivered synchronously at exit.
      for (const f of readdirSync(_zbSpoolDir).filter(f => /\.jsonl\.sending\.\d+$/.test(f))) {
        const base = join(_zbSpoolDir, f.replace(/\.sending\.\d+$/, ''))
        try { renameSync(join(_zbSpoolDir, f), base) } catch { /* non-fatal */ }
      }
      for (const f of readdirSync(_zbSpoolDir).filter(f => f.endsWith('.jsonl'))) {
        await _zbFlush(f.slice(0, -6))
      }
    } catch { /* non-fatal */ }
  }

  const _zbCatEnabled = (cat) => ZB_CATEGORIES.includes(cat)

  // Sweep leftover spool from prior sessions at plugin start (mirror claude_code.py flush_spool re-sweep).
  await _zbSweep()

  // Tool invocation: spool locally (no network, no token on the hot path).
  // user + machine mirror the fields claude_code.py emits so coverage.sql joins work.
  if (_zbCatEnabled('usage')) {
    _ret["tool.execute.after"] = async (input, output) => {
      try {
        const ts = Date.now() * 1000
        const user = await _zbGetWsUser()
        const machine = hostname()
        _zbAppend(input.sessionID, {
          event_id: `${input.sessionID || 'u'}-${input.callID || 'c'}-${ts}`,
          event_time: ts, category: 'usage', event_name: 'tool_used',
          session_id: input.sessionID, agent: ZB_AGENT,
          user, machine,
          attributes: JSON.stringify({ tool: input.tool, call_id: input.callID, ...(ZB_CONTENT ? { args: JSON.stringify(input.args) } : {}) }),
        })
      } catch { /* non-fatal */ }
    }
  }

  // UPSTREAM LIMITATION (finding 3): opencode dispatches plugin event hooks as
  //   void hook["event"]?.({ ... })
  // (anomalyco/opencode packages/opencode/src/plugin/index.ts, commit
  // f12e14cf1640cbf0dfb6b1ff425b2daaef459eec) — the returned Promise is NOT awaited.
  // If the process exits immediately after the session ends, the in-flight flush may be
  // lost. This is an upstream limitation; the generator cannot make opencode await it.
  // Recovery: _zbSweep() (above) renames any orphaned .jsonl.sending.<pid> files back
  // to .jsonl on the next session start, so a batch interrupted mid-flush is retried
  // at-least-once. Delivery is best-effort: telemetry may be delayed to the next session
  // start but is not permanently lost unless the spool dir is cleared.
  //
  // Session idle/end: flush is awaited INSIDE the async handler body (process is alive
  // while opencode waits for other hooks; we get a best-effort window here).
  // session.status idle is the normal turn-completion signal (fires every time a
  // session goes idle after a run). session.deleted fires only on explicit deletion
  // and is kept as a backup drain for sessions that end without an idle transition.
  // Both confirmed in anomalyco/opencode packages/opencode/src/plugin/index.ts
  // (commit f12e14cf1640cbf0dfb6b1ff425b2daaef459eec):
  //   hook["event"]?.({ event: { id, type, properties: event.data } })
  _ret["event"] = async ({ event }) => {
    if (event.type === 'session.status') {
      if (event.properties?.status?.type !== 'idle') return
      const sid = event.properties?.sessionID ?? null
      await (sid ? _zbFlush(String(sid)) : _zbSweep())
      return
    }
    if (event.type === 'session.deleted') {
      const sid = event.properties?.sessionID ?? event.properties?.info?.id ?? null
      await (sid ? _zbFlush(String(sid)) : _zbSweep())
    }
  }
"""


def _plugin_telemetry_block(endpoint: str, table: str, secret: str,
                             categories: list[str], log_content: bool,
                             token_ttl_seconds: int) -> str:
    """Render the hook-telemetry code block with baked defaults."""
    repl = {
        "__ZB_ENDPOINT__": endpoint.rstrip("/"),
        "__ZB_TABLE__": table,
        "__ZB_SECRET__": secret,
        "__ZB_CATEGORIES__": ",".join(categories),
        "__ZB_CONTENT__": "true" if log_content else "false",
        "__ZB_TOKEN_TTL_MS__": str(token_ttl_seconds * 1000),
    }
    block = _PLUGIN_TELEMETRY_BLOCK
    for sentinel, val in repl.items():
        block = block.replace(sentinel, val)
    return block


def _zerobus_cloud_suffix(host: str) -> str:
    """Zerobus host suffix for the workspace's cloud, inferred from the workspace host."""
    h = host.lower()
    if h.endswith(".azuredatabricks.net"):
        return ".azuredatabricks.net"
    if h.endswith(".gcp.databricks.com"):
        return ".gcp.databricks.com"
    return ".cloud.databricks.com"  # AWS (default)


def _derive_zerobus_endpoint(host: str, profile: str, databricks_bin: str = "databricks") -> str | None:
    """Derive https://<workspace-id>.zerobus.<region><cloud-suffix> from workspace metadata.

    Best-effort: returns None on any failure so generation still succeeds (the hook
    ships dormant until ZEROBUS_ENDPOINT is known at runtime).
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


def _render_plugin(host: str, profile: str, telemetry_block: str | None = None) -> str:
    """Fill the workspace host, CLI profile, and optional hook-telemetry block into the plugin source."""
    imports = _PLUGIN_TELEMETRY_IMPORTS if telemetry_block is not None else "// no hook telemetry"
    block = telemetry_block if telemetry_block is not None else "  // hook-telemetry: off"
    return (
        _PLUGIN_TEMPLATE
        .replace("__DATABRICKS_HOST__", host)
        .replace("__DATABRICKS_PROFILE__", profile)
        .replace("__HOOK_TELEMETRY_IMPORTS__", imports)
        .replace("__HOOK_TELEMETRY__", block)
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


# --- Cross-agent hardening knobs NOT wired here (custom CA, version floor, native OTEL) --
# Claude Code exposes --ssl-cert-file and --required-min-version. opencode has no
# config equivalent this generator can emit, so neither flag is offered:
#   * Custom CA / TLS: opencode's documented mechanism is the NODE_EXTRA_CA_CERTS
#     ENVIRONMENT variable, exported before launch (see opencode docs/network). The
#     opencode config schema (Config.Info) has no env-injection key, and Node reads
#     NODE_EXTRA_CA_CERTS at startup, so the auth plugin cannot set it after boot.
#     Set it in the launch environment instead.
#   * Version floor: the opencode config schema has no minimum-version / version-lock
#     key (autoupdate controls updates, not a floor). Not faked.
#   * Native OTEL: opencode has no `experimental.openTelemetry` or equivalent config
#     key this generator can emit. Use the OTEL_* launch-environment recipe instead
#     (see install_notes). A static bearer in OTEL_EXPORTER_OTLP_HEADERS will expire;
#     refresh it via a wrapper script or CI/CD rotation, not via the config.
#     Never emit experimental.openTelemetry, OTEL_EXPORTER_OTLP_ENDPOINT, or
#     OTEL_EXPORTER_OTLP_HEADERS inside opencode.json — not faked here.
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
        # ---- hook telemetry (custom reporting events via Zerobus REST) ----
        parser.add_argument(
            "--hook-telemetry",
            choices=["auto", "on", "off"],
            default="auto",
            help=(
                "Extend the databricks-auth.ts plugin with Zerobus REST event hooks that "
                "stream reporting events (usage) to the same table as Claude Code. 'auto' "
                "(default) enables it iff the Terraform telemetry.hook_events table is "
                "present; 'on' requires it; 'off' skips. Hook names confirmed from "
                "anomalyco/opencode packages/plugin/src/index.ts (commit "
                "f12e14cf1640cbf0dfb6b1ff425b2daaef459eec): tool.execute.after + "
                "event (session.status idle as primary flush, session.deleted as backup)."
            ),
        )
        parser.add_argument(
            "--hook-categories",
            default=",".join(HOOK_CATEGORIES),
            help=(
                "Comma-separated reporting categories to wire up. Currently only "
                f"'usage' is implemented (tool.execute.after). Valid: "
                f"{', '.join(HOOK_CATEGORIES)}. Default: usage. "
                "governance/adoption have no confirmed hook seam in opencode and "
                "are excluded; a selection with no implemented producer is rejected."
            ),
        )
        parser.add_argument(
            "--hook-log-content",
            action="store_true",
            help=(
                "Include tool args in the tool_used event. Privacy-sensitive; OFF by "
                "default so only tool names and session metadata are reported."
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
                "telemetry.hook_events endpoint > auto-derivation from workspace metadata. "
                "Format: https://<workspace-id>.zerobus.<region>.cloud.databricks.com"
            ),
        )

    def _hook_parts(self, ctx: GatewayContext, args: argparse.Namespace) -> tuple[str | None, list[str]]:
        """Return (plugin telemetry block, enabled categories) — or (None, []) when off.

        When on, extends the databricks-auth.ts plugin with a Zerobus spool/flush
        mechanism. Uses the same telemetry SP, UC secret, and hook_events table as
        Claude Code and Codex. The token cache is a SECOND cache distinct from the
        gateway token (different SP and audience: zerobusDirectWriteApi).
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

        endpoint = args.zerobus_endpoint or (he.get("endpoint") if isinstance(he, dict) else "") or ""
        profile = args.auth_profile or args.__dict__.get("profile", "DEFAULT")
        if not endpoint:
            endpoint = _derive_zerobus_endpoint(ctx.host, profile) or ""
            if endpoint:
                print(
                    f"[opencode] hook telemetry: derived Zerobus endpoint {endpoint} "
                    "from workspace metadata.",
                    file=sys.stderr,
                )
        if not endpoint:
            print(
                "[opencode] hook telemetry: no Zerobus endpoint available — hooks ship "
                "dormant (no-op) until ZB_ENDPOINT is set in the plugin.",
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

        secret = tel.secret_full_name  # type: ignore[union-attr]
        block = _plugin_telemetry_block(
            endpoint=endpoint,
            table=table,
            secret=secret,
            categories=categories,
            log_content=args.hook_log_content,
            token_ttl_seconds=args.hook_token_ttl_seconds,
        )
        print(f"[opencode] hook telemetry: {len(categories)} categor"
              f"{'y' if len(categories) == 1 else 'ies'} -> {table}", file=sys.stderr)
        return block, categories

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

        hook_block, hook_categories = self._hook_parts(ctx, args)
        plugin_source = _render_plugin(ctx.host, auth_profile, telemetry_block=hook_block)

        # Record what was actually emitted so install_notes reflects the real bundle.
        self._managed = managed
        self._providers = sorted(providers)
        self._emitted_hooks = hook_block is not None
        self._tel_tables: dict[str, str] = ctx.telemetry.tables if ctx.telemetry else {}

        files = {
            "opencode/opencode.json": config_json,
            f"opencode/{PLUGIN_FILENAME}": plugin_source,
        }
        if managed:
            files[f"opencode/{MOBILECONFIG_FILENAME}"] = _mobileconfig(config, sorted(providers))
        return files

    def install_notes(self, args: argparse.Namespace) -> str:
        auth_profile = args.auth_profile or args.profile
        hooks_on = getattr(self, "_emitted_hooks", args.hook_telemetry != "off")
        if args.user_config:
            lines = [
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
            ]
            if hooks_on:
                lines += self._hook_install_lines()
            lines += self._otel_install_lines()
            return "\n".join(lines)

        # Managed mode (default).
        lines = [
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
        ]
        if hooks_on:
            lines += self._hook_install_lines()
        lines += self._otel_install_lines()
        return "\n".join(lines)

    def _hook_install_lines(self) -> list[str]:
        """Install notes for the hook-telemetry extension (when emitted)."""
        return [
            "",
            "Hook telemetry (built into the databricks-auth.ts plugin when emitted):",
            "  The plugin extension spools tool events locally and flushes them to Zerobus",
            "  REST at session end. It uses a SECOND bearer token cache (distinct from the",
            "  gateway token): a separate telemetry service principal, down-scoped to the",
            "  hook_events UC table via authorization_details.",
            "  UPSTREAM LIMITATION: opencode dispatches event hooks as void hook[...] — the",
            "  Promise is NOT awaited by the runtime. Telemetry delivery is best-effort: a",
            "  batch interrupted by an immediate process exit is recovered on the NEXT session",
            "  start (the plugin renames orphaned .sending.<pid> files back to .jsonl and",
            "  retries them), not delivered synchronously at exit. Events are not permanently",
            "  lost unless the spool dir (~/.cache/unity-gateway/opencode-spool/) is cleared.",
            "  Preconditions:",
            "    1. The developer must hold READ_SECRET on the telemetry UC secret",
            "       (the same one the OTEL helper uses; grant via telemetry_reader_groups).",
            "    2. The second token uses a DISTINCT TELEMETRY SERVICE PRINCIPAL — not the",
            "       same SP as the gateway auth, and not just a different audience on the",
            "       same SP. Provision it separately and store its client_id/client_secret",
            "       in the UC secret (effective_value JSON with client_id + client_secret).",
            "    3. The Zerobus REST endpoint must be reachable from the developer's machine.",
            "  Verify: run opencode, execute a tool, then check the hook_events UC table",
            "  for a row with agent='opencode'. The plugin logs mint failures to the",
            "  opencode console; check there if no rows appear.",
        ]

    def _otel_install_lines(self) -> list[str]:
        """Install notes for native OTEL (launch-env recipe, always documented)."""
        tables: dict[str, str] = getattr(self, "_tel_tables", {})

        def _table(sig: str) -> str:
            return tables.get(sig, f"<otel-{sig}-table>")

        # Per-signal *_HEADERS carry X-Databricks-UC-Table-Name for routing; the
        # generic OTEL_EXPORTER_OTLP_HEADERS carries the shared static bearer.
        # Mirrors the claude_code.py split: content-type + table in static env,
        # Authorization in a helper (here: static bearer + env rotation).
        signal_lines = []
        for sig in ("metrics", "logs", "traces"):
            env_key = f"OTEL_EXPORTER_OTLP_{sig.upper()}_HEADERS"
            signal_lines.append(
                f"    export {env_key}="
                f"'content-type=application/x-protobuf,"
                f"X-Databricks-UC-Table-Name={_table(sig)}'"
            )

        return [
            "",
            "Native OTEL (launch-environment recipe — NOT emitted in opencode.json):",
            "  opencode has no config key for OTEL; set these in the launch environment.",
            "  A static bearer in OTEL_EXPORTER_OTLP_HEADERS will expire; refresh it via",
            "  a wrapper script or launchd/systemd EnvironmentFile rotation.",
            "    export OTEL_EXPORTER_OTLP_ENDPOINT=<workspace>/api/2.0/otel",
            "    export OTEL_LOGS_EXPORTER=otlp",
            "    export OTEL_TRACES_EXPORTER=otlp",
            "    # Generic bearer (merged with per-signal headers by the OTEL SDK):",
            "    export OTEL_EXPORTER_OTLP_HEADERS='Authorization=Bearer <static-bearer>'",
            "    # Per-signal headers carry X-Databricks-UC-Table-Name for table routing:",
        ] + signal_lines + [
            "  Verify: open opencode after setting these; check the OTEL UC tables for rows.",
        ]
