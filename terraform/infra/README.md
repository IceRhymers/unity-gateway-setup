# infra

A concrete, applyable deployment that wires the reusable modules into a working
Unity AI Gateway footprint. Defaults target the `fevm-west` sandbox in
reference-catalog mode.

## What it deploys

1. `unity-foundation` — references (or creates) the catalog and creates one
   schema per model provider (`anthropic`, `openai`, `gemini`).
2. `model-service` (fanned out with `for_each`) — an FMAPI-backed model service
   for every endpoint in the provider catalog, each with inference logging
   enabled so traffic is captured to a UC Delta table.
3. `telemetry` (when `telemetry_enabled = true`, the default) — the OpenTelemetry
   ingestion stack: a `telemetry` schema, the OTEL metrics/logs/traces Delta
   tables, a Databricks-managed service principal + workspace OAuth secret, a
   `databricks_secret_uc` holding those credentials, and the grants. Coding
   agents emit OTLP here; see [modules/telemetry](../modules/telemetry/README.md).

### Provider catalog

`var.model_providers` maps each provider (schema) to two kinds of endpoints:

- **aliases** — versionless names pointing to the current latest model
  (`claude-opus` → opus 5, `gpt` → the flagship, `gemini-pro` → gemini 3 pro).
- **versioned_models** — version-pinned endpoints whose name is the `system.ai`
  model minus the `databricks-` prefix (`…databricks-claude-opus-4-8` →
  `claude-opus-4-8`). These serve power users pinning a version and harnesses
  (e.g. Claude Code) that hardcode names like `claude-haiku-4-5`.

The default is a **curated-recent** catalog (latest of each aliased family plus a
recent prior). Edit `model_providers` to widen/narrow endpoints or add providers.

## Quickstart

```bash
cd terraform/infra
cp terraform.tfvars.example terraform.tfvars   # edit values
terraform init
terraform plan
terraform apply
```

Validate without credentials (no provider network calls):

```bash
terraform init -backend=false
terraform validate
```

## Switching modes

- **Restricted env (default):** `create_catalog = false`, set `catalog_name` to
  an existing catalog you have `CREATE SCHEMA` on.
- **Greenfield / admin:** `create_catalog = true` to have Terraform own the
  catalog too.

## Outputs

| Output | Description |
|---|---|
| `catalog_name` | Catalog used. |
| `provider_schemas` | Map of provider → fully-qualified schema. |
| `endpoints` | Map of `<schema>/<endpoint>` → `{ full_name, foundation_model, inference_table }`. |
| `endpoint_count` | Total number of model services created. |
| `telemetry` | Telemetry facts (schema, OTEL tables, UC secret, SP app id) consumed by the agent-config generator; `null` when disabled. |

## Telemetry

`telemetry_enabled = true` (default) provisions OTEL ingestion. It **requires a
SQL warehouse** to create the tables — resolved by name via
`telemetry_warehouse_name` (default `"Serverless Starter Warehouse"`; it
auto-starts), or set `telemetry_warehouse_id` to skip the lookup. Grant
developers read access to the credential by
listing their group in `telemetry_reader_groups`, or grant `READ_SECRET` on the
UC secret out of band. After `apply`, regenerate the Claude Code settings
(`make agent-claude-code`) — the generator reads the `telemetry` output and adds
the OTEL env + `otelHeadersHelper`. Set `telemetry_enabled = false` to skip it.

## State

Local state by default. For team use, uncomment and configure a remote backend
in `versions.tf` before the first `init`.
