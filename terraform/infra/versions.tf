terraform {
  required_version = ">= 1.5.0"

  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.129.0, < 2.0.0"
    }
  }

  # Local state by default. For team/enterprise use, switch to a remote backend
  # (S3 + DynamoDB lock, Terraform Cloud, etc.) so state is shared and locked.
  #
  # backend "s3" {
  #   bucket         = "my-tfstate-bucket"
  #   key            = "unity-gateway/fevm-west.tfstate"
  #   region         = "us-west-2"
  #   dynamodb_table = "my-tfstate-locks"
  #   encrypt        = true
  # }
}
