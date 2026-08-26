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

variable "gateway_schema" {
  description = "Schema that holds the model services and their inference tables."
  type        = string
  default     = "ai_gateway"
}

variable "force_destroy" {
  description = "Allow destroying non-empty catalog/schemas. Keep false in shared workspaces."
  type        = bool
  default     = false
}

# ---- model service ----

variable "model_service_id" {
  description = "Leaf name of the model service to create."
  type        = string
  default     = "agent_gateway"
}

variable "foundation_model" {
  description = "FMAPI model the service routes to (UC registered-model name)."
  type        = string
  default     = "system.ai.databricks-claude-sonnet-4-5"
}

variable "rate_limits" {
  description = "Rate limits for the model service (see module for the object shape)."
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
  description = "Principals granted EXECUTE on the model service."
  type        = list(string)
  default     = []
}
