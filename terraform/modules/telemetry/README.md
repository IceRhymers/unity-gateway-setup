# telemetry

Provisions the **ingestion side** of Claude Code / coding-agent OpenTelemetry on
Databricks: the tables OTLP data lands in, plus the identity and credential the
agents authenticate with.

## What it creates

| Resource | Purpose |
|---|---|
| `databricks_schema` (optional) | A dedicated `telemetry` schema. |
| OTEL Delta tables | `claude_otel_metrics` / `_logs` / `_traces` — created via the Statement Execution API (see below). |
| Hook-event Delta table (optional) | `claude_hook_events` — the landing table for custom Claude Code reporting hooks (Zerobus REST); created the same way. See below. |
| `databricks_service_principal` | Databricks-managed SP; the identity telemetry is attributed to. |
| `databricks_service_principal_secret` | Workspace-level OAuth (M2M) secret for that SP. |
| `databricks_secret_uc` | Stores `{client_id, client_secret}` as JSON; read at runtime by each developer's `otelHeadersHelper`. |
| `databricks_grant` ×2 | SP gets `USE_CATALOG` on the catalog and `USE_SCHEMA`+`MODIFY`+`SELECT` on the telemetry schema. |
| `null_resource` (readers) | Grants `READ_SECRET` on the UC secret to `reader_groups` via the UC permissions API. |

## Why this shape

- **Service-principal identity, not per-developer.** OTLP ingest needs `MODIFY`
  on the target tables, which also permits `UPDATE`/`DELETE`. Attributing
  telemetry to a dedicated SP means developers never get table access — only
  `READ_SECRET` on the credential — so they can't tamper with telemetry.
- **The header split.** Claude Code's `otelHeadersHelper` returns one header set
  merged across signals, so it carries only the sensitive `Authorization: Bearer`
  (minted from this SP's OAuth creds). The per-signal `X-Databricks-UC-Table-Name`
  and `content-type` are static env vars the generator emits.
- **Workspace-level only.** Both the SP and its secret use `api = "workspace"`,
  so no account-level provider is required.
- **Tables via Statement Execution.** The OTLP schema is deeply nested and the
  `/api/2.0/otel` endpoint requires the tables to pre-exist, so a `null_resource`
  runs the canonical DDL (`templates/otel_*.sql`) on `warehouse_id` through
  `scripts/create_otel_table.py`. `CREATE TABLE IF NOT EXISTS` is idempotent.

## Hook events (`claude_hook_events`)

Native OTEL captures what the Claude Code binary emits; it does **not** emit the
per-hook reporting signals (slash-command / skill / subagent usage with plugin
attribution, per-session plugin inventory, `StopFailure` mid-stream stalls,
guardrail hits, workflow adoption). This module optionally provisions the landing
table for those — a single wide managed Delta table (`templates/hook_events.sql`,
one row per event, `category` + `event_name` + a VARIANT `attributes` bag,
queryable as `attributes:field` — Zerobus carries VARIANT as a JSON-encoded string
on the wire, which is what the hook sends), plus
an **explicit table-level** `MODIFY`/`SELECT` grant on the telemetry SP (Zerobus's
`authorization_details` flow needs it — schema-level grants alone fail with error
4024). The generated `emit_hook_events.sh` hook (from `agent_setups`) ingests into
it over the **Zerobus REST API** as that same SP, so developers still need only
`READ_SECRET` on the UC secret — no table access, no dev-machine SDK.

The generated `managed-settings.json` ships the reporting hooks regardless; set
`zerobus_endpoint` (`https://<workspace-id>.zerobus.<region>.cloud.databricks.com`)
to activate them — until then they're wired but dormant (the script no-ops on an
empty endpoint, which is also `ZEROBUS_ENDPOINT`-overridable at runtime). Zerobus
does not create tables — that's why this runs the DDL up front. Set
`hook_events_enabled = false` to skip the table and grant entirely.

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `catalog_name` | — | Existing catalog (required). |
| `schema_name` | `telemetry` | |
| `create_schema` | `true` | |
| `warehouse_id` | — | **Required.** SQL warehouse for the table DDL. |
| `signals` | `["metrics","logs","traces"]` | |
| `hook_events_enabled` | `true` | Create `claude_hook_events` + the table-level SP grant. |
| `hook_events_table_name` | `claude_hook_events` | Leaf name of the hook-event table. |
| `zerobus_endpoint` | `""` | Zerobus REST base URL; empty leaves the generated hook off. |
| `service_principal_display_name` | `unity-gateway-otel-telemetry` | |
| `secret_name` | `otel_exporter_oauth` | |
| `reader_groups` | `[]` | Groups granted `READ_SECRET` on the secret. |
| `databricks_profile` | — | Used by the `local-exec` runner + grant. |

Requires `python3` and the `databricks` CLI on PATH at apply time (the
`local-exec` provisioners use them), run from a **POSIX shell** (the READ_SECRET
grant relies on single-quote shell semantics). Outputs feed the agent-config
generator via the infra `telemetry` output.

## Operational notes

- **`reader_groups` revocation.** Each group is a separate `null_resource` with a
  destroy-time provisioner, so removing a group from `reader_groups` and applying
  revokes its `READ_SECRET`. (Adding is create-time; both go through
  `databricks grants update SECRET`.)
- **Table DDL is `CREATE TABLE IF NOT EXISTS`.** These are the fixed OTLP v1 spec
  schemas. Editing a `templates/otel_*.sql` for a table that already exists is a
  **no-op** — apply looks green but the live table is not altered. If a column
  ever must change, migrate the existing table manually (`ALTER TABLE`).
