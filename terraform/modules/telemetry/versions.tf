terraform {
  required_version = ">= 1.5.0"

  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.129.0, < 2.0.0"
    }
    # Drives table DDL through the Statement Execution API (see main.tf).
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2.0"
    }
  }
}
