output "name" {
  description = "Resource name of the model service (model-services/{catalog}.{schema}.{id})."
  value       = databricks_ai_gateway_model_service.this.name
}

output "full_name" {
  description = "Three-level Unity Catalog name of the model service securable (catalog.schema.id)."
  value       = local.service_full_name
}

output "model_service_id" {
  description = "Leaf id of the model service."
  value       = var.model_service_id
}

output "foundation_model" {
  description = "Resolved FMAPI model reference the service routes to."
  value       = local.model_ref
}

output "supported_api_types" {
  description = "API types the service exposes (computed by the platform)."
  value       = databricks_ai_gateway_model_service.this.supported_api_types
}

output "inference_table" {
  description = "Fully-qualified inference (payload) table, or null when logging is disabled."
  value       = var.inference_logging_enabled ? try(databricks_ai_gateway_model_service.this.config.inference_table.table, null) : null
}
