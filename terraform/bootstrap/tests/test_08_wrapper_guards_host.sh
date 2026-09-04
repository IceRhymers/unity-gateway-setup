#!/bin/sh
# test_08_wrapper_guards_host.sh — the pooler guard re-runs on every wrapped
# invocation, not only at bootstrap. A .lakebase.env whose PGHOST contains
# "pooler" makes with-state.sh fail before it mints any credential.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
WRAPPER="${BOOTSTRAP_DIR}/with-state.sh"
T="test_08_wrapper_guards_host"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

# Stub databricks: the guard must fire BEFORE any credential mint, so this must
# never be called.
_bin="${_work}/bin"
mkdir -p "${_bin}"
printf '#!/bin/sh\ntouch "%s/DATABRICKS_CALLED"\nexit 0\n' "${_work}" > "${_bin}/databricks"
chmod +x "${_bin}/databricks"

_d="${_work}/tf"
mkdir -p "${_d}"
: > "${_d}/backend.tf"
cat > "${_d}/.lakebase.env" <<'ENVEOF'
PGHOST=instance-abc123-pooler.database.cloud.databricks.com
PGPORT=5432
PGDATABASE=databricks_postgres
PGSSLMODE=require
LAKEBASE_ENDPOINT=projects/x/branches/production/endpoints/primary
DATABRICKS_PROFILE=fevm-west
ENVEOF

_e=0
PATH="${_bin}:${PATH}" sh "${WRAPPER}" true -chdir="${_d}" > "${_work}/out.txt" 2>&1 || _e=$?
if [ "${_e}" -eq 0 ]; then
  printf 'FAIL: %s — wrapper accepted a pooled PGHOST (expected non-zero exit)\n' "${T}"
  cat "${_work}/out.txt"
  exit 1
fi
if ! grep -qi 'pooler' "${_work}/out.txt"; then
  printf 'FAIL: %s — rejection message does not mention the pooled host\n' "${T}"
  cat "${_work}/out.txt"
  exit 1
fi
if [ -f "${_work}/DATABRICKS_CALLED" ]; then
  printf 'FAIL: %s — guard did not fire before the credential mint\n' "${T}"
  exit 1
fi

printf '  ok: wrapper re-runs the pooler guard and fails before minting\n'
printf 'PASS: %s\n' "${T}"
