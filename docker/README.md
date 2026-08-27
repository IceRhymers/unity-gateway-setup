# docker — isolated test harness

A throwaway container for testing the generated agent configs — Claude Code's
`managed-settings.json` (gateway routing **and** OTEL telemetry) and Codex's
`config.toml` (gateway routing) — on a machine that already has host-level
settings. The container has its own `/etc/claude-code/managed-settings.json` and
its own `~/.codex/config.toml`, so the host's are never touched.

## What's inside

- **Claude Code** + **Codex** + the **databricks CLI** + **python3** (for the
  api-key, otel-headers, and Codex auth helpers).
- **`ucode`** (Unity AI Gateway coding CLI), installed via `uv` and available on
  `PATH` by default — used here to discover Databricks MCP services and register
  them into Claude Code's user-level config. See [MCP servers](#mcp-servers).
- The generated **Claude Code** config staged as Linux enterprise-managed settings
  at `/etc/claude-code/` (the `managed-settings.json`, the `otel-headers-helper.sh`,
  and the `emit_hook_events.sh` reporting hook) — the harness mounts the generator's
  **`linux/` bundle** (`agent_setups/generated/container/claude-code/linux/`), so it
  tests the same artifact a Linux deploy ships. The generated **Codex** `config.toml`
  is staged at the dev user's `~/.codex/config.toml` (Codex has no system-managed
  path). `jq` is installed for the hook.
- A fresh, isolated `~/.databrickscfg` written at start: both `DEFAULT` and the
  named profile point at the workspace, so `databricks auth login` and the
  settings' `--profile` calls resolve. The profile is also the default
  (`DATABRICKS_CONFIG_PROFILE`).
- A `socat` bridge so in-container `databricks auth login` completes: the CLI's
  OAuth callback listens on `127.0.0.1:8020` (unreachable through `docker -p`);
  socat forwards the container's external `:8020` to it, and `-p 8020:8020` maps
  it to your host so the browser redirect to `localhost:8020` lands.

## Flow (Makefile targets)

```bash
make tf-apply           # provision the telemetry infra first (creates tables, SP, secret)
make docker-build       # build the image (once) — Claude Code + Codex
make docker-config-all  # generate both agent configs (or docker-config / docker-config-codex)
make docker-up          # start the container (maps 8020, mounts configs, writes the profile)
make docker-login       # runs `databricks auth login` inside — see browser note below
make docker-shell       # exec in as the dev user
```

`make docker-test` runs build + both configs + up together. To iterate on the
configs without restarting (keeps auth), `make docker-reload` regenerates **both**
agent configs and pushes them into the running container in one step.

### Authenticating

`make docker-login` prints an OAuth URL. Open it in your **host** browser and
complete SSO; the redirect to `localhost:8020` flows through the port map + socat
bridge back into the container, and the token is cached inside the container
(not on your host). Everything defaults to the `PROFILE` (fevm-west), so no
`--profile` juggling is needed.

### Testing telemetry

Inside `make docker-shell`:

```bash
# 1. Verify the OTEL header helper end-to-end (reads the UC secret, mints the SP token):
/etc/claude-code/otel-headers-helper.sh        # -> {"Authorization": "Bearer ..."}

# 2. Run Claude Code — it routes through the gateway and exports OTLP:
claude
```

Then confirm rows are landing (from the shell, or your workspace):

```bash
databricks api post /api/2.0/sql/statements --json '{"warehouse_id":"<id>","statement":"SELECT count(*) FROM <catalog>.telemetry.claude_otel_metrics","wait_timeout":"30s"}'
```

### Testing hook telemetry (custom reporting events)

The generated `managed-settings.json` always carries the reporting `hooks` block,
and the harness stages `emit_hook_events.sh` at `/etc/claude-code/`. The hooks are
**dormant until a Zerobus endpoint is set**, so to exercise them end-to-end,
generate the container config with one:

```bash
make docker-config ARGS="--zerobus-endpoint https://<workspace-id>.zerobus.<region>.cloud.databricks.com"
make docker-reload   # (or docker-up if not running)
```

Inside `make docker-shell`, fire a hook directly (no need to drive Claude Code):

```bash
# a skill-usage event; should insert one row (backgrounded curl):
echo '{"session_id":"harness","tool_name":"Skill","tool_input":{"skill":"databricks:databricks-jobs"}}' \
  | /etc/claude-code/emit_hook_events.sh posttool

# then confirm it landed:
databricks api post /api/2.0/sql/statements --json '{"warehouse_id":"<id>","statement":"SELECT category,event_name,attributes FROM <catalog>.telemetry.claude_hook_events ORDER BY event_time DESC LIMIT 5","wait_timeout":"30s"}'
```

The first fire mints + caches the SP bearer (needs `READ_SECRET` on the UC secret,
same as OTEL); subsequent fires reuse it. With no endpoint set, the hook exits 0
without sending — wired but dormant, exactly as a default deploy ships.

### Testing Codex

The Codex `config.toml` is staged at `~/.codex/config.toml` inside the container.
Inside `make docker-shell`:

```bash
codex doctor      # checks config, auth, and runtime health against the gateway
codex             # launches Codex, routed through <host>/ai-gateway/mlflow/v1 (+ /responses)
```

Codex has no client-side OTEL, but its traffic is still captured server-side by
each model service's inference-logging UC table. Switch models with
`codex -m tanner_..._catalog.openai.gpt-5-6-sol` (any model listed in the config's
comment header). To iterate on the config without restarting the container, run
`make docker-reload` (reloads both agent configs).

## MCP servers

`ucode` is baked into the image, so you can test MCP-service discovery and
registration against the deployed MDM config. After authenticating
(`make docker-login`):

```bash
make docker-mcp        # runs `ucode configure mcp` inside the container
```

It discovers the Databricks MCP servers your identity can see (external
connections, Databricks SQL, managed MCPs, and `system.ai.*` services), lets you
pick which to add, and writes them to the agent's **user-level** config
(`~/.claude.json` for Claude Code, `~/.codex/config.toml`'s `[mcp_servers.*]` for
Codex) — separate from the gateway-routing configs this harness stages. Each
server is registered as a local stdio bridge (`ucode mcp-proxy`) that mints a
fresh OAuth token per request from the container's databricks profile.

Then run `claude` or `codex` (via `make docker-shell`) and the registered MCP
tools are available. Target a specific agent through `ARGS`, e.g.:

```bash
make docker-mcp ARGS="--agents claude"
make docker-mcp ARGS="--agents codex"
```

> The image installs `ucode` from `github.com/databricks/ucode` at build time.
> If that repo needs auth or a mirror, override the source:
> `make docker-build UCODE_SOURCE="git+https://<token>@github.com/databricks/ucode"`.

## Cleanup

```bash
make docker-down     # stop + remove the container
```

The image and the generated config under `agent_setups/generated/container/`
(gitignored) remain; rebuild/regenerate as needed. Override `PROFILE`,
`DOCKER_IMAGE`, or `DOCKER_CONTAINER` on the `make` command line.
