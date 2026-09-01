#!/usr/bin/env sh
# test_12_codex_local_install.sh — per-user local Codex placement.
#
# Exercises install-codex-local.sh (the non-managed, per-user complement to
# install.sh):
#   A: fresh target        -> config.toml placed, content matches source
#   B: existing target     -> timestamped backup made, new content installed
#   C: --dry-run           -> nothing written
#   D: missing source      -> exit 4
#   E: --print-target      -> prints the resolved target path, exit 0
#   F: --no-backup         -> existing file overwritten, no backup file made
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
LOCAL_SH="${TESTS_DIR}/../install-codex-local.sh"
T="test_12_codex_local_install"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

_src="${_work}/config.toml"
printf 'model_provider = "databricks"\nmodel = "cat.oai.gpt"\n' > "${_src}"
# The hook files sit beside the source config.toml; install must place them too.
printf '{ "description": "codex hooks" }\n' > "${_work}/hooks.json"
printf '#!/usr/bin/env bash\n# emit_hook_events.sh stub\n' > "${_work}/emit_hook_events.sh"

# ---------------------------------------------------------------------------
# E: --print-target (do this first; touches nothing)
# ---------------------------------------------------------------------------
_tdir="${_work}/cfg"
_got_target="$(sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" --print-target)"
if [ "${_got_target}" != "${_tdir}/config.toml" ]; then
  printf 'FAIL: %s/E — --print-target = %s, expected %s\n' "${T}" "${_got_target}" "${_tdir}/config.toml"
  exit 1
fi
printf '  ok E: --print-target -> %s\n' "${_got_target}"

# ---------------------------------------------------------------------------
# C: --dry-run writes nothing
# ---------------------------------------------------------------------------
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" --dry-run > "${_work}/out_c.txt" 2>&1
if [ -e "${_tdir}/config.toml" ] || [ -e "${_tdir}/hooks.json" ] || [ -e "${_tdir}/emit_hook_events.sh" ]; then
  printf 'FAIL: %s/C — --dry-run wrote a file\n' "${T}"
  exit 1
fi
printf '  ok C: --dry-run -> nothing written\n'

# ---------------------------------------------------------------------------
# A: fresh install
# ---------------------------------------------------------------------------
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" > "${_work}/out_a.txt" 2>&1
if [ ! -f "${_tdir}/config.toml" ]; then
  printf 'FAIL: %s/A — config.toml not installed to %s\n' "${T}" "${_tdir}"
  exit 1
fi
if ! cmp -s "${_src}" "${_tdir}/config.toml"; then
  printf 'FAIL: %s/A — installed content differs from source\n' "${T}"
  exit 1
fi
# the sibling hook files must be placed beside config.toml
if [ ! -f "${_tdir}/hooks.json" ] || [ ! -f "${_tdir}/emit_hook_events.sh" ]; then
  printf 'FAIL: %s/A — hook files not installed to %s\n' "${T}" "${_tdir}"
  exit 1
fi
# emit_hook_events.sh must be executable
if [ ! -x "${_tdir}/emit_hook_events.sh" ]; then
  printf 'FAIL: %s/A — emit_hook_events.sh not executable\n' "${T}"
  exit 1
fi
printf '  ok A: fresh install -> config.toml + hook files placed, content matches\n'

# ---------------------------------------------------------------------------
# B: existing target -> a backup is made and the new content wins
# ---------------------------------------------------------------------------
printf 'model = "OLD"\n' > "${_tdir}/config.toml"
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir}" > "${_work}/out_b.txt" 2>&1
if ! cmp -s "${_src}" "${_tdir}/config.toml"; then
  printf 'FAIL: %s/B — target was not overwritten with source content\n' "${T}"
  exit 1
fi
# exactly one backup file, holding the OLD content
_bak="$(find "${_tdir}" -name 'config.toml.bak-*' 2>/dev/null | head -1)"
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
printf 'model = "OLD2"\n' > "${_tdir2}/config.toml"
sh "${LOCAL_SH}" --source "${_src}" --target-dir "${_tdir2}" --no-backup > "${_work}/out_f.txt" 2>&1
if ! cmp -s "${_src}" "${_tdir2}/config.toml"; then
  printf 'FAIL: %s/F — target not overwritten with --no-backup\n' "${T}"
  exit 1
fi
if find "${_tdir2}" -name 'config.toml.bak-*' 2>/dev/null | grep -q .; then
  printf 'FAIL: %s/F — --no-backup should not create a backup file\n' "${T}"
  exit 1
fi
printf '  ok F: --no-backup -> overwritten, no backup file\n'

# ---------------------------------------------------------------------------
# D: missing source -> exit 4
# ---------------------------------------------------------------------------
_exit_d=0
sh "${LOCAL_SH}" --source "${_work}/does-not-exist.toml" --target-dir "${_tdir}" \
  > "${_work}/out_d.txt" 2>&1 || _exit_d=$?
if [ "${_exit_d}" != "4" ]; then
  printf 'FAIL: %s/D — missing source expected exit 4, got %d\n' "${T}" "${_exit_d}"
  cat "${_work}/out_d.txt"
  exit 1
fi
printf '  ok D: missing source -> exit 4\n'

printf 'PASS: %s\n' "${T}"
