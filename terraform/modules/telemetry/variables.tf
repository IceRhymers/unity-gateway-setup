# -----------------------------------------------------------------------------
# telemetry module inputs
# -----------------------------------------------------------------------------

variable "catalog_name" {
  description = "Catalog that holds the telemetry schema, tables, and UC secret. Must already exist (typically the gateway catalog)."
  type        = string
}

variable "schema_name" {
  description = "Schema (inside catalog_name) for the OTEL tables and the UC secret."
  type        = string
  default     = "telemetry"
}

variable "create_schema" {
  description = "Create the telemetry schema (true) or assume it already exists (false)."
  type        = bool
  default     = true
}

variable "schema_comment" {
  description = "Comment applied to the telemetry schema when create_schema = true."
  type        = string
  default     = "Claude Code / coding-agent OpenTelemetry ingestion (managed by unity-gateway-setup)"
}

# ---- OTEL signal tables ----

variable "signals" {
  description = "Which OTEL signals to provision tables for. Any of: metrics, logs, traces."
  type        = list(string)
  default     = ["metrics", "logs", "traces"]

  validation {
    condition     = length(setsubtract(toset(var.signals), toset(["metrics", "logs", "traces"]))) == 0
    error_message = "signals may only contain: metrics, logs, traces."
  }
}

variable "table_names" {
  description = "Leaf table name per signal. Keys not present fall back to claude_otel_<signal>."
  type        = map(string)
  default     = {}
}

variable "warehouse_id" {
  description = "SQL warehouse ID used to run the CREATE TABLE DDL (serverless recommended; it auto-starts on demand)."
  type        = string

  validation {
    condition     = length(trimspace(var.warehouse_id)) > 0
    error_message = "warehouse_id is required to create the OTEL tables. Set telemetry_warehouse_id."
  }
}

# ---- service principal + credential ----

variable "service_principal_display_name" {
  description = "Display name for the Databricks-managed service principal that owns telemetry ingestion."
  type        = string
  default     = "unity-gateway-otel-telemetry"
}

variable "secret_lifetime" {
  description = "Lifetime of the SP OAuth secret, formatted as NNNNs (e.g. 63072000s = 730d). Null uses the provider default."
  type        = string
  default     = null
}

variable "secret_name" {
  description = "Name (leaf) of the databricks_secret_uc that stores the SP OAuth client_id/client_secret."
  type        = string
  default     = "otel_exporter_oauth"
}

variable "secret_owner" {
  description = "Owner (user or group) of the UC secret. Null keeps the creating principal."
  type        = string
  default     = null
}

variable "reader_groups" {
  description = "Groups (or users) granted READ_SECRET on the UC secret — the developers whose otelHeadersHelper reads it. Empty = grant nothing (operator grants access out of band)."
  type        = list(string)
  default     = []
}

# ---- shared ----

variable "owner" {
  description = "Owner (user or group) applied to the telemetry schema when created. Null keeps the creating principal."
  type        = string
  default     = null
}

variable "force_destroy" {
  description = "Allow destroying the telemetry schema even when it still contains tables. Keep false in shared environments."
  type        = bool
  default     = false
}

variable "databricks_profile" {
  description = "Databricks CLI profile used by the table-DDL runner and the READ_SECRET grant (local-exec)."
  type        = string
}

variable "databricks_bin" {
  description = "Path to the databricks CLI used by local-exec provisioners."
  type        = string
  default     = "databricks"
}
