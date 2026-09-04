#!/bin/sh
# lakebase-env.sh — emit PG* exports for eval.
#
# This script is the ONE deliberate exception to "never print the token".
# It is designed for the rollback alternative path that needs env without
# the with-state.sh wrapper (see bootstrap RUNBOOK §10):
#
#   eval "$(terraform/bootstrap/lakebase-env.sh --print --profile <p>)"
#   # ... run terraform init -migrate-state ...
#   unset PGPASSWORD PGHOST PGPORT PGUSER PGDATABASE PGSSLMODE
#
# --print warns on stderr that it is emitting a live credential.
# Never use --print in contexts where stdout is logged.
#
# Usage:
#   lakebase-env.sh --print --profile <name> [--env-file <path>]
#   lakebase-env.sh -h|--help
#
# Options:
#   --print             Emit PG* export lines for eval (REQUIRED)
#   --profile <name>    Databricks CLI profile (REQUIRED)
#   --env-file <path>   Path to .lakebase.env (default: terraform/infra/.lakebase.env,
#                       resolved relative to the repo root detected from this script's location)
#   -h, --help          Show this message
#
# Exit codes:
#   0  success
#   2  usage error
#   1  runtime error (missing file, mint failure)
set -eu

# ---------------------------------------------------------------------------
# Resolve script and repo roots
# ---------------------------------------------------------------------------
# shellcheck disable=SC2164
_SCRIPT_DIR="$(cd "$(dirname -- "$0")" && pwd)"
_REPO_ROOT="$(cd "${_SCRIPT_DIR}/../.." && pwd)"

# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/lib/lakebase.sh"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DO_PRINT=0
_PROFILE=""
_ENV_FILE="${_REPO_ROOT}/terraform/infra/.lakebase.env"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
_usage() {
  cat <<'EOF'
Usage: lakebase-env.sh --print --profile <name> [--env-file <path>]

Emit PG* export lines for eval. This is the ONE deliberate exception to
"never print the token". Use only for the rollback migration path.

WARNING: --print emits a live credential. Never use it in logged contexts.

Options:
  --print             Emit PG* export lines for eval (required)
  --profile <name>    Databricks CLI profile (required)
  --env-file <path>   Path to .lakebase.env (default: terraform/infra/.lakebase.env)
  -h, --help          Show this message

Example (rollback migration only):
  eval "$(terraform/bootstrap/lakebase-env.sh --print --profile fevm-west)"
  terraform -chdir=terraform/infra init -migrate-state -force-copy
  unset PGPASSWORD PGHOST PGPORT PGUSER PGDATABASE PGSSLMODE
EOF
  exit "${1:-2}"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --print)         _DO_PRINT=1 ;;
    --profile)       shift; _PROFILE="${1:?--profile requires a value}" ;;
    --env-file)      shift; _ENV_FILE="${1:?--env-file requires a value}" ;;
    -h|--help)       _usage 0 ;;
    *)
      printf '[lakebase-env] ERROR: unknown option: %s\n' "$1" >&2
      _usage
      ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
if [ "${_DO_PRINT}" = "0" ]; then
  printf '[lakebase-env] ERROR: --print is required\n' >&2
  _usage
fi

if [ -z "${_PROFILE}" ]; then
  printf '[lakebase-env] ERROR: --profile is required\n' >&2
  _usage
fi

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
lb_require_cmd databricks
lb_require_cmd jq

# ---------------------------------------------------------------------------
# Parse env file (never source it)
# ---------------------------------------------------------------------------
lb_parse_env "${_ENV_FILE}"

# PGHOST must be set after parsing.
if [ -z "${PGHOST:-}" ]; then
  printf '[lakebase-env] FATAL: PGHOST not found in %s\n' "${_ENV_FILE}" >&2
  exit 1
fi

# Guard against pooled host.
lb_assert_direct_host "${PGHOST}"

_endpoint="${LAKEBASE_ENDPOINT:?LAKEBASE_ENDPOINT not set in ${_ENV_FILE}}"

# ---------------------------------------------------------------------------
# Resolve PGUSER from the caller's identity.
# ---------------------------------------------------------------------------
_pguser="$(databricks current-user me -o json --profile "${_PROFILE}" | jq -r '.userName')"
if [ -z "${_pguser}" ]; then
  printf '[lakebase-env] FATAL: could not resolve current user from profile %s\n' "${_PROFILE}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Mint token
# ---------------------------------------------------------------------------
_token="$(lb_mint_token "${_endpoint}" "${_PROFILE}")"
if [ -z "${_token}" ]; then
  printf '[lakebase-env] FATAL: token mint returned empty string\n' >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# WARN on stderr — this script emits a live credential.
# ---------------------------------------------------------------------------
printf '[lakebase-env] WARNING: emitting a live credential to stdout for eval.\n' >&2
printf '[lakebase-env] Unset PG vars immediately after use:\n' >&2
printf '[lakebase-env]   unset PGPASSWORD PGHOST PGPORT PGUSER PGDATABASE PGSSLMODE\n' >&2

# ---------------------------------------------------------------------------
# Emit export lines for eval
# ---------------------------------------------------------------------------
printf 'export PGHOST=%s\n'     "${PGHOST}"
printf 'export PGPORT=%s\n'     "${PGPORT:-5432}"
printf 'export PGDATABASE=%s\n' "${PGDATABASE}"
printf 'export PGUSER=%s\n'     "${_pguser}"
printf 'export PGSSLMODE=%s\n'  "${PGSSLMODE:-require}"
printf 'export PGPASSWORD=%s\n' "${_token}"

exit 0
