#!/bin/sh
# test_02_arg_parsing.sh — bootstrap-state.sh argument handling and the
# zero-API-call guarantee of --dry-run.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
BOOTSTRAP="${BOOTSTRAP_DIR}/bootstrap-state.sh"
T="test_02_arg_parsing"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

# Stub databricks so ANY workspace API call leaves a marker. git stays real:
# the generator sha lookup is local and is not an API call.
_bin="${_work}/bin"
mkdir -p "${_bin}"
printf '#!/bin/sh\ntouch "%s/DATABRICKS_CALLED"\nexit 0\n' "${_work}" > "${_bin}/databricks"
chmod +x "${_bin}/databricks"

# _run <expected-exit> <label> [args...]
_run() {
  _exp="$1"; _label="$2"; shift 2
  _e=0
  PATH="${_bin}:${PATH}" sh "${BOOTSTRAP}" "$@" >/dev/null 2>&1 || _e=$?
  if [ "${_e}" != "${_exp}" ]; then
    printf 'FAIL: %s — %s: expected exit %s, got %s\n' "${T}" "${_label}" "${_exp}" "${_e}"
    exit 1
  fi
}

_run 0 "--help"                  --help
_run 0 "-h"                      -h
_run 2 "unknown flag"            --bogus
_run 2 "--profile with no value" --profile
_run 2 "missing --profile"       --project foo

# --dry-run must make zero API calls.
_run 0 "--dry-run"               --profile fevm-west --dry-run --out "${_work}/out"
if [ -f "${_work}/DATABRICKS_CALLED" ]; then
  printf 'FAIL: %s — --dry-run invoked the databricks CLI\n' "${T}"
  exit 1
fi

printf '  ok: usage exit codes correct; --dry-run made zero API calls\n'
printf 'PASS: %s\n' "${T}"
