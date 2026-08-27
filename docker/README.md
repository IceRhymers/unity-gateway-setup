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
  at `/etc/claude-code/`, and the generated **Codex** `config.toml` staged at the
  dev user's `~/.codex/config.toml` (Codex has no system-managed path) — both
  mounted read-only from `agent_setups/generated/container/`.
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

`make docker-test` runs build + both configs + up together. To iterate on a config
without restarting (keeps auth): `make docker-reload` (Claude Code) or
`make docker-reload-codex` (Codex).

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

### Testing Codex

The Codex `config.toml` is staged at `~/.codex/config.toml` inside the container.
Inside `make docker-shell`:

```bash
codex doctor      # checks config, auth, and runtime health against the gateway
codex             # launches Codex, routed through <host>/ai-gateway/codex/v1
```

Codex has no client-side OTEL, but its traffic is still captured server-side by
each model service's inference-logging UC table. Switch models with
`codex -m tanner_..._catalog.openai.gpt-5-6-sol` (any model listed in the config's
comment header). To iterate on the config without restarting the container, run
`make docker-reload-codex`.

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
