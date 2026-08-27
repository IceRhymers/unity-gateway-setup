# agent_setups/scripts

The entrypoint for **generating coding-agent configs** from the deployed Unity AI
Gateway. It reads the Terraform outputs of `terraform/infra` (the model services
you provisioned) and emits opinionated, ready-to-deploy config for a coding agent.

**First supported agent: Claude Code** (`managed-settings.json` for MDM/fleet
deployment). The design is a registry, so Codex, Gemini CLI, OpenCode, etc. can
be added as new generators.

## How it works

```
terraform/infra  ──terraform output -json──▶  gateway.py (GatewayContext)
                                                    │
                                                    ▼
                                          agents/<agent>.py  ──▶  generated/<agent>/…
```

Each endpoint from the `endpoints` output carries its three-level UC name
(`catalog.schema.endpoint`) — exactly the string Claude Code needs as a model
pin. The generator wires those into the agent's config.

## Usage

```bash
# Generate Claude Code managed-settings.json from the applied Terraform state.
./generate.py claude-code --profile fevm-west

# Preview without writing.
./generate.py claude-code --profile fevm-west --stdout

# Without invoking terraform (use a saved output + explicit host).
terraform -chdir=../../terraform/infra output -json > /tmp/tf.json
./generate.py claude-code --tf-output-json /tmp/tf.json --host https://myws.cloud.databricks.com
```

Output lands in `agent_setups/generated/claude-code/managed-settings.json`
(gitignored — it embeds a workspace host, so regenerate per workspace).

Or via the repo Makefile: `make agent-claude-code PROFILE=fevm-west`.

## What the Claude Code config encodes

- **Routing:** `ANTHROPIC_BASE_URL = <host>/ai-gateway/anthropic`; Claude Code
  posts Anthropic Messages API there.
- **Auth:** `apiKeyHelper` mints a fresh U2M OAuth token
  (`databricks auth token --force-refresh`), honoring a `DATABRICKS_BEARER`
  override; 15-min cache TTL.
- **Model pins:** the tier env vars point at three-level gateway names —
  `opus`→`claude-opus`, `sonnet`→`claude-sonnet`, `fable`→`claude-fable`, and
  `haiku`→**`claude-haiku-4-5`** (pinned; Claude Code hardcodes haiku-4-5
  patterns). These derive straight from our Terraform endpoints, so they cost
  nothing to keep current — just regenerate.
- **Context window:** the `opus` and `sonnet` families default to **1M context**
  (the `[1m]` suffix, e.g. `…claude-opus[1m]`; Claude Code strips it before the
  gateway call). `--small-context` reverts to native windows. Haiku/Fable always
  use their native window.
- **Model discovery:** `availableModels` is **every** deployed endpoint — across
  all provider schemas — that exposes the Anthropic API (`anthropic/v1/messages`),
  discovered live per workspace via a GET on each model service. This sweeps in
  Anthropic and any polyglot model (e.g. Gemini, where the gateway exposes the
  Anthropic surface) and excludes OpenAI-only endpoints. `--skip-api-discovery`
  falls back to a schema heuristic for offline use.
- **Governance:** `enforceAvailableModels` + `availableModels`, and
  `permissions.deny: ["WebSearch"]` (built-in search can't reach
  api.anthropic.com through the gateway; replace with `ucode mcp web-search`, the
  gateway-backed `web_search` MCP).
- **Model picker (opt-in, `--model-picker`):** `availableModels` is only an
  allow-list — it does **not** add rows to the interactive `/model` picker, which
  otherwise shows just the four tier slots. `--model-picker` emits a `modelPicker`
  (`{ options: [{model, label, description}], replaceBuiltInOptions }`, Claude Code
  v2.1.242+) listing every Anthropic-capable endpoint (aliases first, then version
  pins). It replaces the built-in tier rows by default; `--model-picker-append`
  keeps them and appends instead.
- **Telemetry:** when the infra `telemetry` output is present (default), the
  generator adds the OTEL env block (metrics/logs/traces → `<host>/api/2.0/otel`),
  per-signal `X-Databricks-UC-Table-Name` static headers, and an
  `otelHeadersHelper` pointing at a generated `otel-headers-helper.sh`. That
  helper reads the telemetry UC secret **as the developer** and mints the
  ingestion service principal's OAuth token for the `Authorization` header, so
  the bearer token is never baked into settings. Prompt/tool/API-body content
  logging is **off** unless `--otel-log-content` is passed.

## Key options (`claude-code`)

| Flag | Default | Purpose |
|---|---|---|
| `--profile` | `fevm-west` | Databricks profile (host + auth). |
| `--host` | (from profile) | Override the workspace URL. |
| `--skip-api-discovery` | off | Skip live `supported_api_types` lookup; use `--fallback-schema` instead (offline). |
| `--fallback-schema` | `anthropic` | Schema assumed Anthropic-capable when discovery is skipped. |
| `--default-tier` | `sonnet` | Tier Claude Code starts on. |
| `--small-context` | off | Use native context windows; default gives opus/sonnet the `[1m]` (1M) suffix. |
| `--lock-models` | `catalog` | `catalog` (all Anthropic-capable endpoints, enforced) · `aliases` (aliases only) · `none`. |
| `--allow-websearch` | off | Keep the built-in WebSearch tool. |
| `--declare-capabilities` | off | Emit per-tier `_NAME`/`_SUPPORTED_CAPABILITIES` env vars. Off by default — a drift-prone surface that mirrors model facts we don't own; enable only if effort/thinking toggles don't appear on their own. |
| `--model-picker` | off | Emit a `modelPicker` listing every Anthropic-capable endpoint in the `/model` picker (v2.1.242+). |
| `--model-picker-append` | off | With `--model-picker`, append to the built-in tier rows instead of replacing them. |
| `--api-key-ttl-ms` | `900000` | apiKeyHelper cache TTL. |
| `--databricks-bin` | `databricks` | CLI path (use absolute for launchd/MDM). |
| `--ssl-cert-file` | – | Per-machine CA bundle (`SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`). |
| `--required-min-version` | – | Enforce a Claude Code version floor. |
| `--telemetry` | `auto` | OTEL export: `auto` (on iff the `telemetry` output exists) · `on` (require it) · `off`. |
| `--otel-log-content` | off | Also log prompts, tool details/content, and raw API bodies. Privacy-sensitive. |
| `--otel-metric-interval-ms` | `60000` | `OTEL_METRIC_EXPORT_INTERVAL`. |
| `--otel-logs-interval-ms` | `5000` | `OTEL_LOGS_EXPORT_INTERVAL`. |
| `--otel-headers-helper-debounce-ms` | `900000` | Token refresh interval for the headers helper. |
| `--otel-helper-install-path` | macOS ClaudeCode path | Where `otel-headers-helper.sh` is deployed on each machine. |

## Deploying the output

Push `managed-settings.json` to the OS path via MDM (Jamf/Intune/GPO):

| OS | Path |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux/WSL | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

Each developer runs `databricks auth login --host <url> --profile <profile>` once.
Verify with `/status` in Claude Code.

This `managed-settings.json` is the **inference baseline**: with it deployed,
`claude` invoked directly routes through the gateway and emits telemetry on its
own. The **intended launch surface is `ucode`**, which layers Databricks MCP
discovery and a per-request OAuth surface on top — see the repo
[README](../../README.md#launching-agents-ucode-is-the-intended-entrypoint).

When telemetry is enabled, also deploy the generated
`claude-code/otel-headers-helper.sh` to the path in `--otel-helper-install-path`
(default `/Library/Application Support/ClaudeCode/otel-headers-helper.sh`), make
it executable, and ensure `python3` + the `databricks` CLI are on PATH. Each
developer needs `READ_SECRET` on the telemetry UC secret (grant a group via
`telemetry_reader_groups`).

## Adding an agent

1. Create `agents/<agent>.py` with a class subclassing `AgentGenerator`
   (`name`, `add_arguments`, `generate`).
2. Register it in `agents/__init__.py`.

Requires Python 3.10+ (stdlib only) and the `databricks` CLI on PATH.
