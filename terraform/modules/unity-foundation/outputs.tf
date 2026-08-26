output "catalog_name" {
  description = "Name of the catalog the schemas were created under (created or referenced)."
  value       = local.catalog_name
}

output "catalog_managed" {
  description = "True when this module created and manages the catalog; false when it referenced an existing one."
  value       = var.create_catalog
}

output "schemas" {
  description = "Map of created schemas keyed by schema name, with name, full_name (catalog.schema) and id."
  value = {
    for name, s in databricks_schema.this : name => {
      name      = s.name
      full_name = "${local.catalog_name}.${s.name}"
      id        = s.id
    }
  }
}

output "parent_schemas" {
  description = "Map of schema name -> `schemas/{catalog}.{schema}` parent string, ready to feed the model-service module's parent."
  value = {
    for name, s in databricks_schema.this : name => "schemas/${local.catalog_name}.${s.name}"
  }
}
