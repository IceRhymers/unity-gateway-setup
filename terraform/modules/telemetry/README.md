# telemetry

This module provisions the **ingestion side** of Claude Code / coding-agent
OpenTelemetry on Databricks. It creates the tables that OTLP data lands in. It
also creates the identity and credential that the agents authenticate with.

## What it creates

| Resource | Purpose |
|---|---|
| `databricks_schema` (optional) | A dedicated `telemetry` schema. |
| OTEL Delta tables | `claude_otel_metrics` / `_logs` / `_traces` — created via the Statement Execution API (see below). |
| Hook-event Delta table (optional) | `claude_hook_events` — the landing table for custom Claude Code reporting hooks (Zerobus REST). Created the same way. See below. |
| `databricks_service_principal` | Databricks-managed SP. Telemetry is attributed to this identity. |
| `databricks_service_principal_secret` | Workspace-level OAuth (M2M) secret for that SP. |
| `databricks_secret_uc` | Stores `{client_id, client_secret}` as JSON. Each developer's `otelHeadersHelper` reads it at runtime. |
| `databricks_grant` ×2 | SP gets `USE_CATALOG` on the catalog and `USE_SCHEMA`+`MODIFY`+`SELECT` on the telemetry schema. |
| `null_resource` (readers) | Grants `READ_SECRET` on the UC secret to `reader_groups` via the UC permissions API. |

## Why this shape

- **Service-principal identity, not per-developer.** OTLP ingest needs `MODIFY`
  on the target tables, which also permits `UPDATE`/`DELETE`. The telemetry is
  attributed to a dedicated SP, so developers never get table access — only
  `READ_SECRET` on the credential. This means they cannot tamper with telemetry.
- **The header split.** Claude Code's `otelHeadersHelper` returns one header set
  merged across signals, so it carries only the sensitive `Authorization: Bearer`
  (minted from this SP's OAuth creds). The per-signal `X-Databricks-UC-Table-Name`
  and `content-type` are static env vars the generator emits.
- **Workspace-level only.** Both the SP and its secret use `api = "workspace"`,
  so you do not need an account-level provider.
- **Tables via Statement Execution.** The OTLP schema is deeply nested. The
  `/api/2.0/otel` endpoint requires the tables to pre-exist. So a `null_resource`
  runs the canonical DDL (`templates/otel_*.sql`) on `warehouse_id` through
  `scripts/create_otel_table.py`. `CREATE TABLE IF NOT EXISTS` is idempotent.

## Hook events (`claude_hook_events`)

Native OTEL captures what the Claude Code binary emits. It does **not** emit the
per-hook reporting signals (slash-command / skill / subagent usage with plugin
attribution, per-session plugin inventory, `StopFailure` mid-stream stalls,
guardrail hits, workflow adoption). This module optionally provisions the landing
table for those signals. The table is a single wide managed Delta table
(`templates/hook_events.sql`). It holds one row per event, with `category` +
`event_name` + a VARIANT `attributes` bag, queryable as `attributes:field`.
Zerobus carries VARIANT as a JSON-encoded string on the wire, which is what the
hook sends. The module also creates an **explicit table-level** `MODIFY`/`SELECT`
grant on the telemetry SP. Zerobus's `authorization_details` flow needs this grant
— schema-level grants alone fail with error 4024. The generated
`emit_hook_events.sh` hook (from `agent_setups`) ingests into it over the
**Zerobus REST API** as that same SP. So developers still need only `READ_SECRET`
on the UC secret — no table access, no dev-machine SDK.

The generated `managed-settings.json` ships the reporting hooks regardless. The
config generator **auto-derives** the Zerobus endpoint from workspace metadata
(numeric workspace id from the `x-databricks-org-id` header + UC metastore region
+ host cloud suffix). So `zerobus_endpoint` normally stays empty. Set it only to
override the derived value. If derivation is unavailable (offline generation), the
hooks are wired but dormant (the script no-ops on an empty endpoint, which is also
`ZEROBUS_ENDPOINT`-overridable at runtime). Zerobus does not create tables. That is
why this module runs the DDL in advance. Set `hook_events_enabled = false` to skip
the table and grant entirely.

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
| `zerobus_endpoint` | `""` | Zerobus REST base URL. An empty value disables the generated hook. |
| `service_principal_display_name` | `unity-gateway-otel-telemetry` | |
| `secret_name` | `otel_exporter_oauth` | |
| `reader_groups` | `[]` | Groups granted `READ_SECRET` on the secret. |
| `databricks_profile` | — | The `local-exec` runner and grant use it. |

This module requires `python3` and the `databricks` CLI on PATH at apply time (the
`local-exec` provisioners use them). Run it from a **POSIX shell** (the READ_SECRET
grant relies on single-quote shell semantics). The outputs feed the agent-config
generator via the infra `telemetry` output.

## Operational notes

- **`reader_groups` revocation.** Each group is a separate `null_resource` with a
  destroy-time provisioner. So if you remove a group from `reader_groups` and
  apply, the destroy-time provisioner revokes its `READ_SECRET`. (Adding a group
  is create-time. Both operations go through `databricks grants update SECRET`.)
- **Table DDL is `CREATE TABLE IF NOT EXISTS`.** These are the fixed OTLP v1 spec
  schemas. If you edit a `templates/otel_*.sql` for a table that already exists,
  the change is a **no-op** — apply looks green but the live table is not altered.
  If a column ever must change, migrate the existing table manually (`ALTER TABLE`).
