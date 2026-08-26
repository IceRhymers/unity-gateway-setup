# -----------------------------------------------------------------------------
# model-service
#
# Creates a Unity Catalog MODEL_SERVICE securable via the native
# databricks_ai_gateway_model_service resource (UC AI Gateway API,
# /api/2.1/unity-catalog/model-services). The service routes to a Databricks
# Foundation Model API (FMAPI) model on a pay-per-token basis, and (by default)
# logs all traffic to a UC Delta inference table for agentic capture.
#
# This is distinct from databricks_model_serving: a model service is its own UC
# securable, not a serving endpoint.
# -----------------------------------------------------------------------------

locals {
  parent = "schemas/${var.catalog_name}.${var.schema_name}"

  # Accept either "system.ai.<model>" or a pre-qualified "models/system.ai.<model>".
  model_ref = startswith(var.foundation_model, "models/") ? var.foundation_model : "models/${var.foundation_model}"

  # Inference table placement, defaulting to the service's own catalog/schema.
  it_catalog = coalesce(var.inference_table_catalog, var.catalog_name)
  it_schema  = coalesce(var.inference_table_schema, var.schema_name)
  it_parent  = "schemas/${local.it_catalog}.${local.it_schema}"
  it_prefix  = coalesce(var.inference_table_prefix, var.model_service_id)

  # Three-level UC name used as the grant target.
  service_full_name = "${var.catalog_name}.${var.schema_name}.${var.model_service_id}"
}

resource "databricks_ai_gateway_model_service" "this" {
  parent           = local.parent
  model_service_id = var.model_service_id
  comment          = var.comment
  owner            = var.owner

  config = {
    routing = {
      destinations = [
        {
          name               = var.destination_name
          destination_type   = "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL"
          traffic_percentage = var.traffic_percentage
          pay_per_token_config = {
            model = local.model_ref
          }
        }
      ]
    }

    # Logging is the whole point for agentic capture. When disabled we omit the
    # block entirely rather than provisioning a table we never write to.
    inference_table = var.inference_logging_enabled ? {
      parent            = local.it_parent
      table_name_prefix = local.it_prefix
      disabled          = false
    } : null

    rate_limits = [
      for r in var.rate_limits : {
        key               = r.key
        renewal_period    = r.renewal_period
        requests          = r.requests
        tokens            = r.tokens
        principal         = r.principal
        request_tag_key   = r.request_tag_key
        request_tag_value = r.request_tag_value
      }
    ]
  }
}

# Additive EXECUTE grants (databricks_grant, not the authoritative _grants) so we
# never clobber ACLs set elsewhere. Depends on the service existing first.
resource "databricks_grant" "execute" {
  for_each = toset(var.execute_principals)

  model_service = local.service_full_name
  principal     = each.value
  privileges    = ["EXECUTE"]

  depends_on = [databricks_ai_gateway_model_service.this]
}
