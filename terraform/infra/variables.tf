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
