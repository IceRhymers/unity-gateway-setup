# -----------------------------------------------------------------------------
# telemetry
#
# Provisions the ingestion side of Claude Code / coding-agent OpenTelemetry:
#
#   1. a telemetry schema (optional) inside an existing catalog;
#   2. the three OTEL Delta tables (metrics / logs / traces) the Databricks
#      /api/2.0/otel endpoint writes to — created via the Statement Execution
#      API because their schema is deeply nested (see scripts/create_otel_table.py);
#   3. a Databricks-managed service principal + a workspace-level OAuth secret —
#      the identity telemetry is attributed to (so developers never need MODIFY
#      on the tables, only READ_SECRET on the secret below);
#   4. a databricks_secret_uc holding that SP's client_id/client_secret, read at
#      runtime by each developer's otelHeadersHelper to mint the bearer token;
#   5. grants: the SP gets USE/MODIFY/SELECT on the telemetry objects; the
#      configured reader_groups get READ_SECRET on the UC secret.
#
# Everything runs through the workspace-level provider (api = "workspace"); no
# account-level provider is required.
# -----------------------------------------------------------------------------

locals {
  # Resolve each enabled signal to its leaf + fully-qualified table name.
  default_table_names = { for s in var.signals : s => "claude_otel_${s}" }
  leaf_tables         = { for s in var.signals : s => lookup(var.table_names, s, local.default_table_names[s]) }
  schema_full         = "${var.catalog_name}.${var.schema_name}"
  fq_tables           = { for s, leaf in local.leaf_tables : s => "${local.schema_full}.${leaf}" }

  # Hook-event table: custom Claude Code reporting events (see templates/hook_events.sql).
  hook_events_table_fq = "${local.schema_full}.${var.hook_events_table_name}"
}

# --- 1. telemetry schema ---------------------------------------------------

resource "databricks_schema" "this" {
  count = var.create_schema ? 1 : 0

  catalog_name  = var.catalog_name
  name          = var.schema_name
  comment       = var.schema_comment
  owner         = var.owner
  force_destroy = var.force_destroy
}

# --- 2. OTEL signal tables -------------------------------------------------
#
# The OTLP schema is far too nested to model cleanly as databricks_sql_table
# column blocks, and the /api/2.0/otel endpoint requires the tables to pre-exist
# (it does not auto-create them). We render the canonical DDL and run it on a
# warehouse. CREATE TABLE IF NOT EXISTS is idempotent; the trigger re-runs when
# the DDL or target name changes.
#
# CAUTION: because the DDL is IF NOT EXISTS, editing templates/otel_*.sql for a
# table that ALREADY exists is a no-op — apply reports success but the live table
# is NOT altered (silent schema drift). These are the fixed OTLP v1 spec schemas
# and should not need edits; if a column ever must change, migrate the existing
# table manually (ALTER TABLE) — do not rely on this resource to do it.

resource "null_resource" "otel_table" {
  for_each = local.fq_tables

  triggers = {
    ddl_sha   = filesha256("${path.module}/templates/otel_${each.key}.sql")
    table     = each.value
    warehouse = var.warehouse_id
    profile   = var.databricks_profile
  }

  provisioner "local-exec" {
    command = join(" ", [
      "python3",
      "${path.module}/scripts/create_otel_table.py",
      "--ddl-file", "${path.module}/templates/otel_${each.key}.sql",
      "--table", each.value,
      "--warehouse-id", var.warehouse_id,
      "--profile", var.databricks_profile,
      "--databricks-bin", var.databricks_bin,
    ])
  }

  depends_on = [databricks_schema.this]
}

# --- 2b. hook-event table --------------------------------------------------
#
# The Zerobus REST ingest API (used by the generated emit_hook_events.sh hook)
# does NOT create tables — the target managed Delta table must pre-exist. Same
# Statement-Execution runner and IF NOT EXISTS idempotency as the OTEL tables.

resource "null_resource" "hook_events_table" {
  count = var.hook_events_enabled ? 1 : 0

  triggers = {
    ddl_sha   = filesha256("${path.module}/templates/hook_events.sql")
    table     = local.hook_events_table_fq
    warehouse = var.warehouse_id
    profile   = var.databricks_profile
  }

  provisioner "local-exec" {
    command = join(" ", [
      "python3",
      "${path.module}/scripts/create_otel_table.py",
      "--ddl-file", "${path.module}/templates/hook_events.sql",
      "--table", local.hook_events_table_fq,
      "--warehouse-id", var.warehouse_id,
      "--profile", var.databricks_profile,
      "--databricks-bin", var.databricks_bin,
    ])
  }

  depends_on = [databricks_schema.this]
}

# --- 3. service principal + OAuth secret -----------------------------------

resource "databricks_service_principal" "otel" {
  # Databricks-managed SP (no application_id): works on AWS and Azure and is
  # eligible for OAuth M2M secrets. api = "workspace" keeps this on the
  # workspace-level SCIM API so no account-level provider is needed.
  display_name = var.service_principal_display_name
  api          = "workspace"

  # Required for OTLP ingestion: the exporter authenticates as this SP and POSTs
  # to the workspace REST API at /api/2.0/otel/v1/{metrics,logs,traces}. Any
  # workspace API rejects an identity without workspace-access with HTTP 403
  # ("This API is disabled for users without the workspace-access entitlement"),
  # so without this the UC grants below are not enough — every export is refused
  # before it reaches the tables. Workspace-local SPs are NOT granted this by
  # default here, so set it explicitly.
  workspace_access = true
}

resource "databricks_service_principal_secret" "otel" {
  service_principal_id = databricks_service_principal.otel.id
  api                  = "workspace"
  lifetime             = var.secret_lifetime
}

# --- 4. UC secret holding the OAuth credentials ----------------------------

resource "databricks_secret_uc" "otel" {
  catalog_name = var.catalog_name
  schema_name  = var.schema_name
  name         = var.secret_name
  owner        = var.secret_owner
  comment      = "OAuth client_id/client_secret for the OTEL exporter service principal. Read by otelHeadersHelper to mint the ingestion bearer token."

  # JSON blob so the helper needs only the secret's full name + its own auth.
  value = jsonencode({
    client_id     = databricks_service_principal.otel.application_id
    client_secret = databricks_service_principal_secret.otel.secret
  })

  depends_on = [databricks_schema.this]
}

# --- 5. grants -------------------------------------------------------------
#
# databricks_grant (singular, non-authoritative) so we only add the SP's
# privileges without clobbering existing grants on a shared catalog/schema.

resource "databricks_grant" "sp_catalog" {
  catalog    = var.catalog_name
  principal  = databricks_service_principal.otel.application_id
  privileges = ["USE_CATALOG"]
}

resource "databricks_grant" "sp_schema" {
  schema     = local.schema_full
  principal  = databricks_service_principal.otel.application_id
  privileges = ["USE_SCHEMA", "MODIFY", "SELECT"]

  depends_on = [databricks_schema.this]
}

# Zerobus's authorization_details OAuth flow requires an EXPLICIT table-level
# grant on the ingest SP — the schema-level MODIFY above is not sufficient and
# ingest fails with error 4024. This grant is redundant for the OTLP tables (the
# /api/2.0/otel path is satisfied by the schema grant) but mandatory for Zerobus.
resource "databricks_grant" "sp_hook_events_table" {
  count = var.hook_events_enabled ? 1 : 0

  table      = local.hook_events_table_fq
  principal  = databricks_service_principal.otel.application_id
  privileges = ["MODIFY", "SELECT"]

  depends_on = [null_resource.hook_events_table]
}

# READ_SECRET has no securable in databricks_grant(s) as of provider 1.129.0, so
# grant it through the UC permissions API. One resource PER principal (not one
# batched call) so that dropping a group from reader_groups destroys its resource
# and the destroy-time provisioner REVOKES the grant — otherwise access would be
# add-only from Terraform. Runs on a POSIX shell (single-quoted JSON).
resource "null_resource" "secret_reader" {
  for_each = toset(var.reader_groups)

  triggers = {
    secret     = databricks_secret_uc.otel.full_name
    principal  = each.value
    profile    = var.databricks_profile
    databricks = var.databricks_bin
  }

  # Add READ_SECRET for this principal.
  provisioner "local-exec" {
    command = "${self.triggers.databricks} grants update SECRET ${self.triggers.secret} --profile ${self.triggers.profile} --json '${jsonencode({ changes = [{ principal = each.value, add = ["READ_SECRET"] }] })}'"
  }

  # Revoke it when the principal is removed from reader_groups (or on destroy).
  # on_failure = continue so an already-absent grant does not wedge destroy.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "${self.triggers.databricks} grants update SECRET ${self.triggers.secret} --profile ${self.triggers.profile} --json '{\"changes\":[{\"principal\":\"${self.triggers.principal}\",\"remove\":[\"READ_SECRET\"]}]}'"
  }
}
