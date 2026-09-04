#!/bin/sh
# test_01_lib_sourcing.sh — sourcing lib/lakebase.sh has no side effects and
# defines the six documented lb_ functions.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
LIB="${BOOTSTRAP_DIR}/lib/lakebase.sh"
T="test_01_lib_sourcing"

if [ ! -f "${LIB}" ]; then
  printf 'FAIL: %s — library not found: %s\n' "${T}" "${LIB}"
  exit 1
fi

# No side effects: sourcing must print nothing on stdout or stderr.
# The trailing marker proves the subshell reached the end.
# shellcheck disable=SC1090
_out=$(. "${LIB}" 2>&1; printf 'END')
if [ "${_out}" != "END" ]; then
  printf 'FAIL: %s — sourcing the library produced output: %s\n' "${T}" "${_out}"
  exit 1
fi

# Every documented function must be defined after sourcing.
# shellcheck disable=SC1090
. "${LIB}"
for _fn in lb_require_cmd lb_pick_host lb_assert_direct_host \
           lb_parse_env lb_render_env lb_mint_token \
           lb_project_count lb_role_count lb_db_name \
           lb_group_id lb_group_has_member; do
  if ! command -v "${_fn}" >/dev/null 2>&1; then
    printf 'FAIL: %s — function not defined after sourcing: %s\n' "${T}" "${_fn}"
    exit 1
  fi
done

printf '  ok: library sources cleanly and defines all lb_ functions\n'
printf 'PASS: %s\n' "${T}"
