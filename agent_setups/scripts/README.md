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
- **Governance:** `enforceAvailableModels` + `availableModels`, and
  `permissions.deny: ["WebSearch"]` (built-in search can't reach
  api.anthropic.com through the gateway; replace with a ucode web_search MCP).

## Key options (`claude-code`)

| Flag | Default | Purpose |
|---|---|---|
| `--profile` | `fevm-west` | Databricks profile (host + auth). |
| `--host` | (from profile) | Override the workspace URL. |
| `--schema` | `anthropic` | Provider schema backing this agent. |
| `--default-tier` | `sonnet` | Tier Claude Code starts on. |
| `--small-context` | off | Use native context windows; default gives opus/sonnet the `[1m]` (1M) suffix. |
| `--lock-models` | `catalog` | `catalog` (all endpoints, enforced) · `aliases` (aliases only) · `none`. |
| `--allow-websearch` | off | Keep the built-in WebSearch tool. |
| `--declare-capabilities` | off | Emit per-tier `_NAME`/`_SUPPORTED_CAPABILITIES` env vars. Off by default — a drift-prone surface that mirrors model facts we don't own; enable only if effort/thinking toggles don't appear on their own. |
| `--api-key-ttl-ms` | `900000` | apiKeyHelper cache TTL. |
| `--databricks-bin` | `databricks` | CLI path (use absolute for launchd/MDM). |
| `--ssl-cert-file` | – | Per-machine CA bundle (`SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`). |
| `--required-min-version` | – | Enforce a Claude Code version floor. |

## Deploying the output

Push `managed-settings.json` to the OS path via MDM (Jamf/Intune/GPO):

| OS | Path |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux/WSL | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

Each developer runs `databricks auth login --host <url> --profile <profile>` once.
Verify with `/status` in Claude Code.

## Adding an agent

1. Create `agents/<agent>.py` with a class subclassing `AgentGenerator`
   (`name`, `add_arguments`, `generate`).
2. Register it in `agents/__init__.py`.

Requires Python 3.10+ (stdlib only) and the `databricks` CLI on PATH.
