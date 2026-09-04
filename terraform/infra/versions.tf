terraform {
  required_version = ">= 1.5.0"

  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.129.0, < 2.0.0"
    }
  }

  # State backend.
  #
  # This file declares no backend. The backend lives in its own committed file,
  # backend.tf, which stores state in Databricks Lakebase Postgres.
  #
  # Run this once to create your local connection details:
  #   make tf-bootstrap-state ARGS=--env-only PROFILE=<your-profile>
  #
  # `terraform fmt` and `terraform validate` work with no credentials.
  # See terraform/bootstrap/README.md and terraform/bootstrap/RUNBOOK.md.
}
