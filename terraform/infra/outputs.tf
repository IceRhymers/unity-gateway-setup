output "catalog_name" {
  description = "Catalog the deployment used (created or referenced)."
  value       = module.foundation.catalog_name
}

output "gateway_schema" {
  description = "Schema holding the model services."
  value       = module.foundation.schemas[var.gateway_schema].full_name
}

output "model_service_name" {
  description = "Resource name of the created model service."
  value       = module.model_service.name
}

output "model_service_full_name" {
  description = "Three-level UC name of the model service."
  value       = module.model_service.full_name
}

output "inference_table" {
  description = "UC Delta table capturing request/response payloads."
  value       = module.model_service.inference_table
}
