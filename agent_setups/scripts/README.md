# agent_setups/scripts

The entrypoint for **generating coding-agent configs** from the deployed Unity AI
Gateway. It reads the Terraform outputs of `terraform/infra` (the model services
you provisioned) and emits opinionated, ready-to-deploy config for a coding agent.

**Supported agents: Claude Code** (`managed-settings.json` for MDM/fleet
deployment) **and Codex** (`config.toml` routed through the gateway's MLflow
serving route — `mlflow/v1/responses`). The design is a registry, so Gemini CLI,
OpenCode, etc. can be added as new generators.

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

# Generate a Codex config.toml (gateway routing).
./generate.py codex --profile fevm-west

# Preview without writing.
./generate.py claude-code --profile fevm-west --stdout

# Without invoking terraform (use a saved output + explicit host).
terraform -chdir=../../terraform/infra output -json > /tmp/tf.json
./generate.py claude-code --tf-output-json /tmp/tf.json --host https://myws.cloud.databricks.com
```

Output lands in `agent_setups/generated/<agent>/…`
(`claude-code/managed-settings.json`, `codex/config.toml`) — gitignored, since it
embeds a workspace host; regenerate per workspace.

Or via the repo Makefile: `make agent-claude-code PROFILE=fevm-west` /
`make agent-codex PROFILE=fevm-west` (append `-preview` to print without writing).

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

  It's **off by default** on purpose — this is an MDM-pushed file that lands on
  every machine, so the baseline stays the minimal config that works on the widest
  range of client versions:
  1. **Version floor.** `modelPicker` needs Claude Code v2.1.242+; defaulting it on
     would push a setting older installs in a fleet may not understand.
  2. **Not part of governance.** The four tier pins plus
     `enforceAvailableModels`/`availableModels` already define and enforce which
     models are usable. The picker only changes what `/model` *displays* — a UX
     nicety, not a governance control.
  3. **It's a UI opinion.** By default it *replaces* the familiar built-in tier
     rows, which is a bigger change to impose fleet-wide without being asked.

  So it's a deliberate opt-in you enable once your fleet is current. (Note: the
  Docker harness does **not** enable it either, so the container mirrors a default
  deploy; pass `make docker-config ARGS="--model-picker"` to exercise it there.)
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

## Codex (`codex`)

Emits a single self-contained `codex/config.toml` that routes the Codex CLI
through the gateway. Unlike Claude Code, Codex has **no OS-level managed-config
path** — it reads `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`) per
user — so the file is deployed into a developer's `$CODEX_HOME`, not pushed to a
system path via MDM.

### What the Codex config encodes

- **Routing:** a `[model_providers.<name>]` block with
  `base_url = <host>/ai-gateway/mlflow/v1`, `wire_api = "responses"`, and
  `supports_websockets = false`. Codex appends `/responses` to `base_url`, so it
  lands on `mlflow/v1/responses` — the MLflow serving route is the actual
  model-inference surface. `model_provider` points at it and `model` pins a default
  endpoint (the `gpt` alias by default).
- **Auth:** an inline `[model_providers.<name>.auth]` command
  (`command = "bash"`, `args = ["-c", …]`) that prints a **bare** short-lived
  Databricks OAuth token — honoring `$DATABRICKS_BEARER`, else minting via
  `databricks auth token --force-refresh`. Kept inline so the whole setup is one
  file (no helper script to deploy alongside). Codex re-runs it every
  `refresh_interval_ms`.
- **Model surface:** every endpoint exposing the chosen `--api-type` becomes a
  switchable model (listed as a comment; switch with `codex -m <full-name>`). The
  default `mlflow/v1/responses` is the broad Responses surface served by the MLflow
  route, so GPT, Gemini, Claude, and the open models are all reachable. Narrow to
  `openai/v1/responses` for OpenAI-native only.
- **Not included — the ChatGPT desktop app.** A working local Codex install also
  carries app machinery (plugins, marketplaces, `node_repl`, computer-use,
  `CODEX_CLI_PATH`). That is installed by the ChatGPT app and is machine-specific;
  the app need not even run for CLI gateway use, so none of it is reproduced here.

### Key options (`codex`)

| Flag | Default | Purpose |
|---|---|---|
| `--profile` | `fevm-west` | Databricks profile (host + auth). |
| `--host` | (from profile) | Override the workspace URL. |
| `--api-type` | `mlflow/v1/responses` | Endpoint filter; narrow to `openai/v1/responses` for OpenAI-native responses only. |
| `--skip-api-discovery` | off | Skip live `supported_api_types` lookup; use `--fallback-schema` (offline). |
| `--fallback-schema` | `openai` | Schema assumed responses-capable when discovery is skipped. |
| `--default-model` | `gpt` alias | Model Codex starts on (endpoint leaf or full UC name). |
| `--reasoning-effort` | `high` | `model_reasoning_effort` (`minimal`…`xhigh`). |
| `--provider-name` | `databricks` | Key for `[model_providers.<name>]` / `model_provider`. |
| `--gateway-path` | `/ai-gateway/mlflow/v1` | Gateway route base appended to the host; Codex appends `/responses`. Override to route elsewhere (e.g. `/ai-gateway/codex/v1`). |
| `--refresh-interval-ms` | `900000` | `auth.refresh_interval_ms` (token re-mint interval). |
| `--auth-timeout-ms` | `5000` | `auth.timeout_ms`. |
| `--databricks-bin` | `databricks` | CLI path used in the auth command (absolute for minimal-PATH contexts). |

### Deploying the output

Copy `codex/config.toml` into a developer's `$CODEX_HOME`, one of two ways:

- **Full config:** `→ $CODEX_HOME/config.toml` (default `~/.codex/config.toml`).
- **Non-destructive overlay:** `→ $CODEX_HOME/databricks.config.toml`, then launch
  with `codex -p databricks` — layers the gateway provider on top of an existing
  (e.g. ChatGPT-app) `config.toml`.

Each developer runs `databricks auth login --host <url> --profile <profile>` once;
`python3` + the `databricks` CLI must be on PATH. Verify with `codex doctor`. As
with Claude Code, the intended launch surface is `ucode` (`ucode codex`), which
adds MCP discovery and the per-request OAuth surface — see the repo
[README](../../README.md#launching-agents-ucode-is-the-intended-entrypoint).

### Telemetry — none client-side, by design

The generator emits **no `[otel]` block**. Codex traffic is instead captured
**server-side** by each model service's **inference-logging** UC Delta table (the
Terraform `inference_table` per endpoint) — the same data plane, with no client
dependency. This is a deliberate choice, not a gap:

- Codex's `[otel]` exporter takes only **static headers** with `${ENV_VAR}`
  interpolation resolved once at process start — there's no headers *command* like
  Claude Code's `otelHeadersHelper`, so a launch-minted OAuth token would expire
  mid-session (SP M2M tokens ~1h) with no way to refresh.
- `ucode` (the intended launch surface) ships **no OTEL forwarder** either — it
  treats Codex telemetry the same way.

A refresh-safe client-OTEL path would require a local forwarder that injects a
fresh token per request (as the separate `databricks-agents` Codex wrapper does),
which is out of scope for a static config generator. If you want best-effort client
spans anyway, mint an OAuth token into an env var at launch and add an `[otel]`
block referencing it — accepting the ~1h token-TTL limitation.

## Adding an agent

1. Create `agents/<agent>.py` with a class subclassing `AgentGenerator`
   (`name`, `add_arguments`, `generate`).
2. Register it in `agents/__init__.py`.

Requires Python 3.10+ (stdlib only) and the `databricks` CLI on PATH.
