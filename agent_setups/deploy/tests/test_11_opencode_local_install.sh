#!/usr/bin/env sh
# test_11_opencode_local_install.sh — per-user local opencode placement.
#
# Exercises install-opencode-local.sh (the non-managed, per-user complement to
# install.sh):
#   A: fresh target        -> opencode.json placed, content matches source
#   B: existing target     -> timestamped backup made, new content installed
#   C: --dry-run           -> nothing written
#   D: missing source      -> exit 4
#   E: --print-target      -> prints the resolved target path, exit 0
#   F: --no-backup         -> existing file overwritten, no backup file made
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
LOCAL_SH="${TESTS_DIR}/../install-opencode-local.sh"
T="test_11_opencode_local_install"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

_src="${_work}/opencode.json"
printf '{ "model": "databricks/x", "enabled_providers": ["databricks"] }\n' > "${_src}"

# ---------------------------------------------------------------------------
# E: --print-target (do this first; touches nothing)
# ---------------------------------------------------------------------------
_tdir="${_work}/cfg"
_got_target="$(sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" --print-target)"
if [ "${_got_target}" != "${_tdir}/opencode.json" ]; then
  printf 'FAIL: %s/E — --print-target = %s, expected %s\n' "${T}" "${_got_target}" "${_tdir}/opencode.json"
  exit 1
fi
printf '  ok E: --print-target -> %s\n' "${_got_target}"

# ---------------------------------------------------------------------------
# C: --dry-run writes nothing
# ---------------------------------------------------------------------------
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" --dry-run > "${_work}/out_c.txt" 2>&1
if [ -e "${_tdir}/opencode.json" ]; then
  printf 'FAIL: %s/C — --dry-run wrote a file\n' "${T}"
  exit 1
fi
printf '  ok C: --dry-run -> nothing written\n'

# ---------------------------------------------------------------------------
# A: fresh install
# ---------------------------------------------------------------------------
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" > "${_work}/out_a.txt" 2>&1
if [ ! -f "${_tdir}/opencode.json" ]; then
  printf 'FAIL: %s/A — opencode.json not installed to %s\n' "${T}" "${_tdir}"
  exit 1
fi
if ! cmp -s "${_src}" "${_tdir}/opencode.json"; then
  printf 'FAIL: %s/A — installed content differs from source\n' "${T}"
  exit 1
fi
printf '  ok A: fresh install -> opencode.json placed, content matches\n'

# ---------------------------------------------------------------------------
# B: existing target -> a backup is made and the new content wins
# ---------------------------------------------------------------------------
printf '{ "model": "databricks/OLD" }\n' > "${_tdir}/opencode.json"
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" > "${_work}/out_b.txt" 2>&1
if ! cmp -s "${_src}" "${_tdir}/opencode.json"; then
  printf 'FAIL: %s/B — target was not overwritten with source content\n' "${T}"
  exit 1
fi
# exactly one backup file, holding the OLD content
_bak="$(find "${_tdir}" -name 'opencode.json.bak-*' 2>/dev/null | head -1)"
if [ -z "${_bak}" ] || [ ! -f "${_bak}" ]; then
  printf 'FAIL: %s/B — no backup file created for existing config\n' "${T}"
  exit 1
fi
if ! grep -q 'OLD' "${_bak}"; then
  printf 'FAIL: %s/B — backup does not hold the previous content\n' "${T}"
  exit 1
fi
printf '  ok B: existing target -> backup made, new content installed\n'

# ---------------------------------------------------------------------------
# F: --no-backup overwrites without a new backup file
# ---------------------------------------------------------------------------
_tdir2="${_work}/cfg2"
mkdir -p "${_tdir2}"
printf '{ "model": "databricks/OLD2" }\n' > "${_tdir2}/opencode.json"
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir2}" --no-backup > "${_work}/out_f.txt" 2>&1
if ! cmp -s "${_src}" "${_tdir2}/opencode.json"; then
  printf 'FAIL: %s/F — target not overwritten with --no-backup\n' "${T}"
  exit 1
fi
if find "${_tdir2}" -name 'opencode.json.bak-*' 2>/dev/null | grep -q .; then
  printf 'FAIL: %s/F — --no-backup should not create a backup file\n' "${T}"
  exit 1
fi
printf '  ok F: --no-backup -> overwritten, no backup file\n'

# ---------------------------------------------------------------------------
# D: missing source -> exit 4
# ---------------------------------------------------------------------------
_exit_d=0
sh "${LOCAL_SH}" --source "${_work}/does-not-exist.json" --target-dir "${_tdir}" \
  > "${_work}/out_d.txt" 2>&1 || _exit_d=$?
if [ "${_exit_d}" != "4" ]; then
  printf 'FAIL: %s/D — missing source expected exit 4, got %d\n' "${T}" "${_exit_d}"
  cat "${_work}/out_d.txt"
  exit 1
fi
printf '  ok D: missing source -> exit 4\n'

printf 'PASS: %s\n' "${T}"
