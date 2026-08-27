# unity-gateway-setup

An opinionated, reproducible setup for standing up a **Databricks Unity AI
Gateway** that governs **coding-agent traffic** — and for putting that gateway in
front of your developers' agents with Unity Catalog governance, inference
logging, and OpenTelemetry all wired in.

It's three layers plus a launch surface:

1. **Terraform** (`terraform/`) provisions the gateway — model services backed by
   Databricks FMAPI, inference logging to UC Delta tables, the OTEL ingestion
   stack, and a hook-event table for the custom reporting signals native OTEL
   doesn't emit (agent-usage, reliability, governance, adoption).
2. **Config generator** (`agent_setups/`) reads the Terraform outputs and emits
   opinionated, deployable agent configs — Claude Code `managed-settings.json`
   (+ an OTEL headers helper) and a Codex `config.toml` — the routing baseline.
3. **`ucode`** is the **intended entrypoint** developers use to launch agents. It
   layers MCP discovery and a governed OAuth surface on top of that baseline.
4. **Docker harness** (`docker/`) tests the generated configs — routing, telemetry,
   and MCP — in an isolated container that never touches the host's real settings.

```
 terraform/infra ── outputs ──▶ agent_setups (generator) ──▶ managed-settings.json
   (gateway,                                                   + otel-headers-helper.sh
    logging,                                                          │
    telemetry)                                                        ▼
                                                          MDM (Jamf/Intune/GPO)
                                                          fleet-wide baseline
                                                                      │
                                                                      ▼
                                          developer machine:  ucode ──▶ claude / codex / …
                                                             (MCP discovery + OAuth)
```

The MDM baseline makes **inference** work; `ucode` is how agents are meant to be
**launched**. The two are complementary — details below.

---

## Launching agents: `ucode` is the intended entrypoint

The generated `managed-settings.json` (deployed fleet-wide via MDM) fully
configures **inference**: it points the agent at `<host>/ai-gateway/anthropic`,
mints a U2M OAuth token for every model call via `apiKeyHelper`, pins the model
tiers to the gateway's three-level UC names, enforces the allow-list, and exports
OTEL telemetry. With that file in place, running `claude` directly **just works**
for inference, and that path stays supported.

But the **intended surface is `ucode`** (the Unity AI Gateway coding CLI), because
it supplies the two things a static MDM file cannot:

- **MCP discovery.** `ucode mcp add` finds the Databricks MCP servers your
  identity can see — UC external connections, Databricks SQL, managed MCPs, and
  `system.ai.*` services — lets you pick which to register, and writes them into
  the agent's **user-level** config (alongside, not replacing, the MDM baseline).
  Each server is bridged through `ucode` as a local stdio proxy.
- **A governed OAuth surface.** Launching through `ucode` (bare `ucode`, or
  `ucode claude`) runs a per-launch Databricks auth + AI Gateway re-validation
  before starting the agent, and every registered MCP bridge mints a **fresh
  OAuth token per request** from your Databricks profile. Tool access is governed
  the same way inference is — with no bearer tokens baked into any config file.

It also closes a gap the MDM baseline deliberately leaves open: the managed
config **denies the built-in `WebSearch` tool** (built-in search can't reach
`api.anthropic.com` through the gateway). `ucode mcp web-search` supplies a
gateway-backed `web_search` MCP as the drop-in replacement.

### Day-to-day

```bash
ucode                      # launch the agent your workspace's managed config selects
ucode claude               # launch Claude Code specifically, routed through the gateway
ucode claude --model <catalog.schema.name>   # pin a specific gateway model for this launch

# one-time (per workspace / when MCP servers change):
ucode configure            # set up workspace + tool settings (auth, routing)
ucode mcp add              # discover + register Databricks MCP servers
ucode status               # show workspace, tool configs, saved model selections
ucode usage                # AI Gateway usage summary (last 7 days)
```

`ucode` supports Claude Code, Codex, Gemini CLI, OpenCode, Copilot CLI, Pi, and
Cursor — the same gateway + MCP + OAuth surface for each.

### `claude` directly still works

Because the MDM `managed-settings.json` configures inference on its own, invoking
`claude` (or any agent) directly routes its model calls through the gateway and
emits telemetry with no `ucode` involvement. What you give up going direct: the
discovered Databricks MCP tools and the per-launch auth re-validation. Treat the
direct path as a supported fallback; **prefer `ucode`** for normal work.

---

## End-to-end setup

### 1. Provision the gateway (Terraform)

```bash
cd terraform/infra
cp terraform.tfvars.example terraform.tfvars   # pick catalog mode, providers, telemetry
terraform init && terraform apply
```

This creates the provider schemas, an FMAPI-backed **model service** per endpoint
(each logging inference to a UC Delta table), and — by default — the OTEL
ingestion stack (schema, metrics/logs/traces tables, a managed service principal
+ workspace OAuth secret, and grants) plus a `claude_hook_events` table for
custom hook-based reporting (set `telemetry_zerobus_endpoint` to turn the hook on).
See [`terraform/README.md`](terraform/README.md).

### 2. Generate the agent config

```bash
make agent-claude-code PROFILE=fevm-west   # Claude Code managed-settings.json
make agent-codex PROFILE=fevm-west         # Codex config.toml
```

Reads the Terraform outputs and writes a **self-contained bundle per OS** —
`agent_setups/generated/claude-code/{macos,linux,windows}/`, each with a
`managed-settings.json` plus the `otel-headers-helper.sh` and `emit_hook_events.sh`
scripts (when enabled) — and/or `agent_setups/generated/codex/config.toml`. The
bundles are identical except the on-disk paths `managed-settings.json` references
(keyed to each OS's ClaudeCode dir); deploy the bundle for each platform you
manage. Every model pin, the allow-list, and the telemetry env block derive
straight from the deployed gateway, so keeping them current costs nothing — just
regenerate. See
[`agent_setups/scripts/README.md`](agent_setups/scripts/README.md) for every flag
and the Codex specifics (it has no MDM path — deploy per-user into `$CODEX_HOME`).

### 3. Deploy the baseline via MDM

Push each OS's bundle (`managed-settings.json` + the `otel-headers-helper.sh` and
`emit_hook_events.sh` scripts) with your MDM tool (Jamf / Intune / GPO) to that
OS's ClaudeCode directory — the helper/hook paths inside `managed-settings.json`
already point there, so the whole bundle goes in one place:

| OS | Deploy the `<os>/` bundle to |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/` |
| Linux/WSL | `/etc/claude-code/` |
| Windows | `C:\Program Files\ClaudeCode\` |

Each developer authenticates once
(`databricks auth login --host <url> --profile <profile>`), and needs
`READ_SECRET` on the telemetry UC secret when telemetry is on (grant a group via
`telemetry_reader_groups`).

### 4. Developers launch through `ucode`

Install `ucode` (`uv tool install git+https://github.com/databricks/ucode`), then
`ucode configure` / `ucode mcp add` once, and `ucode` / `ucode claude` from then
on — see [Launching agents](#launching-agents-ucode-is-the-intended-entrypoint)
above.

### Testing it first (Docker harness)

Before touching real machines, validate the generated config end-to-end in an
isolated container — gateway routing, OTEL export, and `ucode` MCP discovery — all
without touching the host's own managed settings:

```bash
make docker-test     # build + generate both agent configs + start the container
make docker-login    # databricks auth login inside (browser on host)
make docker-mcp      # discover + register Databricks MCP servers via ucode
make docker-shell    # exec in; run `ucode claude` / `ucode codex` (or `claude` / `codex`)
```

See [`docker/README.md`](docker/README.md).

---

## Repo layout

```
terraform/          Provision the gateway
  infra/              Applyable deployment (defaults: fevm-west sandbox)
  modules/            unity-foundation · model-service · telemetry
agent_setups/       Generate agent configs from the TF outputs
  scripts/            The generator (Claude Code + Codex; registry for more agents)
  generated/          Output (gitignored — embeds a workspace host)
docker/             Isolated test harness (Claude Code + Codex + databricks CLI + ucode)
Makefile            Task runner — `make help` lists targets
```

## Requirements

- Terraform ≥ 1.5.0 and the Databricks provider ≥ 1.129.0 (first with the AI
  Gateway model-service resources).
- The `databricks` CLI on PATH, with a `~/.databrickscfg` profile that has Unity
  Catalog + AI Gateway access.
- Python 3.10+ (stdlib only) for the config generator.
- `ucode` (`uv tool install git+https://github.com/databricks/ucode`, Python
  3.12+) on each developer machine — the launch entrypoint.
- Docker, only for the test harness.

Run `make help` for the full list of targets.
