#!/bin/sh
# test_04_env_render.sh — --dry-run renders a .lakebase.env that carries the
# expected keys, no PGUSER line, and no secret material.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
BOOTSTRAP="${BOOTSTRAP_DIR}/bootstrap-state.sh"
T="test_04_env_render"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

# Stub databricks: --dry-run must never call it.
_bin="${_work}/bin"
mkdir -p "${_bin}"
printf '#!/bin/sh\ntouch "%s/DATABRICKS_CALLED"\nexit 0\n' "${_work}" > "${_bin}/databricks"
chmod +x "${_bin}/databricks"

_out="${_work}/out"
_e=0
PATH="${_bin}:${PATH}" sh "${BOOTSTRAP}" \
  --profile fevm-west --dry-run --out "${_out}" >/dev/null 2>&1 || _e=$?
if [ "${_e}" != "0" ]; then
  printf 'FAIL: %s — --dry-run exited %s\n' "${T}" "${_e}"
  exit 1
fi

_env="${_out}/.lakebase.env"
if [ ! -f "${_env}" ]; then
  printf 'FAIL: %s — .lakebase.env was not written\n' "${T}"
  exit 1
fi

# Required content.
for _needle in 'LAKEBASE_SCHEMA' 'LAKEBASE_PROJECT' 'PGSSLMODE=require'; do
  if ! grep -q "${_needle}" "${_env}"; then
    printf 'FAIL: %s — .lakebase.env is missing %s\n' "${T}" "${_needle}"
    exit 1
  fi
done

# No PGUSER assignment line (a comment mentioning PGUSER is fine).
if grep -q '^PGUSER' "${_env}"; then
  printf 'FAIL: %s — .lakebase.env contains a PGUSER assignment\n' "${T}"
  exit 1
fi

# Negative: no secret material of any kind. -i makes "password" cover PGPASSWORD.
if grep -qiE 'password|token|conn_str' "${_env}"; then
  printf 'FAIL: %s — .lakebase.env contains secret-looking content:\n' "${T}"
  grep -niE 'password|token|conn_str' "${_env}"
  exit 1
fi

printf '  ok: env has required keys, no PGUSER line, no secret material\n'
printf 'PASS: %s\n' "${T}"
