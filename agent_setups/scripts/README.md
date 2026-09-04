# agent_setups/scripts

This is the entrypoint that **generates coding-agent configs** from the deployed
Unity AI Gateway. It reads the Terraform outputs of `terraform/infra` — the model
services you provisioned. It then emits opinionated, deployable config for a
coding agent.

**Supported agents: Claude Code** (`managed-settings.json` for MDM/fleet
deployment), **Claude Desktop** (an importable config plus per-OS helper
scripts), **Codex** (`config.toml` routed through the gateway's MLflow serving
route, `mlflow/v1/responses`), and the **DeepSeek Harness** (a home patch plus a
token-refresh plugin). The design is a registry. You can add other agents as new
generators.

The generator covers only what a fleet baseline must carry: inference routing and
telemetry. It does not register MCP servers and it does not configure agents that
`ug` already configures. See [Division of labour with `ug`](../../README.md#division-of-labour-the-generator-and-ug) in the repo README.

## How it works

```
terraform/infra  ──terraform output -json──▶  gateway.py (GatewayContext)
                                                    │
                                                    ▼
                                          agents/<agent>.py  ──▶  generated/<agent>/…
```

Each endpoint from the `endpoints` output carries its three-level UC name
(`catalog.schema.endpoint`). This is exactly the string Claude Code needs as a
model pin. The generator writes those names into the agent's config.

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

Output lands in `agent_setups/generated/<agent>/…`. This path is gitignored,
because it embeds a workspace host. Regenerate it per workspace. Claude Code writes
a **self-contained bundle per OS**: `claude-code/{macos,linux,windows}/`. Each
bundle has a `managed-settings.json` plus the `otel-headers-helper.sh` and
`emit_hook_events.sh` scripts (when enabled). The bundles differ only in the
on-disk paths that `managed-settings.json` references, keyed to each OS's
ClaudeCode dir. The scripts are byte-identical. Codex writes a single
`codex/config.toml`.

Or via the repo Makefile: `make agent-claude-code PROFILE=fevm-west` /
`make agent-codex PROFILE=fevm-west` (append `-preview` to print without writing).

## What the Claude Code config encodes

- **Routing:** `ANTHROPIC_BASE_URL = <host>/ai-gateway/anthropic`. Claude Code
  posts the Anthropic Messages API there.
- **Auth:** `apiKeyHelper` mints a fresh U2M OAuth token
  (`databricks auth token --force-refresh`) and honors a `DATABRICKS_BEARER`
  override. The cache TTL is 15 minutes.
- **Model pins:** the tier env vars point at three-level gateway names —
  `opus`→`claude-opus`, `sonnet`→`claude-sonnet`, `fable`→`claude-fable`, and
  `haiku`→**`claude-haiku-4-5`** (pinned, because Claude Code hardcodes haiku-4-5
  patterns). These derive straight from our Terraform endpoints, so they cost
  nothing to keep current — just regenerate.
- **Context window:** the `opus` and `sonnet` families default to **1M context**
  (the `[1m]` suffix, for example `…claude-opus[1m]`, which Claude Code strips
  before the gateway call). `--small-context` reverts to native windows.
  Haiku/Fable always use their native window.
- **Model discovery:** `availableModels` is **every** deployed endpoint — across
  all provider schemas — that exposes the Anthropic API (`anthropic/v1/messages`).
  The generator discovers it live per workspace via a GET on each model service.
  This includes Anthropic and any polyglot model (for example Gemini, where the
  gateway exposes the Anthropic surface), and excludes OpenAI-only endpoints.
  `--skip-api-discovery` falls back to a schema heuristic for offline use.
- **Governance:** `enforceAvailableModels` + `availableModels`, and
  `permissions.deny: ["WebSearch"]` (built-in search cannot reach
  api.anthropic.com through the gateway, so replace it with `ug mcp web-search`,
  the gateway-backed `web_search` MCP).
- **Model picker (opt-in, `--model-picker`):** `availableModels` is only an
  allow-list. It does **not** add rows to the interactive `/model` picker, which
  otherwise shows just the four tier slots. `--model-picker` emits a `modelPicker`
  (`{ options: [{model, label, description}], replaceBuiltInOptions }`, Claude Code
  v2.1.242+) that lists every Anthropic-capable endpoint (aliases first, then version
  pins). It replaces the built-in tier rows by default. `--model-picker-append`
  keeps them and appends instead.

  It is **off by default** on purpose. This is an MDM-pushed file that lands on
  every machine, so the baseline stays the minimal config that works on the widest
  range of client versions:
  1. **Version floor.** `modelPicker` needs Claude Code v2.1.242+. If it defaulted
     on, it would push a setting that older installs in a fleet may not understand.
  2. **Not part of governance.** The four tier pins plus
     `enforceAvailableModels`/`availableModels` already define and enforce which
     models are usable. The picker only changes what `/model` *displays* — a UX
     nicety, not a governance control.
  3. **It is a UI opinion.** By default it *replaces* the familiar built-in tier
     rows, which is a bigger change to impose fleet-wide without being asked.

  So it is a deliberate opt-in that you enable once your fleet is current. (Note: the
  Docker harness does **not** enable it either, so the container mirrors a default
  deploy. Pass `make docker-config ARGS="--model-picker"` to exercise it there.)
- **Telemetry:** when the infra `telemetry` output is present (default), the
  generator adds the OTEL env block (metrics/logs/traces → `<host>/api/2.0/otel`),
  per-signal `X-Databricks-UC-Table-Name` static headers, and an
  `otelHeadersHelper` pointing at a generated `otel-headers-helper.sh`. That
  helper reads the telemetry UC secret **as the developer** and mints the
  ingestion service principal's OAuth token for the `Authorization` header, so
  the bearer token is never baked into settings. Prompt/tool/API-body content
  logging is **off** unless you pass `--otel-log-content`.
- **Hook telemetry (custom reporting):** native OTEL does not emit the per-hook
  signals that internal teams report — slash-command / skill / subagent usage with
  plugin attribution, per-session plugin inventory, `StopFailure` mid-stream
  stalls, guardrail hits, workflow adoption. When the infra `telemetry.hook_events`
  table is present (default), the generator emits `emit_hook_events.sh` and a
  `hooks` block that wires it to the relevant events. The generator
  **auto-derives the Zerobus endpoint at generation time** from workspace metadata —
  the numeric workspace id (`x-databricks-org-id` response header) + the UC metastore
  region + the host's cloud suffix → `https://<id>.zerobus.<region>.<suffix>` — and
  bakes it into the script (override via `--zerobus-endpoint` or
  `telemetry_zerobus_endpoint`). `--skip-api-discovery` skips the derivation.
  If the generator cannot derive it, the hook still ships but stays dormant (no-op)
  until `ZEROBUS_ENDPOINT` is set.
  **Delivery uses a spool-then-flush model**, so the per-tool-call hot path never
  blocks and nothing depends on a backgrounded process surviving. Each producer hook
  **appends** its event to a per-session spool file (instant, local — no network).
  A **`flush`** batches the spool into one **Zerobus REST** insert at turn/session
  boundaries (`Stop`, `StopFailure`, `SubagentStop`, `SessionEnd`, and
  `SessionStart` to sweep leftovers). The flush is synchronous — but off the hot
  path and batched — and authenticates as the **same telemetry service principal**
  (bearer minted from the UC secret and cached, no SDK/gRPC). The spool is
  persistent, so an interrupted flush loses nothing — the next flush retries it
  (at-least-once, so dedupe downstream on `event_id`). The hook attributes each
  event to the developer's **workspace identity** — their `databricks current-user`
  email (for example `tanner.wendland@databricks.com`), resolved once and cached,
  not the OS login (override with `HOOK_EVENTS_USER`, which falls back to the OS
  user if the lookup fails). It is **report-only** (never blocks a tool call) and
  **content-free by default** (names/counts/IDs, not prompt or file content).
  `--hook-log-paths` includes paths. `--hook-categories` selects the categories.
  The adoption doc-matcher is `--hook-doc-patterns` (generalized from the internal
  `TESTING.md`).

## Key options (`claude-code`)

| Flag | Default | Purpose |
|---|---|---|
| `--profile` | `fevm-west` | Databricks profile (host + auth). |
| `--host` | (from profile) | Override the workspace URL. |
| `--skip-api-discovery` | off | Skip live `supported_api_types` lookup. Use `--fallback-schema` instead (offline). |
| `--fallback-schema` | `anthropic` | Schema assumed Anthropic-capable when discovery is skipped. |
| `--default-tier` | `sonnet` | Tier Claude Code starts on. |
| `--small-context` | off | Use native context windows. The default gives opus/sonnet the `[1m]` (1M) suffix. |
| `--lock-models` | `catalog` | `catalog` (all Anthropic-capable endpoints, enforced) · `aliases` (aliases only) · `none`. |
| `--allow-websearch` | off | Keep the built-in WebSearch tool. |
| `--declare-capabilities` | off | Emit per-tier `_NAME`/`_SUPPORTED_CAPABILITIES` env vars. Off by default — a drift-prone surface that mirrors model facts we do not own. Enable it only if effort/thinking toggles do not appear on their own. |
| `--model-picker` | off | Emit a `modelPicker` listing every Anthropic-capable endpoint in the `/model` picker (v2.1.242+). |
| `--model-picker-append` | off | With `--model-picker`, append to the built-in tier rows instead of replacing them. |
| `--api-key-ttl-ms` | `900000` | apiKeyHelper cache TTL. |
| `--databricks-bin` | `databricks` | CLI path (use absolute for launchd/MDM). |
| `--ssl-cert-file` | – | Per-machine CA bundle (`SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`). |
| `--required-min-version` | – | Enforce a Claude Code version floor. |
| `--user-config` | off | Emit a per-user `settings.json` bundle (for `~/.claude/`) instead of the per-OS managed bundle. See "User (local, non-managed)" below. |
| `--telemetry` | `auto` | OTEL export: `auto` (on iff the `telemetry` output exists) · `on` (require it) · `off`. |
| `--otel-log-content` | off | Also log prompts, tool details/content, and raw API bodies. Privacy-sensitive. |
| `--otel-metric-interval-ms` | `60000` | `OTEL_METRIC_EXPORT_INTERVAL`. |
| `--otel-logs-interval-ms` | `5000` | `OTEL_LOGS_EXPORT_INTERVAL`. |
| `--otel-headers-helper-debounce-ms` | `900000` | Token refresh interval for the headers helper. |
| `--platforms` | `macos,linux,windows` | OSes to emit a self-contained bundle for. Each `claude-code/<platform>/` gets paths keyed to that OS's ClaudeCode dir. |
| `--hook-telemetry` | `auto` | Custom reporting hook + `hooks` block via Zerobus REST: `auto` (on iff the `telemetry.hook_events` table exists) · `on` (require it) · `off`. Endpoint baked when known but not required — the hook ships dormant until `ZEROBUS_ENDPOINT` is set. |
| `--hook-categories` | all four | Comma list of `usage,reliability,governance,adoption`. The generator registers only the selected categories' hook events. |
| `--hook-doc-patterns` | `TESTING\.md` | grep -E of file basenames whose Read counts as a workflow-adoption event. |
| `--hook-log-paths` | off | Include full file paths in adoption events (default: basename only). |
| `--hook-token-ttl-seconds` | `600` | Refresh-hint TTL for the cached Zerobus bearer. |
| `--zerobus-endpoint` | (auto-derived) | Override the Zerobus REST base URL. Default: TF output, else auto-derived from workspace metadata (org-id header + metastore region). |

## Deploying the output

**`agent_setups/deploy/install.sh` handles all file placement** — it is the single
placement authority for both agents. Do not copy files by hand. The deployment
workflow is:

```bash
# 0. Generate the bundles (requires Terraform outputs + network access).
#    Codex must be generated in managed mode (default; do NOT pass --user-config).
make agent-claude-code   # → claude-code/{macos,linux,windows}/ per-OS bundles
make agent-codex         # → codex/etc/ managed bundle

# 1. Build per-OS tarballs (includes install.sh + runbooks + VERSION).
#    deploy-package hard-errors if a claude-code bundle or managed codex bundle is absent.
make deploy-package

# 2. Distribute and run on each machine (see MDM runbooks below).
#    install.sh places files with correct modes and writes a version marker.
```

MDM runbooks for fleet deployment:

- **macOS (Jamf):** `agent_setups/deploy/runbooks/jamf.md`
- **Linux/servers (Ansible):** `agent_setups/deploy/runbooks/ansible.md`

The path matrix for reference (install.sh encodes this, so no manual path
construction is needed):

| OS | `claude-code/<os>/` bundle installs to |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/` |
| Linux/WSL | `/etc/claude-code/` |
| Windows | `C:\Program Files\ClaudeCode\` |

**Two-phase auth:** `install.sh` handles Phase A (root config placement) only.
Each developer must run Phase B once — `databricks auth login --host <url>
--profile <profile>` — interactively. This is a permanent boundary. Browser OAuth
(U2M) cannot be pushed. Verify with `/status` in Claude Code.

**User (local, non-managed):** for a per-user install without root or MDM, run
`make claude-code-install-local`. It generates a user-mode bundle
(`--user-config`) and installs `settings.json` (plus any helper scripts) to
`$HOME/.claude/`. The placement script
`agent_setups/deploy/install-claude-code-local.sh` does the copy. It saves a
timestamped backup of any existing `settings.json` first. The user-mode
`settings.json` accepts the same keys as `managed-settings.json`, except
`requiredMinimumVersion` (managed-only), which the generator drops. The generator
bakes the helper paths (`otelHeadersHelper`, the hook command) as absolute
`~/.claude` paths, so you must generate and install on the same machine. This mode
has no enforcement. Run the script directly with `--dry-run` to preview,
`--target-dir <dir>` to write elsewhere, or `--no-backup` to overwrite in place.

This `managed-settings.json` is the **inference baseline**. With it deployed, a
direct `claude` call routes through the gateway and emits telemetry on its own. The
**intended launch surface is `ug`**, which layers Databricks MCP discovery and a
per-request OAuth surface on top — see the repo
[README](../../README.md#launching-agents-ug-is-the-intended-entrypoint).

- **OTEL helper** (`otel-headers-helper.sh`, when telemetry is on): needs `python3`
  + the `databricks` CLI on PATH, and `READ_SECRET` on the telemetry UC secret
  (grant a group via `telemetry_reader_groups`).
- **Reporting hook** (`emit_hook_events.sh`, when hook telemetry is on): same deps
  plus `jq` + `curl`. It reuses the same UC secret / `READ_SECRET` grant. Confirm the
  **Zerobus REST API is available in the workspace region** before fleet rollout.
- **Windows** runs the `.sh` helper/hook through Claude Code's shell (Git Bash).
  Verify the `C:\` paths resolve there before a Windows rollout.

## Codex (`codex`)

The generator emits an **enforced, root-owned `/etc/codex` bundle** by default (the
Codex analogue of Claude Code's managed settings), or a per-user `$CODEX_HOME`
bundle with `--user-config`.

Codex reads three system-level files under `/etc/codex` — `config.toml`,
`managed_config.toml`, and `requirements.toml` (verified against codex-cli 0.150.1).
**`managed_config.toml` overrides each user's `~/.codex/config.toml`** (confirmed
empirically: a managed `model`/`base_url` wins over the user's), so it enforces
gateway routing and the default model/provider fleet-wide. `requirements.toml`
carries the enforcement policy. This tool has no cloud MDM push — deploy the
bundle to `/etc/codex/` on each machine via your MDM / config-management, the same
way you push `managed-settings.json` for Claude Code.

The generator writes the managed bundle to `codex/etc/`:

| File | Role |
|---|---|
| `managed_config.toml` | Gateway routing + default model/provider + inline `[hooks]`. Overrides user config. |
| `requirements.toml` | Enforcement policy (`allow_managed_hooks_only = true` plus a commented model/provider-lock stub). |
| `emit_hook_events.sh` | The hook dispatcher, invoked by absolute path `/etc/codex/emit_hook_events.sh`. |

With `--user-config` it instead emits the non-enforced per-user layout
(`codex/config.toml` [+ `hooks.json` + `emit_hook_events.sh`] for `$CODEX_HOME`) —
useful for laptops without root, or to overlay the gateway provider on an existing
(for example ChatGPT-app) `config.toml` via `codex -p <name>`.

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
  `databricks auth token --force-refresh`. The generator keeps it inline so the
  whole setup is one file (no helper script to deploy alongside). Codex re-runs it
  every `refresh_interval_ms`.
- **Model surface:** every endpoint exposing the chosen `--api-type` becomes a
  switchable model (listed as a comment — switch with `codex -m <full-name>`). The
  default `mlflow/v1/responses` is the broad Responses surface served by the MLflow
  route, so GPT, Gemini, Claude, and the open models are all reachable. Narrow to
  `openai/v1/responses` for OpenAI-native only.
- **Not included — the ChatGPT desktop app.** A working local Codex install also
  carries app machinery (plugins, marketplaces, `node_repl`, computer-use,
  `CODEX_CLI_PATH`). The ChatGPT app installs that machinery, and it is
  machine-specific. The app need not even run for CLI gateway use, so this generator
  reproduces none of it.

### Key options (`codex`)

| Flag | Default | Purpose |
|---|---|---|
| `--profile` | `fevm-west` | Databricks profile (host + auth). |
| `--host` | (from profile) | Override the workspace URL. |
| `--api-type` | `mlflow/v1/responses` | Endpoint filter. Narrow to `openai/v1/responses` for OpenAI-native responses only. |
| `--skip-api-discovery` | off | Skip live `supported_api_types` lookup. Use `--fallback-schema` (offline). |
| `--fallback-schema` | `openai` | Schema assumed responses-capable when discovery is skipped. |
| `--default-model` | `gpt` alias | Model Codex starts on (endpoint leaf or full UC name). |
| `--reasoning-effort` | `high` | `model_reasoning_effort` (`minimal`…`xhigh`). |
| `--provider-name` | `databricks` | Key for `[model_providers.<name>]` / `model_provider`. |
| `--gateway-path` | `/ai-gateway/mlflow/v1` | Gateway route base appended to the host. Codex appends `/responses`. Override to route elsewhere (for example `/ai-gateway/codex/v1`). |
| `--refresh-interval-ms` | `900000` | `auth.refresh_interval_ms` (token re-mint interval). |
| `--auth-timeout-ms` | `5000` | `auth.timeout_ms`. |
| `--databricks-bin` | `databricks` | CLI path used in the auth command (absolute for minimal-PATH contexts). |
| `--user-config` | off | Emit the per-user `$CODEX_HOME` bundle instead of the default enforced `/etc/codex` bundle. |
| `--hook-telemetry` | `auto` | Emit the hook telemetry (managed `[hooks]` in `managed_config.toml`, or `hooks.json` with `--user-config`) that streams reporting events via Zerobus REST. `auto` = on iff the Terraform `telemetry.hook_events` table exists. `on` requires it. `off` skips. |
| `--hook-categories` | `usage,governance,adoption` | Reporting categories to configure. (No `reliability` — Codex has no error/failure hook.) |
| `--hook-token-ttl-seconds` | `600` | Refresh-hint TTL for the cached Zerobus bearer. |
| `--hook-script-path` | `/etc/codex/emit_hook_events.sh` (managed) · `${CODEX_HOME:-$HOME/.codex}/emit_hook_events.sh` (`--user-config`) | Path the hook command invokes the emitter from. |
| `--zerobus-endpoint` | (auto-derived) | Override the Zerobus REST base URL (else the TF output, else derived from workspace metadata). |

### Deploying the output

**`agent_setups/deploy/install.sh` handles all file placement** — it is the single
placement authority for both agents. Do not copy files by hand. The deployment
workflow is:

```bash
# 1. Build per-OS tarballs.
make deploy-package

# 2. Distribute and run on each machine (see MDM runbooks below).
#    install.sh detects managed vs. user-mode Codex and places files accordingly.
```

MDM runbooks for fleet deployment:

- **macOS (Jamf):** `agent_setups/deploy/runbooks/jamf.md`
- **Linux/servers (Ansible):** `agent_setups/deploy/runbooks/ansible.md`

**Managed (default):** `install.sh` places the `codex/etc/` bundle into **`/etc/codex/`**,
root-owned, with correct modes (644 configs / 755 scripts). `managed_config.toml`
overrides each user's `~/.codex/config.toml`, so it enforces routing and the default
model/provider fleet-wide. Confirm the parse + effective provider with
`codex --strict-config doctor`.

**User (local, non-managed):** for a per-user install without root or MDM, run
`make codex-install-local`. It generates a user-mode bundle (`--user-config`) and
installs `config.toml` (plus `hooks.json` + `emit_hook_events.sh` when hook
telemetry is on) to `${CODEX_HOME:-$HOME/.codex}/`. The placement script
`agent_setups/deploy/install-codex-local.sh` does the copy. It saves a timestamped
backup of any existing `config.toml` first. Codex user hooks require per-user
trust, so trust them in Codex (or launch with `--dangerously-bypass-hook-trust`)
for the reporting hooks to run. Run the script directly with `--dry-run` to
preview, `--target-dir <dir>` to write elsewhere, or `--no-backup` to overwrite in
place.

To overlay the gateway provider on an existing (for example ChatGPT-app)
`config.toml` instead, copy `codex/config.toml` to
`$CODEX_HOME/databricks.config.toml` and launch with `codex -p databricks`.

**Two-phase auth:** `install.sh` handles Phase A (root config placement) only.
Either way, each developer runs `databricks auth login --host <url> --profile <profile>`
once, interactively — browser OAuth (U2M), cannot be pushed. `python3` + the
`databricks` CLI must be on PATH. As with Claude Code, the intended launch surface
is `ug` (`ug codex`), which adds MCP discovery and the per-request OAuth
surface — see the repo
[README](../../README.md#launching-agents-ug-is-the-intended-entrypoint).

### OTEL telemetry — none client-side, by design

The generator emits **no `[otel]` block**. Instead, each model service's
**inference-logging** UC Delta table captures Codex traffic **server-side** (the
Terraform `inference_table` per endpoint) — the same data plane, with no client
dependency. This is a deliberate choice, not a gap:

- Codex's `[otel]` exporter takes only **static headers** with `${ENV_VAR}`
  interpolation resolved once at process start. There is no headers *command* like
  Claude Code's `otelHeadersHelper`, so a launch-minted OAuth token would expire
  mid-session (SP M2M tokens ~1h) with no way to refresh.
- `ug` (the intended launch surface) ships **no OTEL forwarder** either — it
  treats Codex telemetry the same way.

A refresh-safe client-OTEL path would require a local forwarder that injects a
fresh token per request (as the separate `databricks-agents` Codex wrapper does),
which is out of scope for a static config generator. If you want best-effort client
spans anyway, mint an OAuth token into an env var at launch and add an `[otel]`
block referencing it — accepting the ~1h token-TTL limitation.

### Hook-event telemetry (custom reporting events)

Separately from OTEL, Codex ships a **`[hooks]` system** that is a near-clone of
Claude Code's (stdin-JSON in, stdout-JSON out, a regex `matcher` on the tool name,
the same `snake_case` payload fields). So when `telemetry.hook_events` is deployed,
the generator wires it — as inline `[hooks]` in `managed_config.toml` (managed
default) or a standalone `hooks.json` (`--user-config`) plus the `emit_hook_events.sh`
dispatcher — and reuses the **same** UC table, service principal, and secret as the
Claude Code hook. Events stream to Zerobus REST. It is **report-only** (never blocks a
tool call).

Three of Claude Code's four categories map cleanly:

| Category | Codex event | Emits |
|---|---|---|
| `governance` | `PreToolUse` (matcher `^(Bash\|shell\|local_shell\|exec_command\|unified_exec)$`) | `command_flagged` (risk-pattern), `secret_detected` |
| `adoption` | `PostToolUse` (same matcher) | `pr_pushed` |
| `usage` | `SubagentStart` | `subagent_used` |
| *(delivery)* | `SessionStart`/`Stop`/`SubagentStop`/`SessionEnd` | batched spool flush |

The matcher covers every shell-exec tool name in the codex-cli 0.150.1 binary — the
runtime tool is **`shell`** (137 refs), not `Bash` (1, a compat alias), so a `^Bash$`
matcher would have matched nothing.

**Not ported:** `reliability` (`stop_failure`) — Codex has **no error/failure
hook**. Turn failures surface only in `codex exec --json`, not the interactive TUI.
The generator also drops the Claude-only tool signals `skill_used` (no Skill tool)
and `doc_read` (no Read tool surfaced to hooks).

**Enforcement:** in the managed bundle the hooks live in `managed_config.toml`
(invoked by absolute path `/etc/codex/emit_hook_events.sh` — no shell-expansion
ambiguity), and `requirements.toml` sets **`allow_managed_hooks_only = true`** so a
user cannot disable or replace them. (Managed hooks are trusted. User hooks otherwise
require per-user trust or `--dangerously-bypass-hook-trust`.) Runtime needs `jq` +
`curl` + `python3` + the `databricks` CLI on PATH, and `READ_SECRET` on the telemetry
UC secret. No new Terraform — Claude Code shares the table/SP/secret/grants from the
`telemetry` module.

`requirements.toml` also ships a **commented model/provider-lock stub**.
`managed_config.toml`'s override already enforces the routing lock, and the deeper
`[models]` (`ModelsRequirementsToml`) schema is not pinned for this Codex version. A
wrong shape makes Codex fail to load config entirely, so validate any additions with
`codex --strict-config doctor` before you enable them.

## Division of labour with `ug`

The generator and `ug` cover different jobs. Keep new work on the correct side of
the line. The repo README holds the full table — see [Division of labour](../../README.md#division-of-labour-the-generator-and-ug).

In short, the generator owns the fleet baseline that must work before a developer
installs anything: inference routing, model pins, the allow-list, OTEL export, and
hook events. `ug` owns the developer's own machine: launch, per-request OAuth, MCP
registration, and skills.

Two rules follow from that split:

1. Do not add MCP registration to a generator. `ug mcp add` does this for seven
   agents, and it also removes stale servers. A second implementation would
   compete with it.
2. Do not add an agent that `ug` already configures, unless the agent needs a
   fleet-managed file that `ug` cannot deliver. `ug` configures Claude Code,
   Codex, Gemini CLI, OpenCode, Copilot CLI, Pi, and Cursor. Claude Code and
   Codex stay here because both read a root-owned managed file that an MDM tool
   must push. Claude Desktop and the DeepSeek Harness stay here because `ug`
   does not support either one.

## Adding an agent

1. Create `agents/<agent>.py` with a class subclassing `AgentGenerator`
   (`name`, `add_arguments`, `generate`).
2. Register it in `agents/__init__.py`.

Requires Python 3.10+ (stdlib only) and the `databricks` CLI on PATH.
