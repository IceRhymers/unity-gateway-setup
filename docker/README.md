# docker — isolated test harness

This container is a throwaway harness that tests the generated agent configs on a
machine that already has host-level settings. It tests Claude Code's
`managed-settings.json` (gateway routing **and** OTEL telemetry), Codex's
`config.toml` (gateway routing), and DeepSeek Harness's `cordis.patch.yml`
(gateway routing). The container has its own
`/etc/claude-code/managed-settings.json`, its own `~/.codex/config.toml`, and its
own `~/.dsh/cordis.patch.yml`, so it never touches the host's files.

## What's inside

- **Claude Code** + **Codex** + **opencode** + **DeepSeek Harness** (`dsh`) + the
  **databricks CLI** + **python3** (for the api-key, otel-headers, and Codex auth
  helpers).
- **`ug`** (Unity AI Gateway coding CLI), installed via `uv` and available on
  `PATH` by default. The harness uses it to discover Databricks MCP services and
  register them into Claude Code's user-level config. See [MCP servers](#mcp-servers).
- The generated **Claude Code** config. The harness stages it as Linux
  enterprise-managed settings at `/etc/claude-code/` (the `managed-settings.json`,
  the `otel-headers-helper.sh`, and the `emit_hook_events.sh` reporting hook). The
  harness mounts the generator's **`linux/` bundle**
  (`agent_setups/generated/container/claude-code/linux/`), so it tests the same
  artifact a Linux deploy ships. The harness stages the generated **Codex**
  `config.toml` at the dev user's `~/.codex/config.toml` (Codex has no
  system-managed path). The image installs `jq` for the hook.
- The generated **opencode** config (npm pkg `opencode-ai`, bin `opencode`). The
  harness stages `opencode.json` and `databricks-auth.ts` at the Linux managed path
  `/etc/opencode/` (opencode reads managed config last; it overrides user config).
  The macOS `.mobileconfig` hard-lock does not apply inside a Linux container.
- The generated **DeepSeek Harness** config. The harness stages the home patch
  (`cordis.patch.yml`) and its token plugin (`databricks-token-refresh.mjs`) at
  the dev user's `~/.dsh/` (DSH has no system-managed path). The entrypoint runs
  the real user-scoped installer (`install-dsh-local.sh`) as the dev user. The
  patch routes DSH's native DeepSeek adapter through the gateway. The token plugin
  mints a fresh Databricks token on a timer and stores it in the credential store,
  so the adapter reads a current token on each request.
- **Tool versions are pinned** via Dockerfile `ARG` defaults (the single source of
  truth): base image digest, `CLAUDE_CODE_VERSION`, `CODEX_VERSION`,
  `OPENCODE_VERSION`, `DSH_VERSION`, `DATABRICKS_CLI_VERSION`, `UV_VERSION`, and
  `UCODE_SOURCE` (commit SHA). Override any pin with `--build-arg <VAR>=<value>`.
  `NPM_REGISTRY`, `UCODE_SOURCE`, and `PYPI_INDEX` overrides remain available for
  corporate mirrors.
- A fresh, isolated `~/.databrickscfg`. The harness writes it at start. Both
  `DEFAULT` and the named profile point at the workspace, so `databricks auth login`
  and the settings' `--profile` calls resolve. The profile is also the default
  (`DATABRICKS_CONFIG_PROFILE`).
- A `socat` bridge that lets the in-container `databricks auth login` complete. The
  CLI's OAuth callback listens on `127.0.0.1:8020`, which is unreachable through
  `docker -p`. socat forwards the container's external `:8020` to it. The
  `-p 8020:8020` map connects it to your host, so the browser redirect to
  `localhost:8020` lands.

## Flow (Makefile targets)

```bash
make tf-apply           # provision the telemetry infra first (creates tables, SP, secret)
make docker-build       # build the image (once) — Claude Code + Codex + opencode + dsh + databricks CLI + ug
make docker-config-all  # generate all agent configs (or docker-config / docker-config-codex / docker-config-opencode / docker-config-dsh)
make docker-up          # start the container (maps 8020, mounts configs, writes the profile)
make docker-login       # runs `databricks auth login` inside — see browser note below
make docker-shell       # exec in as the dev user
```

`make docker-test` runs build + all configs + up together. To iterate on the
configs without restarting (keeps auth), run `make docker-reload`. It regenerates
**all** agent configs and pushes them into the running container in one step.

### Authenticating

`make docker-login` prints an OAuth URL. Open it in your **host** browser. Complete
SSO. The redirect to `localhost:8020` flows through the port map and socat bridge
back into the container. The container caches the token inside itself, not on your
host. Everything defaults to the `PROFILE` (fevm-west), so you do not need any
`--profile` juggling.

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

The generated `managed-settings.json` always carries the reporting `hooks` block.
The harness stages `emit_hook_events.sh` at `/etc/claude-code/`. `make docker-config`
**auto-derives** the workspace's Zerobus endpoint and bakes it into the script, so
the hooks are live by default (pass `ARGS="--zerobus-endpoint <url>"` only to
override).

Delivery uses a **spool-then-flush** model. Producer hooks append events to a
per-session spool (`~/.cache/unity-gateway/spool/`). Then `flush` batches them to
Zerobus at turn/session boundaries. In a real `claude` session the flush happens on
`Stop`. To test by hand, spool an event and then flush it:

```bash
# 1. spool a skill-usage event (instant, no network — just appends to the spool):
echo '{"session_id":"harness","tool_name":"Skill","tool_input":{"skill":"databricks:databricks-jobs"}}' \
  | /etc/claude-code/emit_hook_events.sh posttool
ls ~/.cache/unity-gateway/spool/                     # -> harness.jsonl

# 2. flush the batch (mints + caches the SP bearer on first use; drains the spool):
echo '{"session_id":"harness"}' | /etc/claude-code/emit_hook_events.sh flush

# 3. confirm it landed (Zerobus ingest is streaming — allow a few seconds):
databricks api post /api/2.0/sql/statements --json '{"warehouse_id":"<id>","statement":"SELECT category,event_name,attributes FROM <catalog>.telemetry.claude_hook_events ORDER BY event_time DESC LIMIT 5","wait_timeout":"30s"}'
```

The flush needs `READ_SECRET` on the UC secret (same as OTEL) to mint the bearer.
With no endpoint set, events still spool but the flush no-ops — wired but dormant,
exactly as a default deploy ships. Rows become queryable a few seconds after the
flush returns 200 (streaming ingest), so do not judge success by an instant query.

### Testing Codex

The harness stages the Codex `config.toml` at `~/.codex/config.toml` inside the
container. Inside `make docker-shell`:

```bash
codex doctor      # checks config, auth, and runtime health against the gateway
codex             # launches Codex, routed through <host>/ai-gateway/mlflow/v1 (+ /responses)
```

Codex has no client-side OTEL, but each model service's inference-logging UC table
still captures its traffic server-side. Switch models with
`codex -m tanner_..._catalog.openai.gpt-5-6-sol` (any model listed in the config's
comment header). To iterate on the config without restarting the container, run
`make docker-reload` (reloads all agent configs).

### Testing opencode

The harness stages `opencode.json` and `databricks-auth.ts` at `/etc/opencode/`
inside the container. The container sets `OPENCODE_CONFIG=/etc/opencode/opencode.json`
so the pinned opencode reads the staged managed config. This env var override is
required because opencode 1.1.4 predates automatic `/etc/opencode` directory loading.
That feature was added in a later release. The `OPENCODE_CONFIG` env var is the
documented override that tells opencode to load a specific config file.

Run `make docker-config-opencode` to generate the config first, then start the
container with `make docker-up`. Inside `make docker-shell`:

```bash
opencode --version   # confirm the pinned version is installed
opencode             # launches opencode, routed through <host>/ai-gateway/anthropic/v1
                     # (or gemini/v1beta / mlflow/v1 depending on model family)
```

Authenticate first with `make docker-login`. The `databricks-auth.ts` plugin mints
a fresh OAuth token on each request via the Databricks CLI. To iterate on the
config without restarting the container, run `make docker-reload` (reloads all
agent configs including opencode).

### Testing DeepSeek Harness

The harness stages the DeepSeek Harness home patch (`cordis.patch.yml`) and its
token plugin (`databricks-token-refresh.mjs`) at `~/.dsh/` inside the container.
The token plugin stores the token in the `DATABRICKS_GATEWAY_TOKEN` credential
reference. Do not set that variable in your shell. DSH treats the launch
environment as read-only, and it would shadow the stored token.

Inside `make docker-shell`:

```bash
dsh --profile headless --dump-config   # print the composed plugin tree (verifies the patch applies)
dsh --profile headless "summarize this workspace"   # one-shot run, routed through <host>/ai-gateway/mlflow/v1
```

Authenticate first with `make docker-login`. The token plugin also runs the CLI
itself, so a valid session lets it mint the first token at start. DSH has no
client-side OTEL yet, but the DeepSeek model service's inference-logging UC table
still captures its traffic server-side. To iterate on the config without
restarting the container, run `make docker-reload` (reloads all agent configs).

## MCP servers

The image includes `ug`, so you can test MCP-service discovery and registration
against the deployed MDM config. After authenticating (`make docker-login`):

```bash
make docker-mcp        # runs `ug configure mcp` inside the container
```

It discovers the Databricks MCP servers your identity can see (external
connections, Databricks SQL, managed MCPs, and `system.ai.*` services). It lets you
pick which to add. Then it writes them to the agent's **user-level** config
(`~/.claude.json` for Claude Code, `~/.codex/config.toml`'s `[mcp_servers.*]` for
Codex), separate from the gateway-routing configs this harness stages. It registers
each server as a local stdio bridge (`ug mcp-proxy`) that mints a fresh OAuth
token per request from the container's databricks profile.

Then run `claude` or `codex` (via `make docker-shell`). The registered MCP tools are
then available. Target a specific agent through `ARGS`, for example:

```bash
make docker-mcp ARGS="--agents claude"
make docker-mcp ARGS="--agents codex"
```

> The image installs `ug` from `github.com/databricks/ucode` at build time.
> If that repo needs auth or a mirror, override the source:
> `make docker-build UCODE_SOURCE="git+https://<token>@github.com/databricks/ucode"`.

## Cleanup

```bash
make docker-down     # stop + remove the container
```

The image and the generated config under `agent_setups/generated/container/`
(gitignored) remain. Rebuild or regenerate them as needed. Override `PROFILE`,
`DOCKER_IMAGE`, or `DOCKER_CONTAINER` on the `make` command line.
