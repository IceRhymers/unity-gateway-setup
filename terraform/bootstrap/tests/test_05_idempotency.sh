#!/bin/sh
# test_05_idempotency.sh — --dry-run renders byte-identically on re-run, and a
# pre-planted differing file is refused (exit 3) unless --force overwrites it.
# See plan §7 and the acceptance table row for test_05.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
BOOTSTRAP="${BOOTSTRAP_DIR}/bootstrap-state.sh"
T="test_05_idempotency"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

_bin="${_work}/bin"
mkdir -p "${_bin}"
printf '#!/bin/sh\ntouch "%s/DATABRICKS_CALLED"\nexit 0\n' "${_work}" > "${_bin}/databricks"
chmod +x "${_bin}/databricks"

_render() {  # _render <out-dir> [extra args...]; echoes nothing, returns exit code
  _rd="$1"; shift
  _re=0
  PATH="${_bin}:${PATH}" sh "${BOOTSTRAP}" \
    --profile fevm-west --dry-run --out "${_rd}" "$@" >/dev/null 2>&1 || _re=$?
  return "${_re}"
}

# Two independent renders with identical inputs must be byte-identical.
_a="${_work}/a"; _b="${_work}/b"
_render "${_a}" || { printf 'FAIL: %s — first render failed\n' "${T}"; exit 1; }
_render "${_b}" || { printf 'FAIL: %s — second render failed\n' "${T}"; exit 1; }
if ! cmp -s "${_a}/.lakebase.env" "${_b}/.lakebase.env"; then
  printf 'FAIL: %s — two renders are not byte-identical\n' "${T}"
  diff "${_a}/.lakebase.env" "${_b}/.lakebase.env" || true
  exit 1
fi

# Re-render into an identical existing file: idempotent, exit 0.
_render "${_a}" || { printf 'FAIL: %s — identical re-render did not exit 0\n' "${T}"; exit 1; }

# Pre-plant a DIFFERING file: exit 3 without --force.
printf 'PGHOST=planted-and-different\n' > "${_a}/.lakebase.env"
_e=0
_render "${_a}" || _e=$?
if [ "${_e}" != "3" ]; then
  printf 'FAIL: %s — differing file without --force: expected exit 3, got %s\n' "${T}" "${_e}"
  exit 1
fi

# Same differing file, with --force: exit 0 (overwrites).
_e=0
_render "${_a}" --force || _e=$?
if [ "${_e}" != "0" ]; then
  printf 'FAIL: %s — differing file with --force: expected exit 0, got %s\n' "${T}" "${_e}"
  exit 1
fi

printf '  ok: renders are byte-identical; differing file gated by exit 3 / --force\n'
printf 'PASS: %s\n' "${T}"
