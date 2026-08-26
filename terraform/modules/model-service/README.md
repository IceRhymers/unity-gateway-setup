# Module: `model-service`

Creates a Unity Catalog **model service** — a first-class `MODEL_SERVICE`
securable that routes to a Databricks **Foundation Model API (FMAPI)** model and
(by default) logs all traffic to a UC Delta inference table.

Backed by the native `databricks_ai_gateway_model_service` resource
(provider ≥ 1.129.0), which wraps the UC AI Gateway API at
`/api/2.1/unity-catalog/model-services`.

## Model services vs. serving endpoints / external models

- A **model service** is its own UC securable (not a `databricks_model_serving`
  endpoint) and routes to **FMAPI** Databricks-hosted models.
- **External** providers are a separate securable
  (`databricks_ai_gateway_model_provider_service`) that a model service can
  reference. This module targets the FMAPI (pay-per-token) path — the opinionated
  default for capturing first-party agentic traffic under UC governance.

## Usage

```hcl
module "gateway_service" {
  source           = "../modules/model-service"
  catalog_name     = module.foundation.catalog_name
  schema_name      = "ai_gateway"
  model_service_id = "agent_gateway"

  foundation_model = "system.ai.databricks-claude-sonnet-4-5"

  # Inference logging is on by default -> tanner_wendland_catalog.ai_gateway.agent_gateway_payload
  inference_logging_enabled = true

  rate_limits = [
    { key = "RATE_LIMIT_KEY_USER", renewal_period = "RATE_LIMIT_RENEWAL_PERIOD_MINUTE", requests = 100 },
  ]

  execute_principals = ["data-scientists"]
}
```

## Key inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `catalog_name` / `schema_name` | `string` | – | Where the model service securable lives. |
| `model_service_id` | `string` | – | Leaf name. |
| `foundation_model` | `string` | – | FMAPI model, e.g. `system.ai.databricks-claude-sonnet-4-5`. |
| `inference_logging_enabled` | `bool` | `true` | Log request/response to a UC table. |
| `inference_table_{catalog,schema,prefix}` | `string` | fall back to service placement | Where the payload table lands. |
| `rate_limits` | `list(object)` | `[]` | Per-key request/token limits. |
| `execute_principals` | `list(string)` | `[]` | Principals granted `EXECUTE`. |

## Outputs

| Name | Description |
|---|---|
| `name` | `model-services/{catalog}.{schema}.{id}`. |
| `full_name` | `catalog.schema.id`. |
| `inference_table` | Fully-qualified payload table (or null if logging disabled). |
| `supported_api_types` | Platform-computed API types. |

## Permissions

Creating the service needs `CREATE_SERVICE` + `USE SCHEMA` + `USE CATALOG` on the
parent, `EXECUTE` on the referenced FMAPI model, and `CREATE TABLE` on the
inference-table schema when logging is enabled.

## Extending

The underlying resource also supports traffic splitting, a `fallback` routing
block, provisioned-throughput destinations, and external-model destinations (via
a model-provider-service). This module intentionally ships the single-FMAPI-
destination path; add those blocks as your governance needs grow.
