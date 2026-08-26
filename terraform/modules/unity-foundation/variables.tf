variable "catalog_name" {
  description = "Name of the Unity Catalog catalog that holds the AI Gateway objects. In reference mode it must already exist; in create mode it will be created."
  type        = string
}

variable "create_catalog" {
  description = <<-EOT
    Controls the module's two operating modes:
      * false (default) - REFERENCE an existing catalog via a data source and only
        create schemas beneath it. Use this in environments where you are not
        permitted to create catalogs but can create schemas and everything below.
      * true - CREATE the catalog (databricks_catalog) and manage its lifecycle.
  EOT
  type        = bool
  default     = false
}

variable "schemas" {
  description = "Schemas to create inside the catalog, keyed by schema name. These hold the model services and their inference tables."
  type = map(object({
    comment    = optional(string)
    properties = optional(map(string))
  }))
  default = {
    ai_gateway = {
      comment = "Unity AI Gateway model services and inference logging tables"
    }
  }
}

# ---- create-mode only settings (ignored when create_catalog = false) ----

variable "catalog_comment" {
  description = "Comment applied to the catalog when create_catalog = true."
  type        = string
  default     = "Managed by Terraform (unity-gateway-setup): Unity AI Gateway foundation"
}

variable "catalog_owner" {
  description = "Owner (user or group) for the catalog when create_catalog = true. Null keeps the creating principal."
  type        = string
  default     = null
}

variable "catalog_isolation_mode" {
  description = "Isolation mode for the catalog when create_catalog = true (OPEN or ISOLATED). Null uses the workspace default."
  type        = string
  default     = null

  validation {
    condition     = var.catalog_isolation_mode == null || contains(["OPEN", "ISOLATED"], coalesce(var.catalog_isolation_mode, "OPEN"))
    error_message = "catalog_isolation_mode must be null, \"OPEN\", or \"ISOLATED\"."
  }
}

# ---- shared settings ----

variable "schema_owner" {
  description = "Owner (user or group) applied to every created schema. Null keeps the creating principal."
  type        = string
  default     = null
}

variable "force_destroy" {
  description = "If true, allow destroy of the catalog/schemas even when they still contain objects. Keep false in shared environments."
  type        = bool
  default     = false
}
