# unity-gateway-setup

An opinionated, reproducible setup that creates a **Databricks Unity AI
Gateway** to govern **coding-agent traffic**. It puts the gateway in front of
your developers' agents, with Unity Catalog governance, inference logging, and
OpenTelemetry included.

It has three layers plus a launch surface:

1. **Terraform** (`terraform/`) provisions the gateway. This includes model
   services backed by Databricks FMAPI, inference logging to UC Delta tables,
   the OTEL ingestion stack, and a hook-event table. The hook-event table holds
   the custom reporting signals that native OTEL does not emit (agent-usage,
   reliability, governance, adoption).
2. **Config generator** (`agent_setups/`) reads the Terraform outputs and emits
   the **fleet baseline**: Claude Code `managed-settings.json` (+ an OTEL headers
   helper), a Codex `config.toml`, a Claude Desktop bundle, and a DeepSeek
   Harness patch. It covers inference routing and telemetry only. It does not
   register MCP servers.
3. **`ug`** is the **intended entrypoint** developers use to launch agents. It
   owns everything on the developer's own machine: launch, per-request OAuth, MCP
   registration, and skills.
4. **Docker harness** (`docker/`) tests the generated configs (routing,
   telemetry, and MCP) in an isolated container. The container never touches the
   host's real settings.

```
 terraform/infra ── outputs ──▶ agent_setups (generator) ──▶ managed-settings.json
   (gateway,                                                   + otel-headers-helper.sh
    logging,                                                          │
    telemetry)                                                        ▼
                                                          MDM (Jamf/Intune/GPO)
                                                          fleet-wide baseline
                                                                      │
                                                                      ▼
                                          developer machine:  ug ──▶ claude / codex / …
                                                             (MCP discovery + OAuth)
```

The MDM baseline makes **inference** work. `ug` is how agents are meant to be
**launched**. The two are complementary, and they do not overlap — see
[Division of labour](#division-of-labour-the-generator-and-ug).

---

## Launching agents: `ug` is the intended entrypoint

The generated `managed-settings.json` (deployed fleet-wide via MDM) fully
configures **inference**. It does five things:

- points the agent at `<host>/ai-gateway/anthropic`
- mints a U2M OAuth token for every model call via `apiKeyHelper`
- pins the model tiers to the gateway's three-level UC names
- enforces the allow-list
- exports OTEL telemetry

With that file in place, running `claude` directly **just works** for inference.
That path stays supported.

But the **intended surface is `ug`** (the Unity AI Gateway coding CLI),
because it supplies the two things a static MDM file cannot:

- **MCP discovery.** `ug mcp add` finds the Databricks MCP servers your
  identity can see — UC external connections, Databricks SQL, managed MCPs, and
  `system.ai.*` services. You pick which to register. `ug` writes them into
  the agent's **user-level** config (alongside, not replacing, the MDM baseline).
  `ug` bridges each server as a local stdio proxy.
- **A governed OAuth surface.** A launch through `ug` (bare `ug`, or
  `ug claude`) runs a per-launch Databricks auth and AI Gateway re-validation
  before it starts the agent. Every registered MCP bridge mints a **fresh OAuth
  token per request** from your Databricks profile. `ug` governs tool access
  the same way it governs inference, with no bearer tokens baked into any config
  file.

`ug` also closes a gap the MDM baseline deliberately leaves open. The managed
config **denies the built-in `WebSearch` tool**, because built-in search cannot
reach `api.anthropic.com` through the gateway. `ug mcp web-search` supplies a
gateway-backed `web_search` MCP as the replacement.

### Day-to-day

```bash
ug                      # launch the agent your workspace's managed config selects
ug claude               # launch Claude Code specifically, routed through the gateway
ug claude --model <catalog.schema.name>   # pin a specific gateway model for this launch

# one-time (per workspace / when MCP servers change):
ug configure            # set up workspace + tool settings (auth, routing)
ug mcp add              # discover + register Databricks MCP servers
ug status               # show workspace, tool configs, saved model selections
ug usage                # AI Gateway usage summary (last 7 days)
```

`ug` supports Claude Code, Codex, Gemini CLI, OpenCode, Copilot CLI, Pi, and
Cursor — the same gateway + MCP + OAuth surface for each.

### `claude` directly still works

The MDM `managed-settings.json` configures inference on its own. So invoking
`claude` (or any agent) directly routes its model calls through the gateway and
emits telemetry with no `ug` involvement. The direct path gives up two
things: the discovered Databricks MCP tools and the per-launch auth
re-validation. Treat the direct path as a supported fallback. **Prefer `ug`**
for normal work.

---

## Division of labour: the generator and `ug`

The generator and `ug` do different jobs. Nothing is implemented twice. Use this
table to decide where a change belongs.

| Concern | Owner | Why |
|---|---|---|
| The gateway itself (model services, inference logging, OTEL stack, hook-event table) | **Terraform** (`terraform/`) | Workspace infrastructure. `ug` provisions nothing. |
| Fleet inference baseline (base URL, model pins, allow-list, permission deny-list) | **generator** (`agent_setups/`) | It must work before a developer installs anything, and an MDM tool must push it. |
| OTEL export and hook events | **generator** | `ug` emits no OTEL and no hook events. Its own telemetry is a `User-Agent` string plus Claude Code MLflow tracing. |
| DeepSeek Harness | **generator** | `ug` does not support the agent. |
| Claude Desktop policy and telemetry | **generator** | `ug` has no Claude Desktop target, and neither the OTEL target nor the Claude.ai sign-in lockout can come from `ug`. |
| Claude Desktop models, base URL, identity | **`ug`** | The generated `ug-bootstrap-claude-desktop.sh` runs `ug configure`, then reads the workspace, profile, base URL, and Claude model pins from `~/.ucode/state.json`. The generator pushes no model list. |
| Launching an agent | **`ug`** | `ug claude`, `ug codex`, and so on. |
| Per-request OAuth for model calls | **both, by agent** | Claude Code and Codex get it from `apiKeyHelper` in the managed file. For every agent `ug` launches, `ug` mints the token. |
| MCP discovery and registration | **`ug`** | `ug mcp add` covers seven agents, removes stale servers, and bridges each server through the bundled `ug mcp-proxy`. |
| Skills, spend tiers, workspace-level managed config | **`ug`** | `ug setup` and `ug publish` author a workspace-side `CodingAgentConfig` that developers pull. |
| OpenCode, Gemini CLI, Copilot CLI, Pi, Cursor | **`ug`** | `ug` configures and launches each one. The generator emits nothing for them. |

Two consequences worth knowing:

- **This repo used to register MCP servers itself, and no longer does.** `make mcp`
  and the harness-merge code are gone. Run `ug mcp add` instead. It is a superset:
  it targets seven agents rather than three, it removes stale servers, and it needs
  no `uvx` hop.
- **This repo used to generate an OpenCode config, and no longer does.** `ug`
  configures OpenCode with the same three gateway routes and its own
  token-refresh plugin, so a second config competed with it. Run `ug opencode`.

One interaction to keep in mind: when `ug` runs on a machine, it **reconciles** the
managed settings file this repo deploys. It asks for `sudo`, keeps a backup, and
`ug revert` restores it. So an MDM push and a later `ug` run can both change
`managed-settings.json`. Check `ug status` when the deployed file does not look
like the file you pushed.

---

## End-to-end setup

### 1. Provision the gateway (Terraform)

```bash
cd terraform/infra
cp terraform.tfvars.example terraform.tfvars   # pick catalog mode, providers, telemetry
terraform init && terraform apply
```

This creates several resources:

- the provider schemas
- an FMAPI-backed **model service** per endpoint (each logs inference to a UC
  Delta table)
- by default, the OTEL ingestion stack (schema, metrics/logs/traces tables, a
  managed service principal + workspace OAuth secret, and grants)
- a `claude_hook_events` table for custom hook-based reporting (set
  `telemetry_zerobus_endpoint` to enable the hook)

See [`terraform/README.md`](terraform/README.md).

### 2. Generate the agent config

```bash
make agent-claude-code PROFILE=fevm-west   # Claude Code managed-settings.json
make agent-codex PROFILE=fevm-west         # Codex config.toml
```

This reads the Terraform outputs and writes a **self-contained bundle per OS** —
`agent_setups/generated/claude-code/{macos,linux,windows}/`. Each bundle has a
`managed-settings.json` plus the `otel-headers-helper.sh` and
`emit_hook_events.sh` scripts (when enabled). It can also write
`agent_setups/generated/codex/config.toml`. The bundles are identical except for
the on-disk paths that `managed-settings.json` references (keyed to each OS's
ClaudeCode dir). Deploy the bundle for each platform you manage. Every model pin,
the allow-list, and the telemetry env block derive straight from the deployed
gateway, so keeping them current costs nothing — just regenerate. See
[`agent_setups/scripts/README.md`](agent_setups/scripts/README.md) for every flag
and the Codex specifics (Codex has no MDM path — deploy per-user into
`$CODEX_HOME`).

### 3. Deploy the baseline via MDM

Push each OS's bundle (`managed-settings.json` + the `otel-headers-helper.sh` and
`emit_hook_events.sh` scripts) to that OS's ClaudeCode directory with your MDM
tool (Jamf / Intune / GPO). The helper and hook paths inside
`managed-settings.json` already point there, so the whole bundle goes in one
place:

| OS | Deploy the `<os>/` bundle to |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/` |
| Linux/WSL | `/etc/claude-code/` |
| Windows | `C:\Program Files\ClaudeCode\` |

Each developer authenticates once with
`databricks auth login --host <url> --profile <profile>`. When telemetry is on,
each developer also needs `READ_SECRET` on the telemetry UC secret (grant a group
via `telemetry_reader_groups`).

### 4. Developers launch through `ug`

Follow these steps:

1. Install `ug` with `uv tool install git+https://github.com/databricks/ucode`.
2. Run `ug configure` and `ug mcp add` once.
3. Run `ug` or `ug claude` from then on.

See [Launching agents](#launching-agents-ug-is-the-intended-entrypoint)
above.

### Testing it first (Docker harness)

Before you touch real machines, validate the generated config end-to-end in an
isolated container. This covers gateway routing, OTEL export, and `ug` MCP
discovery. The container never touches the host's own managed settings:

```bash
make docker-test     # build + generate both agent configs + start the container
make docker-login    # databricks auth login inside (browser on host)
make docker-mcp      # discover + register Databricks MCP servers via ug
make docker-shell    # exec in; run `ug claude` / `ug codex` (or `claude` / `codex`)
```

See [`docker/README.md`](docker/README.md).

---

## Repo layout

```
terraform/          Provision the gateway
  infra/              Applyable deployment (defaults: fevm-west sandbox)
  modules/            unity-foundation · model-service · telemetry
agent_setups/       Generate agent configs from the TF outputs
  scripts/            The generator (Claude Code · Claude Desktop · Codex · dsh)
  generated/          Output (gitignored — embeds a workspace host)
docker/             Isolated test harness (Claude Code + Codex + databricks CLI + ug)
Makefile            Task runner — `make help` lists targets
```

## Requirements

- Terraform ≥ 1.5.0 and the Databricks provider ≥ 1.129.0 (first with the AI
  Gateway model-service resources).
- The `databricks` CLI on PATH, with a `~/.databrickscfg` profile that has Unity
  Catalog + AI Gateway access.
- Python 3.10+ (stdlib only) for the config generator.
- `ug` (`uv tool install git+https://github.com/databricks/ucode`, Python
  3.12+) on each developer machine — the launch entrypoint.
- Docker, only for the test harness.

Run `make help` for the full list of targets.
