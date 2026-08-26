# docker — isolated test harness

A throwaway container for testing the generated Claude Code
`managed-settings.json` (gateway routing **and** OTEL telemetry) on a machine
that already has host-level managed settings — the container has its own
`/etc/claude-code/managed-settings.json`, so the host's is never touched.

## What's inside

- **Claude Code** + the **databricks CLI** + **python3** (for the api-key and
  otel-headers helpers).
- The generated config staged as Linux enterprise-managed settings at
  `/etc/claude-code/` (mounted read-only from `agent_setups/generated/container/`).
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
make tf-apply        # provision the telemetry infra first (creates tables, SP, secret)
make docker-build    # build the image (once)
make docker-config   # generate the config with the Linux helper path
make docker-up       # start the container (maps 8020, writes the profile)
make docker-login    # runs `databricks auth login` inside — see browser note below
make docker-shell    # exec in as the dev user
```

`make docker-test` runs build + config + up together.

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

## Cleanup

```bash
make docker-down     # stop + remove the container
```

The image and the generated config under `agent_setups/generated/container/`
(gitignored) remain; rebuild/regenerate as needed. Override `PROFILE`,
`DOCKER_IMAGE`, or `DOCKER_CONTAINER` on the `make` command line.
