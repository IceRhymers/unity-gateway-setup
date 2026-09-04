#!/bin/sh
# test_10_conn_str_refusal.sh — with-state.sh refuses PG_CONN_STR before all
# else (the pg backend would write it into .terraform/ and plan files), even in
# the passthrough quadrant, and sanitizes PGHOSTADDR and PGSERVICE.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
WRAPPER="${BOOTSTRAP_DIR}/with-state.sh"
T="test_10_conn_str_refusal"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

_bin="${_work}/bin"
mkdir -p "${_bin}"
printf '#!/bin/sh\nexit 0\n' > "${_bin}/databricks"
chmod +x "${_bin}/databricks"

# --- PG_CONN_STR refused even in the passthrough quadrant (empty tf dir). ---
_dA="${_work}/a"
mkdir -p "${_dA}"
_e=0
( PG_CONN_STR='host=evil port=5432'
  export PG_CONN_STR
  PATH="${_bin}:${PATH}" sh "${WRAPPER}" true -chdir="${_dA}"
) > "${_work}/oa.txt" 2>&1 || _e=$?
if [ "${_e}" -eq 0 ]; then
  printf 'FAIL: %s — PG_CONN_STR was not refused in the passthrough quadrant\n' "${T}"
  exit 1
fi
if ! grep -q 'PG_CONN_STR' "${_work}/oa.txt"; then
  printf 'FAIL: %s — refusal message does not name PG_CONN_STR\n' "${T}"
  cat "${_work}/oa.txt"
  exit 1
fi

# --- PGHOSTADDR and PGSERVICE are unset before the command runs. ---
_dB="${_work}/b"
mkdir -p "${_dB}"
_probe="${_work}/probe.sh"
_envdump="${_work}/envdump.txt"
cat > "${_probe}" <<PROBE
#!/bin/sh
env > "${_envdump}"
exit 0
PROBE
chmod +x "${_probe}"

_e=0
( PGHOSTADDR='203.0.113.9'
  PGSERVICE='some-service'
  export PGHOSTADDR PGSERVICE
  PATH="${_bin}:${PATH}" sh "${WRAPPER}" "${_probe}" -chdir="${_dB}"
) > "${_work}/ob.txt" 2>&1 || _e=$?
if [ "${_e}" != "0" ]; then
  printf 'FAIL: %s — passthrough with PGHOSTADDR/PGSERVICE exited %s\n' "${T}" "${_e}"
  cat "${_work}/ob.txt"
  exit 1
fi
if grep -qE '^(PGHOSTADDR|PGSERVICE)=' "${_envdump}"; then
  printf 'FAIL: %s — PGHOSTADDR/PGSERVICE were not sanitized:\n' "${T}"
  grep -E '^(PGHOSTADDR|PGSERVICE)=' "${_envdump}"
  exit 1
fi

printf '  ok: PG_CONN_STR refused in passthrough; PGHOSTADDR/PGSERVICE sanitized\n'
printf 'PASS: %s\n' "${T}"
