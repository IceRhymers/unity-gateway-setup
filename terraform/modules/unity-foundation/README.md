# Module: `unity-foundation`

Provisions the Unity Catalog container for a Unity AI Gateway deployment: a
catalog (optionally) and the schemas that hold model services and their
inference-logging tables.

## Why two modes?

Many enterprise environments (and the reference sandbox this repo targets) do
**not** grant principals the right to create catalogs, but **do** allow creating
schemas and everything beneath an existing catalog. This module encodes both
realities behind a single `create_catalog` switch.

| `create_catalog` | Behavior | Use when |
|---|---|---|
| `false` (default) | Looks up an existing catalog with `data.databricks_catalog` and creates only the schemas. Fails fast if the catalog does not exist. | You cannot create catalogs (restricted workspace, shared catalog owned by platform team). |
| `true` | Creates and manages the catalog with `databricks_catalog`, then the schemas. | You own catalog creation (greenfield workspace, full admin). |

The schema resources reference the resolved catalog name from the active path,
so ordering and dependency tracking are correct in both modes.

## Usage

```hcl
# Reference-mode (default): catalog already exists, create schemas under it.
module "foundation" {
  source       = "../modules/unity-foundation"
  catalog_name = "tanner_wendland_catalog"

  schemas = {
    ai_gateway = { comment = "Unity AI Gateway model services + inference logs" }
  }
}

# Create-mode: manage the catalog too.
module "foundation" {
  source         = "../modules/unity-foundation"
  create_catalog = true
  catalog_name   = "acme_ai_gateway"
  catalog_owner  = "platform-admins"
}
```

## Key inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `catalog_name` | `string` | – | Catalog to create or reference. |
| `create_catalog` | `bool` | `false` | Select create vs. reference mode. |
| `schemas` | `map(object)` | `{ ai_gateway = {...} }` | Schemas to create, keyed by name. |
| `force_destroy` | `bool` | `false` | Allow destroying non-empty catalog/schemas. |

See `variables.tf` for the full list including create-mode-only settings.

## Outputs

| Name | Description |
|---|---|
| `catalog_name` | Resolved catalog name (created or referenced). |
| `schemas` | Map of schema name → `{ name, full_name, id }`. |
| `parent_schemas` | Map of schema name → `schemas/{catalog}.{schema}` — feed directly into the `model-service` module. |

## Permissions

- Reference mode: `USE CATALOG` on the catalog, `CREATE SCHEMA` on the catalog.
- Create mode: `CREATE CATALOG` on the metastore (plus the above).
