# -----------------------------------------------------------------------------
# unity-foundation
#
# Two operating modes, selected by var.create_catalog:
#   * create_catalog = true  -> manage the catalog as a resource.
#   * create_catalog = false -> reference an existing catalog via a data source.
#
# In both modes the schemas are created beneath the (created or referenced)
# catalog. The data-source path also validates that the catalog actually exists
# before any schema is created, and wires an implicit dependency so ordering is
# correct.
# -----------------------------------------------------------------------------

resource "databricks_catalog" "this" {
  count = var.create_catalog ? 1 : 0

  name           = var.catalog_name
  comment        = var.catalog_comment
  owner          = var.catalog_owner
  isolation_mode = var.catalog_isolation_mode
  force_destroy  = var.force_destroy
}

data "databricks_catalog" "existing" {
  count = var.create_catalog ? 0 : 1

  name = var.catalog_name
}

locals {
  # Resolve the catalog name from whichever path is active. Referencing the
  # resource/data attribute (rather than var.catalog_name directly) is what
  # establishes the implicit dependency the schemas rely on for ordering.
  catalog_name = var.create_catalog ? databricks_catalog.this[0].name : data.databricks_catalog.existing[0].name
}

resource "databricks_schema" "this" {
  for_each = var.schemas

  catalog_name  = local.catalog_name
  name          = each.key
  comment       = each.value.comment
  properties    = each.value.properties
  owner         = var.schema_owner
  force_destroy = var.force_destroy
}
