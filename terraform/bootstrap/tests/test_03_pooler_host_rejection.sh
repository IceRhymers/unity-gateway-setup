#!/bin/sh
# test_03_pooler_host_rejection.sh — the critical guard. lb_pick_host must
# accept a direct host and reject any pooled or missing host. PgBouncer
# transaction mode has no advisory locks, so a pooled host would break state
# locking SILENTLY.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
LIB="${BOOTSTRAP_DIR}/lib/lakebase.sh"
FIXTURES="${TESTS_DIR}/fixtures"
T="test_03_pooler_host_rejection"

# jq is a hard dependency of lb_pick_host. Do not self-skip: hard-fail instead.
if ! command -v jq >/dev/null 2>&1; then
  printf 'FAIL: %s — jq is required and is not installed\n' "${T}"
  exit 1
fi

# shellcheck disable=SC1090
. "${LIB}"

# Direct host: exit 0 and echo the host verbatim.
_out=$(lb_pick_host < "${FIXTURES}/endpoint_direct.json") || {
  printf 'FAIL: %s — direct host was rejected (expected exit 0)\n' "${T}"
  exit 1
}
case "${_out}" in
  instance-abc123.database.cloud.databricks.com) ;;
  *)
    printf 'FAIL: %s — direct host not echoed, got: %s\n' "${T}" "${_out}"
    exit 1 ;;
esac

# Pooled host, missing host, and pooled-only JSON must each exit non-zero.
for _f in endpoint_pooler endpoint_no_host endpoint_pooled_only; do
  _e=0
  lb_pick_host < "${FIXTURES}/${_f}.json" >/dev/null 2>&1 || _e=$?
  if [ "${_e}" -eq 0 ]; then
    printf 'FAIL: %s — %s.json was accepted (expected rejection)\n' "${T}" "${_f}"
    exit 1
  fi
done

printf '  ok: direct host accepted; pooled and missing hosts rejected\n'
printf 'PASS: %s\n' "${T}"
