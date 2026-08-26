# -----------------------------------------------------------------------------
# infra: a concrete deployment of the reusable modules.
#
# Default posture matches a restricted enterprise sandbox (fevm-west):
#   * reference an existing catalog (create_catalog = false)
#   * create the gateway schema beneath it
#   * create one FMAPI-backed model service with inference logging enabled
# -----------------------------------------------------------------------------

module "foundation" {
  source = "../modules/unity-foundation"

  catalog_name   = var.catalog_name
  create_catalog = var.create_catalog
  force_destroy  = var.force_destroy

  schemas = {
    (var.gateway_schema) = {
      comment = "Unity AI Gateway model services and inference logging tables"
    }
  }
}

module "model_service" {
  source = "../modules/model-service"

  catalog_name     = module.foundation.catalog_name
  schema_name      = var.gateway_schema
  model_service_id = var.model_service_id
  foundation_model = var.foundation_model

  inference_logging_enabled = true

  rate_limits        = var.rate_limits
  execute_principals = var.execute_principals

  # Ensure the schema exists before the model service (and its inference table)
  # are created.
  depends_on = [module.foundation]
}
