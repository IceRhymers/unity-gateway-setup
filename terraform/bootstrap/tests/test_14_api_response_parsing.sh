#!/bin/sh
# test_14_api_response_parsing.sh — the regression guard for the class of bug
# that shipped in the first cut: the response-parsing helpers are asserted
# against fixtures recorded from the REAL Databricks API, not the shapes the
# script once assumed.
#
# The real API returns BARE JSON arrays (no wrapping object, no `.spec`),
# snake_case fields, and a hyphen-only resource id (.role_id / .project_id)
# separate from the Postgres name (.status.postgres_role / .status.postgres_database).
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
LIB="${BOOTSTRAP_DIR}/lib/lakebase.sh"
FIX="${TESTS_DIR}/fixtures"
T="test_14_api_response_parsing"

if ! command -v jq >/dev/null 2>&1; then
  printf 'FAIL: %s — jq is required and is not installed\n' "${T}"
  exit 1
fi

# shellcheck disable=SC1090
. "${LIB}"

_fail() { printf 'FAIL: %s — %s\n' "${T}" "$1"; exit 1; }

# _eq <expected> <actual> <label>
_eq() {
  if [ "$1" != "$2" ]; then
    _fail "$3: expected [$1], got [$2]"
  fi
}

# --- lb_project_count (bare array, top-level .project_id) ---
_eq 1 "$(lb_project_count unity-gateway-tfstate < "${FIX}/list-projects.json")" "project present"
_eq 0 "$(lb_project_count no-such-project    < "${FIX}/list-projects.json")" "project absent"

# --- lb_role_count (matches EITHER .status.postgres_role OR .role_id) ---
_eq 1 "$(lb_role_count terraform_writers terraform-writers        < "${FIX}/list-roles.json")" "group role by postgres_role"
_eq 1 "$(lb_role_count tanner.wendland@databricks.com tanner-wendland < "${FIX}/list-roles.json")" "user role by principal"
_eq 0 "$(lb_role_count absent absent-id                           < "${FIX}/list-roles.json")" "role absent"

# --- lb_db_name (.status.postgres_database, underscore form) ---
_eq databricks_postgres "$(lb_db_name < "${FIX}/get-database.json")" "database name"
_eq "" "$(printf '{}' | lb_db_name)" "database name empty when absent"

# --- lb_group_id (bare array of {id, displayName}) ---
_eq 99999999999999 "$(lb_group_id terraform_writers < "${FIX}/groups-list.json")" "group id present"
_eq "" "$(lb_group_id no-such-group < "${FIX}/groups-list.json")" "group id absent"

# --- lb_group_has_member (.members[].value) ---
_eq 1 "$(lb_group_has_member 2352405730157715 < "${FIX}/group-get.json")" "member present"
_eq 0 "$(lb_group_has_member 000000000000000 < "${FIX}/group-get.json")" "member absent"
_eq 0 "$(printf '{}' | lb_group_has_member 123)" "no members key"

printf '  ok: all response-parsing helpers agree with the recorded API fixtures\n'
printf 'PASS: %s\n' "${T}"
