# Terraform: Unity AI Gateway

Reproducible, opinionated Terraform for a Databricks **Unity AI Gateway**
deployment focused on **capturing agentic traffic** under Unity Catalog
governance.

## Layout

```
terraform/
├── modules/            # reusable building blocks
│   ├── unity-foundation/   # catalog (create OR reference) + schemas
│   └── model-service/      # FMAPI-backed model service + inference logging + rate limits + grants
└── infra/              # a concrete deployment applying the modules
```

- **`modules/`** — parameterized, environment-agnostic, no provider/backend
  config. Compose these into any deployment.
- **`infra/`** — the applyable root: provider auth, backend, variable values,
  module wiring. Defaults target the `fevm-west` sandbox.

## The two-mode design

The `unity-foundation` module has a `create_catalog` switch because the target
environments differ on one axis: **who is allowed to create catalogs**.

- `create_catalog = false` (default) — reference an existing catalog, create only
  schemas beneath it. For environments (like this sandbox) where you can create
  schemas and everything below, but not catalogs.
- `create_catalog = true` — Terraform owns the catalog too.

Everything downstream (schemas, model services, inference tables, grants) is
identical across modes.

## What a "model service" is

A model service is a first-class Unity Catalog `MODEL_SERVICE` securable that
routes to a Databricks **FMAPI** foundation model and logs traffic to a UC Delta
table. It is **not** a serving endpoint and **not** an external model. Built on
the native `databricks_ai_gateway_model_service` resource (provider ≥ 1.129.0).

## Get started

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
terraform init && terraform plan
```

See `infra/README.md` and each module's README for details.

## Requirements

- Terraform ≥ 1.5.0
- Databricks provider ≥ 1.129.0 (first version with the AI Gateway model-service
  resources)
- A `~/.databrickscfg` profile with Unity Catalog + AI Gateway access
