variable "databricks_profile" {
  description = "Profile in ~/.databrickscfg to authenticate with."
  type        = string
  default     = "fevm-west"
}

# ---- foundation ----

variable "catalog_name" {
  description = "Catalog for the gateway objects. Referenced by default (see create_catalog)."
  type        = string
  default     = "tanner_wendland_catalog"
}

variable "create_catalog" {
  description = "Create the catalog (true) or reference an existing one (false). Default false to match restricted environments where you can create schemas but not catalogs."
  type        = bool
  default     = false
}

variable "force_destroy" {
  description = "Allow destroying non-empty catalog/schemas. Keep false in shared workspaces."
  type        = bool
  default     = false
}

# ---- provider catalog ----
#
# One schema per model provider. Within each provider we create model services
# (the "endpoints") of two kinds:
#   * aliases          - versionless names that point to the current latest model
#                        (e.g. claude-opus -> opus 5). The map key is the endpoint
#                        name; the value is the system.ai model it routes to.
#   * versioned_models - system.ai models that get a version-pinned endpoint. The
#                        endpoint name is derived by stripping the "databricks-"
#                        prefix (system.ai.databricks-claude-opus-4-8 ->
#                        claude-opus-4-8). These exist for power users pinning a
#                        version and for harnesses (e.g. Claude Code) that hardcode
#                        model-name patterns like claude-haiku-4-5.
#
# The schema name is the provider key (anthropic / openai / gemini / ...).

variable "model_providers" {
  description = "Per-provider schema + endpoint catalog. See file header for the alias vs versioned distinction."
  type = map(object({
    schema_comment   = optional(string)
    aliases          = optional(map(string), {})
    versioned_models = optional(list(string), [])
  }))

  # Versioned scope is "curated recent": the latest of each aliased family plus
  # one recent prior where one exists (not every system.ai version). Edit the
  # versioned_models lists to widen or narrow this.
  default = {
    anthropic = {
      schema_comment = "Unity AI Gateway model services for Anthropic Claude"
      # Versionless aliases -> current latest model.
      aliases = {
        "claude-fable"  = "system.ai.databricks-claude-fable-5"
        "claude-opus"   = "system.ai.databricks-claude-opus-5"
        "claude-sonnet" = "system.ai.databricks-claude-sonnet-5"
        "claude-haiku"  = "system.ai.databricks-claude-haiku-4-5"
      }
      # Version-pinned endpoints (name = model minus the databricks- prefix).
      versioned_models = [
        "system.ai.databricks-claude-opus-5",
        "system.ai.databricks-claude-opus-4-8",
        "system.ai.databricks-claude-sonnet-5",
        "system.ai.databricks-claude-sonnet-4-6",
        "system.ai.databricks-claude-haiku-4-5",
        "system.ai.databricks-claude-fable-5",
      ]
    }

    openai = {
      schema_comment = "Unity AI Gateway model services for OpenAI GPT"
      # `gpt` tracks the current flagship (sol today); gpt-sol/luna/terra track
      # each 5.6 line explicitly.
      aliases = {
        "gpt"       = "system.ai.databricks-gpt-5-6-sol"
        "gpt-sol"   = "system.ai.databricks-gpt-5-6-sol"
        "gpt-luna"  = "system.ai.databricks-gpt-5-6-luna"
        "gpt-terra" = "system.ai.databricks-gpt-5-6-terra"
        "gpt-mini"  = "system.ai.databricks-gpt-5-4-mini"
        "gpt-nano"  = "system.ai.databricks-gpt-5-4-nano"
      }
      versioned_models = [
        "system.ai.databricks-gpt-5-6-sol",
        "system.ai.databricks-gpt-5-6-luna",
        "system.ai.databricks-gpt-5-6-terra",
        "system.ai.databricks-gpt-5-5",
        "system.ai.databricks-gpt-5-4-mini",
        "system.ai.databricks-gpt-5-4-nano",
      ]
    }

    gemini = {
      schema_comment = "Unity AI Gateway model services for Google Gemini"
      aliases = {
        "gemini-pro"   = "system.ai.databricks-gemini-3-pro"
        "gemini-flash" = "system.ai.databricks-gemini-3-7-flash"
      }
      versioned_models = [
        "system.ai.databricks-gemini-3-pro",
        "system.ai.databricks-gemini-3-1-pro",
        "system.ai.databricks-gemini-3-7-flash",
        "system.ai.databricks-gemini-3-6-flash",
      ]
    }

    open_models = {
      schema_comment = "Unity AI Gateway model services for open-weight / open models"
      # Named explicitly (via aliases) so endpoint names stay clean — e.g. the
      # deepseek build stamps (0813/0731) are dropped from the endpoint name.
      aliases = {
        "kimi-k3"           = "system.ai.databricks-kimi-k3"
        "glm-5-2"           = "system.ai.databricks-glm-5-2"
        "deepseek-v4-pro"   = "system.ai.databricks-deepseek-v4-pro-0813"
        "deepseek-v4-flash" = "system.ai.databricks-deepseek-v4-flash-0731"
      }
      versioned_models = []
    }
  }
}

# ---- governance applied to every endpoint ----

variable "inference_logging_enabled" {
  description = "Enable request/response logging on every endpoint (the agentic-capture mechanism)."
  type        = bool
  default     = true
}

variable "rate_limits" {
  description = "Rate limits applied to every model service (see module for object shape)."
  type = list(object({
    key               = string
    renewal_period    = string
    requests          = optional(number)
    tokens            = optional(number)
    principal         = optional(string)
    request_tag_key   = optional(string)
    request_tag_value = optional(string)
  }))
  default = []
}

variable "execute_principals" {
  description = "Principals granted EXECUTE on every model service."
  type        = list(string)
  default     = []
}

# ---- telemetry (OpenTelemetry ingestion for coding agents) ----
#
# Provisions the ingestion side of Claude Code telemetry: a telemetry schema,
# the OTEL metrics/logs/traces Delta tables, a service principal + UC secret
# holding its OAuth credentials, and the grants. The generated Claude Code
# managed-settings.json wires agents to emit OTLP here via an otelHeadersHelper
# that mints the service principal's token (no per-developer table access).

variable "telemetry_enabled" {
  description = "Provision the telemetry ingestion stack (schema, OTEL tables, service principal, UC secret, grants)."
  type        = bool
  default     = true
}

variable "telemetry_schema" {
  description = "Schema (inside catalog_name) for the OTEL tables and the UC secret."
  type        = string
  default     = "telemetry"
}

variable "telemetry_create_schema" {
  description = "Create the telemetry schema (true) or assume it already exists (false)."
  type        = bool
  default     = true
}

variable "telemetry_warehouse_name" {
  description = "Name of the SQL warehouse used to create the OTEL tables (case-sensitive). Resolved to an ID via a data lookup unless telemetry_warehouse_id is set. Serverless recommended; it auto-starts on demand."
  type        = string
  default     = "Serverless Starter Warehouse"
}

variable "telemetry_warehouse_id" {
  description = "Explicit SQL warehouse ID override. Empty (default) looks the warehouse up by telemetry_warehouse_name."
  type        = string
  default     = ""
}

variable "telemetry_signals" {
  description = "OTEL signals to provision tables for. Any of: metrics, logs, traces."
  type        = list(string)
  default     = ["metrics", "logs", "traces"]
}

variable "telemetry_reader_groups" {
  description = "Groups/users granted READ_SECRET on the telemetry UC secret (the developers whose otelHeadersHelper reads it). Empty = grant out of band."
  type        = list(string)
  default     = []
}

variable "telemetry_service_principal_display_name" {
  description = "Display name for the telemetry ingestion service principal."
  type        = string
  default     = "unity-gateway-otel-telemetry"
}

variable "telemetry_secret_lifetime" {
  description = "Lifetime of the SP OAuth secret formatted as NNNNs (e.g. 63072000s). Null uses the provider default."
  type        = string
  default     = null
}

# ---- hook events (custom Claude Code reporting via Zerobus REST) ----
#
# Complements native OTEL: a Delta table for the hook-only events native OTEL
# does not emit (agent-usage attribution, reliability stalls, governance signals,
# workflow adoption). The generated managed-settings.json registers a hook that
# streams these to Zerobus REST as the telemetry service principal.

variable "telemetry_hook_events_enabled" {
  description = "Provision the claude_hook_events table + grant for hook-based reporting (agent-usage, reliability, governance, adoption)."
  type        = bool
  default     = true
}

variable "telemetry_hook_events_table_name" {
  description = "Leaf name of the hook-event table inside the telemetry schema."
  type        = string
  default     = "claude_hook_events"
}

variable "telemetry_zerobus_endpoint" {
  description = <<-EOT
    Zerobus REST ingest base URL for this workspace (baked into the generated hook).
    Format: https://<workspace-id>.zerobus.<region>.cloud.databricks.com
    (.gcp.databricks.com on GCP; .azuredatabricks.net on Azure). <workspace-id> is
    the numeric ID from the workspace URL (`?o=` value). Empty (default) still ships
    the hooks in managed-settings.json; they stay dormant (no-op) until this is set.
  EOT
  type        = string
  default     = ""
}
