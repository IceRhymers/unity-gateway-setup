# backend.tf — Lakebase Postgres remote backend for terraform/infra.
#
# This file is intentionally committed and static. Committing it makes the
# remote-state configuration worktree-durable: every git worktree and every
# fresh clone contains it automatically, which prevents the silent "1 to add"
# failure that occurs when backend.tf is missing but .terraform/ still records
# a pg backend.
#
# Credentials arrive exclusively as PG* environment variables at run time.
# The wrapper terraform/bootstrap/with-state.sh injects them per-invocation;
# nothing here carries or references a secret.
#
# All three skip_* flags are set so terraform init executes no DDL. The state
# schema, sequence, table, and index are pre-created by bootstrap-state.sh and
# owned by the terraform_writers group (plan §3.d and §3.f).

terraform {
  backend "pg" {
    schema_name          = "tfstate_infra"
    skip_schema_creation = true
    skip_table_creation  = true
    skip_index_creation  = true
  }
}
