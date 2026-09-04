#!/bin/sh
# bootstrap-state.sh — idempotent Lakebase remote-state bootstrap.
#
# Creates (or reuses) a Lakebase project, a group Postgres role, the state
# objects, and writes terraform/infra/.lakebase.env. Run once per operator
# checkout; re-run at any time — it is safe to re-run.
#
# Usage:
#   bootstrap-state.sh --profile <name> [OPTIONS]
#
# Required:
#   --profile <name>        Databricks CLI profile
#
# Options:
#   --project <id>          Lakebase project ID (default: unity-gateway-tfstate)
#   --tf-dir <path>         terraform/infra directory (default: auto-detected)
#   --out <dir>             Output directory for .lakebase.env (default: --tf-dir)
#   --dry-run               Make ZERO API calls; render .lakebase.env with placeholders
#   --env-only              Resolve project/endpoint/database, write .lakebase.env, no DDL
#   --force                 Overwrite existing .lakebase.env without prompting
#   --yes                   Skip confirmation prompts
#   --grant-to <principal>  Onboard an operator: create their role and GRANT group membership
#   -h, --help              Show this message
#
# Exit codes:
#   0  success
#   1  runtime error
#   2  usage error
#   3  target .lakebase.env exists and differs (re-run with --force to overwrite)
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
_PROFILE=""
_PROJECT="unity-gateway-tfstate"
# The state schema and the group role are FIXED, not flags. backend.tf statically
# pins schema_name = "tfstate_infra" (plan §3.f), so a different schema would
# break the backend; the group role name is likewise baked into the DDL and the
# ownership model. These constants are the single source of truth for both names.
_GROUP="terraform_writers"
_SCHEMA="tfstate_infra"
_TF_DIR="${_REPO_ROOT}/terraform/infra"
_OUT=""
_DRY_RUN=0
_ENV_ONLY=0
_FORCE=0
_YES=0
_GRANT_TO=""

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
_usage() {
  cat <<'EOF'
Usage: bootstrap-state.sh --profile <name> [OPTIONS]

Idempotent Lakebase remote-state bootstrap.

Creates (or reuses) a Lakebase project, a group Postgres role, the state
objects, and writes terraform/infra/.lakebase.env. Safe to re-run.

Required:
  --profile <name>        Databricks CLI profile

Options:
  --project <id>          Lakebase project ID (default: unity-gateway-tfstate)
  --tf-dir <path>         terraform/infra directory (default: auto-detected)
  --out <dir>             Output directory for .lakebase.env (default: --tf-dir)
  --dry-run               Make ZERO API calls; render .lakebase.env with placeholders
  --env-only              Resolve project/endpoint, write .lakebase.env, run no DDL
  --force                 Overwrite existing .lakebase.env without prompting
  --yes                   Skip confirmation prompts
  --grant-to <principal>  Onboard an operator: create role and grant group membership
  -h, --help              Show this message

Examples:
  # First-time bootstrap:
  bootstrap-state.sh --profile fevm-west

  # Second operator (env only, no DDL):
  bootstrap-state.sh --profile fevm-west --env-only

  # Onboard a new operator:
  bootstrap-state.sh --profile fevm-west --grant-to user@example.com

  # Dry run (no API calls):
  bootstrap-state.sh --profile fevm-west --dry-run --out /tmp/lb

Exit codes:
  0  success
  1  runtime error
  2  usage error
  3  target .lakebase.env exists and differs (re-run with --force to overwrite)
EOF
  exit "${1:-2}"
}

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
_info()  { printf '[bootstrap] %s\n' "$*"; }
_fatal() { printf '[bootstrap] FATAL: %s\n' "$*" >&2; exit 1; }

# _need_val <remaining-arg-count> <flag-name>
# A value-taking flag with no value is a usage error. Exit 2 via _usage.
# The ${1:?} idiom is not portable here: it exits 1 under bash and 2 under dash,
# which contradicts the "2 usage error" contract documented above.
_need_val() {
  if [ "$1" -lt 2 ]; then
    printf '[bootstrap] ERROR: %s requires a value\n' "$2" >&2
    _usage
  fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)    _need_val "$#" --profile;  shift; _PROFILE="$1" ;;
    --project)    _need_val "$#" --project;  shift; _PROJECT="$1" ;;
    --tf-dir)     _need_val "$#" --tf-dir;   shift; _TF_DIR="$1" ;;
    --out)        _need_val "$#" --out;      shift; _OUT="$1" ;;
    --dry-run)    _DRY_RUN=1 ;;
    --env-only)   _ENV_ONLY=1 ;;
    --force)      _FORCE=1 ;;
    --yes)        _YES=1 ;;
    --grant-to)   _need_val "$#" --grant-to; shift; _GRANT_TO="$1" ;;
    -h|--help)    _usage 0 ;;
    *)
      printf '[bootstrap] ERROR: unknown option: %s\n' "$1" >&2
      _usage
      ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Validate required flags
# ---------------------------------------------------------------------------
if [ -z "${_PROFILE}" ]; then
  printf '[bootstrap] ERROR: --profile is required\n' >&2
  _usage
fi

# Default output directory.
if [ -z "${_OUT}" ]; then
  _OUT="${_TF_DIR}"
fi

_ENV_FILE="${_OUT}/.lakebase.env"

# Derived path constants.
_BRANCH="production"
_ENDPOINT_NAME="primary"
_DATABASE="databricks_postgres"
_PROJECT_PATH="projects/${_PROJECT}"
_BRANCH_PATH="${_PROJECT_PATH}/branches/${_BRANCH}"
_ENDPOINT_PATH="${_BRANCH_PATH}/endpoints/${_ENDPOINT_NAME}"
_DATABASE_PATH="${_BRANCH_PATH}/databases/${_DATABASE}"

# ---------------------------------------------------------------------------
# --dry-run: zero API calls, render placeholders, done.
# ---------------------------------------------------------------------------
if [ "${_DRY_RUN}" = "1" ]; then
  _info "DRY-RUN mode: no API calls will be made."
  _info "Rendering .lakebase.env with documented placeholders to: ${_ENV_FILE}"
  mkdir -p "${_OUT}"
  # Compute sha of this script for the generator line.
  # git log on an untracked file exits 0 but prints nothing, so the || branch
  # never fires and the sha comes out empty. Guard the empty result explicitly.
  _sha="<git-sha-of-bootstrap-state.sh>"
  if command -v git >/dev/null 2>&1; then
    _sha="$(git -C "${_REPO_ROOT}" log -1 --format='%H' -- "${_SCRIPT_DIR}/bootstrap-state.sh" 2>/dev/null || true)"
    [ -n "${_sha}" ] || _sha="<git-sha-of-bootstrap-state.sh>"
  fi
  LB_HOST="<status.hosts.host — direct, never *pooler*>"
  LB_PORT="<status.hosts.port>"
  LB_DATABASE="${_DATABASE}"
  LB_PROJECT="${_PROJECT}"
  LB_BRANCH="${_BRANCH}"
  LB_ENDPOINT="${_ENDPOINT_PATH}"
  LB_SCHEMA="${_SCHEMA}"
  LB_PROFILE="${_PROFILE}"
  LB_GEN_SHA="${_sha}"
  export LB_HOST LB_PORT LB_DATABASE LB_PROJECT LB_BRANCH \
         LB_ENDPOINT LB_SCHEMA LB_PROFILE LB_GEN_SHA
  # Idempotency guard (plan §7, test_05): render to a temp file first, then
  # compare. An existing byte-identical file is left in place. An existing file
  # that differs is a refusal (exit 3) unless --force overwrites it.
  _tmp="${_ENV_FILE}.tmp.$$"
  lb_render_env > "${_tmp}"
  if [ -f "${_ENV_FILE}" ] && [ "${_FORCE}" = "0" ]; then
    if cmp -s "${_tmp}" "${_ENV_FILE}"; then
      rm -f "${_tmp}"
      _info "Existing .lakebase.env is byte-identical; left in place."
    else
      rm -f "${_tmp}"
      printf '[bootstrap] FATAL: %s exists and differs from the rendered content.\n' "${_ENV_FILE}" >&2
      printf '[bootstrap] Pass --force to overwrite it.\n' >&2
      exit 3
    fi
  else
    mv "${_tmp}" "${_ENV_FILE}"
    _info "Wrote: ${_ENV_FILE}"
  fi
  _info "DRY-RUN complete. No workspace changes were made."
  exit 0
fi

# ---------------------------------------------------------------------------
# Preflight: hard dependencies
# ---------------------------------------------------------------------------
lb_require_cmd databricks
lb_require_cmd jq
lb_require_cmd psql

# ---------------------------------------------------------------------------
# Resolve operator identity and assert group membership (full mode only).
# --env-only and --grant-to skip the membership check.
# ---------------------------------------------------------------------------
_ME="$(databricks current-user me -o json --profile "${_PROFILE}" | jq -r '.userName')"
if [ -z "${_ME}" ]; then
  _fatal "Could not resolve current user from profile ${_PROFILE}."
fi
_info "Operator: ${_ME}"

if [ "${_ENV_ONLY}" = "0" ] && [ -z "${_GRANT_TO}" ]; then
  # Verify the operator is a member of the group.
  # Hard-fail with the instruction if not — ALTER ... OWNER TO requires membership.
  _info "Checking group membership in ${_GROUP}..."
  _member_check="$(databricks groups get-member "${_GROUP}" \
    --profile "${_PROFILE}" -o json 2>/dev/null \
    | jq -r --arg u "${_ME}" '
        .members // [] | map(select(.display == $u or .value == $u)) | length
      ' 2>/dev/null || printf '0')"
  if [ "${_member_check}" = "0" ]; then
    _fatal "Operator '${_ME}' is not a member of group '${_GROUP}'.
  Both ALTER ... OWNER TO and SET ROLE require membership.
  Ask a workspace admin to add ${_ME} to the '${_GROUP}' workspace group, then re-run."
  fi
  _info "Group membership confirmed."
fi

# ---------------------------------------------------------------------------
# --grant-to: onboard a new operator.
# ---------------------------------------------------------------------------
if [ -n "${_GRANT_TO}" ]; then
  _info "Onboarding operator: ${_GRANT_TO}"

  # Check if a role already exists for this identity.
  _existing_role="$(databricks postgres list-roles "${_BRANCH_PATH}" \
    -o json --profile "${_PROFILE}" \
    | jq -r --arg id "${_GRANT_TO}" \
        '.roles // [] | map(select(.spec.postgresRole == $id)) | length' \
    2>/dev/null || printf '0')"

  if [ "${_existing_role}" = "0" ]; then
    _info "Creating USER role for ${_GRANT_TO}..."
    databricks postgres create-role "${_BRANCH_PATH}" \
      --role-id "${_GRANT_TO}" -o json --profile "${_PROFILE}" \
      --json "{\"spec\":{\"identity_type\":\"USER\",\"postgres_role\":\"${_GRANT_TO}\",\"auth_method\":\"LAKEBASE_OAUTH_V1\"}}" \
      > /dev/null
    _info "Role created."
  else
    _info "Role already exists for ${_GRANT_TO}; skipping create-role."
  fi

  # Mint a token to issue the GRANT.
  _info "Minting token to issue GRANT..."
  _grant_token="$(lb_mint_token "${_ENDPOINT_PATH}" "${_PROFILE}")"
  if [ -z "${_grant_token}" ]; then
    _fatal "Token mint returned empty string."
  fi

  PGPASSWORD="${_grant_token}" \
  PGHOST="$(databricks postgres get-endpoint "${_ENDPOINT_PATH}" -o json --profile "${_PROFILE}" | jq -r '.status.hosts.host')" \
  PGPORT="$(databricks postgres get-endpoint "${_ENDPOINT_PATH}" -o json --profile "${_PROFILE}" | jq -r '.status.hosts.port // "5432"')" \
  PGDATABASE="${_DATABASE}" PGUSER="${_ME}" PGSSLMODE="require" \
    psql -v ON_ERROR_STOP=1 -c "GRANT \"${_GROUP}\" TO \"${_GRANT_TO}\";"

  _info "GRANT issued. ${_GRANT_TO} is now a member of ${_GROUP}."
  exit 0
fi

# ---------------------------------------------------------------------------
# Resolve or create the project.
# ---------------------------------------------------------------------------
_info "Checking for Lakebase project: ${_PROJECT}..."
_project_exists="$(databricks postgres list-projects -o json --profile "${_PROFILE}" \
  | jq -r --arg p "${_PROJECT}" \
      '.projects // [] | map(select(.spec.projectId == $p or .name == $p)) | length' \
  2>/dev/null || printf '0')"

if [ "${_project_exists}" = "0" ]; then
  _info "Project not found. Creating: ${_PROJECT}..."
  databricks postgres create-project "${_PROJECT}" -o json --profile "${_PROFILE}" \
    --json '{"spec":{"pg_version":17,"display_name":"unity-gateway terraform state"}}' \
    > /dev/null
  _info "Project created."
else
  _info "Project already exists: ${_PROJECT}"
fi

# ---------------------------------------------------------------------------
# Resolve endpoint — guard against pooled host.
# ---------------------------------------------------------------------------
_info "Resolving endpoint: ${_ENDPOINT_PATH}..."
_endpoint_json="$(databricks postgres get-endpoint "${_ENDPOINT_PATH}" -o json --profile "${_PROFILE}")"
_host="$(printf '%s' "${_endpoint_json}" | lb_pick_host)"
_port="$(printf '%s' "${_endpoint_json}" | jq -r '.status.hosts.port // "5432"')"
_info "Host: ${_host}  Port: ${_port}"

# ---------------------------------------------------------------------------
# Resolve database name.
# ---------------------------------------------------------------------------
_info "Resolving database: ${_DATABASE_PATH}..."
_db="$(databricks postgres get-database "${_DATABASE_PATH}" -o json --profile "${_PROFILE}" \
  | jq -r '.spec.databaseName // "databricks_postgres"')"
_info "Database: ${_db}"

# ---------------------------------------------------------------------------
# Compute generator sha.
# ---------------------------------------------------------------------------
_sha="<unknown>"
if command -v git >/dev/null 2>&1; then
  _sha="$(git -C "${_REPO_ROOT}" log -1 --format='%H' -- "${_SCRIPT_DIR}/bootstrap-state.sh" 2>/dev/null || true)"
  [ -n "${_sha}" ] || _sha="<unknown>"
fi

# ---------------------------------------------------------------------------
# Write .lakebase.env (full mode and --env-only).
# ---------------------------------------------------------------------------
_write_env() {
  mkdir -p "${_OUT}"
  if [ -f "${_ENV_FILE}" ] && [ "${_FORCE}" = "0" ] && [ "${_YES}" = "0" ]; then
    printf '[bootstrap] .lakebase.env already exists at %s\n' "${_ENV_FILE}"
    printf '[bootstrap] Pass --force to overwrite, or --yes to accept silently.\n'
    printf '[bootstrap] Overwrite? [y/N] '
    # shellcheck disable=SC2162  # read without -r is intentional; no backslash expected here
    read _answer
    case "${_answer}" in
      y|Y|yes|YES) ;;
      *) _info "Skipped writing .lakebase.env."; return 0 ;;
    esac
  fi
  LB_HOST="${_host}"
  LB_PORT="${_port}"
  LB_DATABASE="${_db}"
  LB_PROJECT="${_PROJECT}"
  LB_BRANCH="${_BRANCH}"
  LB_ENDPOINT="${_ENDPOINT_PATH}"
  LB_SCHEMA="${_SCHEMA}"
  LB_PROFILE="${_PROFILE}"
  LB_GEN_SHA="${_sha}"
  export LB_HOST LB_PORT LB_DATABASE LB_PROJECT LB_BRANCH \
         LB_ENDPOINT LB_SCHEMA LB_PROFILE LB_GEN_SHA
  lb_render_env > "${_ENV_FILE}"
  _info "Wrote: ${_ENV_FILE}"
}

# ---------------------------------------------------------------------------
# --env-only: write env and exit. No DDL.
# ---------------------------------------------------------------------------
if [ "${_ENV_ONLY}" = "1" ]; then
  _write_env
  _info "--env-only complete. No DDL was run."
  exit 0
fi

# ---------------------------------------------------------------------------
# Full mode: create group role (if absent), run DDL, write env.
# ---------------------------------------------------------------------------

# Check for existing group role.
_info "Checking for group role: ${_GROUP}..."
_role_exists="$(databricks postgres list-roles "${_BRANCH_PATH}" \
  -o json --profile "${_PROFILE}" \
  | jq -r --arg r "${_GROUP}" \
      '.roles // [] | map(select(.spec.postgresRole == $r)) | length' \
  2>/dev/null || printf '0')"

if [ "${_role_exists}" = "0" ]; then
  _info "Creating group role: ${_GROUP}..."
  databricks postgres create-role "${_BRANCH_PATH}" \
    --role-id "${_GROUP}" -o json --profile "${_PROFILE}" \
    --json "{\"spec\":{\"identity_type\":\"GROUP\",\"postgres_role\":\"${_GROUP}\",\"auth_method\":\"LAKEBASE_OAUTH_V1\"}}" \
    > /dev/null
  _info "Group role created."
else
  _info "Group role already exists: ${_GROUP}"
fi

# Mint token for DDL.
_info "Minting token for DDL (TTL: 3600s)..."
_token="$(lb_mint_token "${_ENDPOINT_PATH}" "${_PROFILE}")"
if [ -z "${_token}" ]; then
  _fatal "Token mint returned empty string."
fi

_info "Running DDL..."
PGPASSWORD="${_token}" \
PGHOST="${_host}" \
PGPORT="${_port}" \
PGDATABASE="${_db}" \
PGUSER="${_ME}" \
PGSSLMODE="require" \
  psql -v ON_ERROR_STOP=1 \
       -f "${_SCRIPT_DIR}/sql/create-state-objects.sql"

_info "DDL complete."

_write_env

_info ""
_info "Bootstrap complete."
_info "  Profile  : ${_PROFILE}"
_info "  Project  : ${_PROJECT}"
_info "  Endpoint : ${_ENDPOINT_PATH}"
_info "  Host     : ${_host}:${_port}"
_info "  Database : ${_db}"
_info "  Schema   : ${_SCHEMA}"
_info "  Group    : ${_GROUP}"
_info "  Env file : ${_ENV_FILE}"
_info ""
_info "Next step: make tf-init PROFILE=${_PROFILE}"

exit 0
