output "catalog_name" {
  description = "Catalog the deployment used (created or referenced)."
  value       = module.foundation.catalog_name
}

output "provider_schemas" {
  description = "Map of provider name -> fully-qualified schema."
  value       = { for name, s in module.foundation.schemas : name => s.full_name }
}

output "endpoints" {
  description = "Map of every created endpoint keyed by <schema>/<endpoint>, with its UC name, routed model, and inference table."
  value = {
    for key, m in module.model_service : key => {
      full_name        = m.full_name
      foundation_model = m.foundation_model
      inference_table  = m.inference_table
    }
  }
}

output "endpoint_count" {
  description = "Total number of model services created."
  value       = length(module.model_service)
}

output "telemetry" {
  description = "Telemetry ingestion facts consumed by the agent-config generator (null when telemetry_enabled = false)."
  value = var.telemetry_enabled ? {
    schema_full_name                 = module.telemetry[0].schema_full_name
    tables                           = module.telemetry[0].tables
    secret_full_name                 = module.telemetry[0].secret_full_name
    service_principal_application_id = module.telemetry[0].service_principal_application_id
  } : null
}
