output "schema_full_name" {
  description = "Fully-qualified telemetry schema (catalog.schema)."
  value       = local.schema_full
}

output "tables" {
  description = "Map of signal -> fully-qualified OTEL table name."
  value       = local.fq_tables
}

output "service_principal_id" {
  description = "SCIM ID of the telemetry service principal."
  value       = databricks_service_principal.otel.id
}

output "service_principal_application_id" {
  description = "Application (client) ID of the telemetry service principal. Not sensitive."
  value       = databricks_service_principal.otel.application_id
}

output "secret_full_name" {
  description = "Three-level name of the UC secret the otelHeadersHelper reads (catalog.schema.secret)."
  value       = databricks_secret_uc.otel.full_name
}
