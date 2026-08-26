# -----------------------------------------------------------------------------
# infra: a concrete deployment of the reusable modules.
#
# Default posture matches a restricted enterprise sandbox (fevm-west):
#   * reference an existing catalog (create_catalog = false)
#   * create one schema per model provider (anthropic / openai / gemini)
#   * create an FMAPI-backed model service for each endpoint in the catalog,
#     with inference logging enabled
# -----------------------------------------------------------------------------

locals {
  # Endpoint name convention: the system.ai model minus the vendor prefixes.
  # system.ai.databricks-claude-opus-4-8 -> claude-opus-4-8
  strip = { for m in distinct(flatten([for p in var.model_providers : p.versioned_models])) :
    m => replace(replace(m, "system.ai.databricks-", ""), "system.ai.", "")
  }

  # Flatten the provider catalog into a single map of endpoints keyed by
  # "<schema>/<endpoint>", merging versionless aliases with versioned pins.
  services = merge([
    for schema, cfg in var.model_providers : merge(
      {
        for alias, model in cfg.aliases :
        "${schema}/${alias}" => { schema = schema, endpoint = alias, model = model }
      },
      {
        for model in cfg.versioned_models :
        "${schema}/${local.strip[model]}" => { schema = schema, endpoint = local.strip[model], model = model }
      },
    )
  ]...)
}

module "foundation" {
  source = "../modules/unity-foundation"

  catalog_name   = var.catalog_name
  create_catalog = var.create_catalog
  force_destroy  = var.force_destroy

  schemas = {
    for schema, cfg in var.model_providers : schema => {
      comment = cfg.schema_comment
    }
  }
}

module "model_service" {
  source   = "../modules/model-service"
  for_each = local.services

  catalog_name     = module.foundation.catalog_name
  schema_name      = each.value.schema
  model_service_id = each.value.endpoint
  foundation_model = each.value.model

  inference_logging_enabled = var.inference_logging_enabled
  rate_limits               = var.rate_limits
  execute_principals        = var.execute_principals

  # Ensure the schemas exist before their model services (and inference tables).
  depends_on = [module.foundation]
}
