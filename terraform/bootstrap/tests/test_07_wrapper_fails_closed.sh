#!/bin/sh
# test_07_wrapper_fails_closed.sh — with-state.sh fails closed when backend.tf
# is present but .lakebase.env is missing, and passes through cleanly when
# neither a backend nor a pg state record is present.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
WRAPPER="${BOOTSTRAP_DIR}/with-state.sh"
T="test_07_wrapper_fails_closed"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

# Stub databricks: passthrough must not call it.
_bin="${_work}/bin"
mkdir -p "${_bin}"
printf '#!/bin/sh\ntouch "%s/DATABRICKS_CALLED"\nexit 0\n' "${_work}" > "${_bin}/databricks"
chmod +x "${_bin}/databricks"

# --- Case 1: backend.tf present, .lakebase.env missing -> hard fail. ---
_d1="${_work}/case1"
mkdir -p "${_d1}"
: > "${_d1}/backend.tf"
_e=0
PATH="${_bin}:${PATH}" sh "${WRAPPER}" true -chdir="${_d1}" > "${_work}/o1.txt" 2>&1 || _e=$?
if [ "${_e}" -eq 0 ]; then
  printf 'FAIL: %s — case1 expected a hard fail, got exit 0\n' "${T}"
  exit 1
fi
if ! grep -q 'tf-bootstrap-state' "${_work}/o1.txt"; then
  printf 'FAIL: %s — case1 message does not name tf-bootstrap-state\n' "${T}"
  cat "${_work}/o1.txt"
  exit 1
fi
if ! grep -q -- '--tf-output-json' "${_work}/o1.txt"; then
  printf 'FAIL: %s — case1 message does not name the --tf-output-json escape hatch\n' "${T}"
  cat "${_work}/o1.txt"
  exit 1
fi

# --- Case 2: neither file, no pg record -> passthrough with a clean env. ---
_d2="${_work}/case2"
mkdir -p "${_d2}"
_probe="${_work}/probe.sh"
_envdump="${_work}/envdump.txt"
cat > "${_probe}" <<PROBE
#!/bin/sh
env > "${_envdump}"
exit 0
PROBE
chmod +x "${_probe}"

_e=0
# Clear inherited PG* first, so any PG* seen in the dump was added by the wrapper.
( unset PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD PGSSLMODE
  PATH="${_bin}:${PATH}" sh "${WRAPPER}" "${_probe}" -chdir="${_d2}"
) > "${_work}/o2.txt" 2>&1 || _e=$?
if [ "${_e}" != "0" ]; then
  printf 'FAIL: %s — passthrough expected exit 0, got %s\n' "${T}" "${_e}"
  cat "${_work}/o2.txt"
  exit 1
fi
if grep -qE '^PG[A-Z_]*=' "${_envdump}"; then
  printf 'FAIL: %s — passthrough leaked PG* into the command environment:\n' "${T}"
  grep -E '^PG[A-Z_]*=' "${_envdump}"
  exit 1
fi
if [ -f "${_work}/DATABRICKS_CALLED" ]; then
  printf 'FAIL: %s — passthrough invoked the databricks CLI\n' "${T}"
  exit 1
fi

printf '  ok: fails closed without .lakebase.env; clean passthrough otherwise\n'
printf 'PASS: %s\n' "${T}"
