# terraform/bootstrap

This directory provides three scripts and a SQL file that provision the Lakebase Postgres
backend for `terraform/infra`.

## What It Does

`bootstrap-state.sh` runs once per workspace. It performs these steps:

1. Creates (or reuses) the Lakebase project `unity-gateway-tfstate`.
2. Creates the workspace-level Databricks group `terraform_writers` if it is absent, and
   adds you to it. This step needs workspace-admin rights.
3. Creates the Lakebase group role `terraform_writers`, backed by that workspace group.
4. Resolves the direct endpoint host and runs the pooler guard.
5. Grants the group role `CREATE` on the database, connected as the database owner.
6. Creates the state schema, sequence, table, and index in `tfstate_infra`, connected
   as the group role, so the group role owns them.
7. Writes `terraform/infra/.lakebase.env` with the host, port, database, role, and
   connection parameters.

You do not inherit a Lakebase group role. You authenticate as it. A member of the
workspace group `terraform_writers` connects to Postgres as the role `terraform_writers`.
Every operator uses the same login role, so they share the state objects the role owns.
The Databricks token-mint audit log records which person minted each credential.

The Databricks resource id and the Postgres name of an object differ. The resource id
allows only lowercase letters, digits, and hyphens. The Postgres name allows underscores.
The bootstrap uses the group role id `terraform-writers` and the Postgres role name
`terraform_writers`. It uses the database resource id `databricks-postgres` and the
Postgres database name `databricks_postgres`.

`with-state.sh` wraps every state-touching Makefile target. Before each Terraform call
it sanitizes the Postgres environment, sets `PGUSER` to the group role, mints a
short-lived OAuth token, and exports it as `PGPASSWORD`.

`lakebase-env.sh --print` emits the full credential environment. Use it only for the
alternative rollback path described in `RUNBOOK.md`.

## Hard Dependencies

The scripts require three tools. The bootstrap hard-fails if any is missing:

- `databricks` — the Databricks CLI, authenticated with a profile that accesses the
  target workspace.
- `jq` — JSON parsing. The pooler guard uses it and cannot be skipped.
- `psql` — DDL execution. **The local client is v14 and the server is Postgres 17.** Plain
  SQL and DML work correctly. Avoid `psql` meta-commands such as `\dn+` or `\dt+`.
  Their output format differs across major versions.

## Flags

### `bootstrap-state.sh`

| Flag | Description |
|---|---|
| `--profile <name>` | Databricks CLI profile to use. Required. |
| `--project <id>` | Lakebase project ID. Default: `unity-gateway-tfstate`. |
| `--tf-dir <path>` | Directory where `.lakebase.env` is written. Default: `terraform/infra`. |
| `--env-only` | Resolve the project and endpoint, run the pooler guard, and write `.lakebase.env`. Runs no DDL. Requires no group membership. Use this to wire an existing installation to a new checkout. |
| `--grant-to <principal>` | Add the given principal to the workspace group `terraform_writers`. The principal then operates the backend by assuming the group role. Needs workspace-admin rights. |
| `--dry-run` | Make no API calls. Render `.lakebase.env` with documented placeholder values to `--out`. |
| `--out <dir>` | Override the output directory for `--dry-run`. |
| `--force` | Overwrite an existing `.lakebase.env` without prompting. |
| `--yes` | Skip confirmation prompts. |
| `--help` | Print usage and exit 0. |

## Pooler Host Rule

Use only `status.hosts.host` from the endpoint JSON — never `read_write_pooled_host`,
`read_only_host`, or `read_only_pooled_host` — because PgBouncer breaks advisory locking
without error.

The guard runs inside `bootstrap-state.sh` and again inside `with-state.sh` on every
invocation, against the final `PGHOST` value after environment sanitization.

## The `--tf-output-json` Escape Hatch

The Makefile routes state-reading targets through `with-state.sh`. The wrapper hard-fails
when `terraform/infra/backend.tf` is present but `.lakebase.env` is absent. If you do not
have Lakebase access, the error message names this flag.

Pass `--tf-output-json <path>` to `generate.py` to supply pre-rendered Terraform output
instead of connecting to the backend. The flag exists at `generate.py:250`.

If you run `python3 agent_setups/scripts/generate.py` directly (rather than through
`make`), the script calls `terraform output` as a child process. That call fails unless
the `PG*` variables from `.lakebase.env` are already in the environment. Either source
`.lakebase.env` first or use `--tf-output-json`.

## Credential Inheritance

`with-state.sh` exports `PGPASSWORD` into the process environment before it calls
Terraform. Any `local-exec` provisioner added to `terraform/infra` will inherit this
credential. Add `environment = { PGPASSWORD = "", PGPASSFILE = "" }` to every new
`local-exec` block unless the block requires database access.
