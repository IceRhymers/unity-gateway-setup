# infra

A concrete, applyable deployment that wires the reusable modules into a working
Unity AI Gateway footprint. Defaults target the `fevm-west` sandbox in
reference-catalog mode.

## What it deploys

1. `unity-foundation` — references (or creates) the catalog and creates the
   `ai_gateway` schema.
2. `model-service` — one FMAPI-backed model service with inference logging
   enabled, so agent traffic is captured to a UC Delta table.

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
| `gateway_schema` | Schema holding the services. |
| `model_service_full_name` | `catalog.schema.id` of the service. |
| `inference_table` | UC table capturing request/response payloads. |

## State

Local state by default. For team use, uncomment and configure a remote backend
in `versions.tf` before the first `init`.
