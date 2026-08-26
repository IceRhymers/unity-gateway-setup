# ---- placement (the model service is itself a UC securable) ----

variable "catalog_name" {
  description = "Catalog that holds the model service securable."
  type        = string
}

variable "schema_name" {
  description = "Schema that holds the model service securable."
  type        = string
}

variable "model_service_id" {
  description = "Leaf name of the model service (the third level of the UC name catalog.schema.<id>)."
  type        = string
}

# ---- routing to a Foundation Model API (FMAPI) model ----

variable "foundation_model" {
  description = <<-EOT
    The FMAPI (Foundation Model API) model this service routes to. Provide the
    Unity Catalog registered-model name (e.g. "system.ai.databricks-claude-sonnet-4-5");
    the module prepends the required "models/" prefix. A fully-qualified
    "models/system.ai.<model>" is also accepted as-is.

    Model services route to Databricks-hosted FMAPI models, NOT external models.
    For external providers use a model-provider-service (separate securable).
  EOT
  type        = string

  validation {
    condition     = length(trimspace(var.foundation_model)) > 0
    error_message = "foundation_model must be a non-empty UC model name."
  }
}

variable "destination_name" {
  description = "Human-readable name for the routing destination."
  type        = string
  default     = "primary"
}

variable "traffic_percentage" {
  description = "Percentage of traffic sent to this destination (single-destination default = 100)."
  type        = number
  default     = 100
}

# ---- inference logging (the agentic-capture mechanism) ----

variable "inference_logging_enabled" {
  description = "Log every request/response to a Unity Catalog Delta table. This is the core mechanism for capturing agentic traffic; on by default."
  type        = bool
  default     = true
}

variable "inference_table_catalog" {
  description = "Catalog for the inference table. Null falls back to catalog_name."
  type        = string
  default     = null
}

variable "inference_table_schema" {
  description = "Schema for the inference table. Null falls back to schema_name."
  type        = string
  default     = null
}

variable "inference_table_prefix" {
  description = "Table-name prefix for the inference table (actual table is <prefix>_payload). Null falls back to model_service_id."
  type        = string
  default     = null
}

# ---- rate limits ----

variable "rate_limits" {
  description = <<-EOT
    Rate limits applied to the service. Each entry:
      key            - RATE_LIMIT_KEY_USER | RATE_LIMIT_KEY_SERVICE_PRINCIPAL | RATE_LIMIT_KEY_ENDPOINT | RATE_LIMIT_KEY_REQUEST_TAG ...
      renewal_period - RATE_LIMIT_RENEWAL_PERIOD_MINUTE | RATE_LIMIT_RENEWAL_PERIOD_HOUR
      requests       - max requests per period (optional)
      tokens         - max tokens per period (optional)
      principal      - specific user/SP the limit applies to (optional)
      request_tag_key / request_tag_value - for REQUEST_TAG-keyed limits (optional)
  EOT
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

# ---- governance ----

variable "comment" {
  description = "Comment stored on the model service securable."
  type        = string
  default     = "Managed by Terraform (unity-gateway-setup): FMAPI-backed AI Gateway model service"
}

variable "owner" {
  description = "Owner (user or group) of the model service securable. Null keeps the creating principal."
  type        = string
  default     = null
}

variable "execute_principals" {
  description = "Principals (users/groups/SPs) to grant EXECUTE on the model service so they can query it."
  type        = list(string)
  default     = []
}
