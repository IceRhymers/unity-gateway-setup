#!/bin/sh
# test_11_env_not_sourced.sh — lb_parse_env parses .lakebase.env; it never
# sources it. A command substitution in a value is not executed, and a
# non-whitelisted key such as PG_CONN_STR is rejected.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
LIB="${BOOTSTRAP_DIR}/lib/lakebase.sh"
T="test_11_env_not_sourced"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

# shellcheck disable=SC1090
. "${LIB}"

# Case 1: a command substitution in a value must NOT execute.
_marker="${_work}/pwned"
_f1="${_work}/env1"
# The literal $(...) is intentional: it must be written verbatim, not expanded.
# shellcheck disable=SC2016
printf 'PGHOST=$(touch %s)\n' "${_marker}" > "${_f1}"

# Run in a subshell so the exported PGHOST does not leak into later cases.
( lb_parse_env "${_f1}" ) >/dev/null 2>&1 || true
if [ -f "${_marker}" ]; then
  printf 'FAIL: %s — command substitution executed; the file was sourced, not parsed\n' "${T}"
  exit 1
fi

# The value must be stored literally, proving no evaluation happened.
_val=$(lb_parse_env "${_f1}" >/dev/null 2>&1; printf '%s' "${PGHOST:-}")
# shellcheck disable=SC2016
case "${_val}" in
  *'$(touch'*) ;;
  *)
    printf 'FAIL: %s — PGHOST was not stored literally: %s\n' "${T}" "${_val}"
    exit 1 ;;
esac

# Case 2: a non-whitelisted key (PG_CONN_STR) is rejected.
_f2="${_work}/env2"
printf 'PG_CONN_STR=host=evil\n' > "${_f2}"
_e=0
( lb_parse_env "${_f2}" ) >/dev/null 2>&1 || _e=$?
if [ "${_e}" -eq 0 ]; then
  printf 'FAIL: %s — PG_CONN_STR was accepted by the whitelist\n' "${T}"
  exit 1
fi

printf '  ok: values are parsed literally and the key whitelist is enforced\n'
printf 'PASS: %s\n' "${T}"
