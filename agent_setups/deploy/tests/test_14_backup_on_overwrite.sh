#!/usr/bin/env sh
# test_14_backup_on_overwrite.sh — backup-on-change behavior in _action_copy.
#
# (1) First install: no .bak-* files created.
# (2) Byte-identical re-install: no .bak-* files, output says "unchanged:".
# (3) Changed content: exactly one .bak-* with OLD bytes, live file has NEW bytes.
# (4) --dry-run over changed file: [plan] backup line printed, no file written.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
# shellcheck disable=SC1091
. "${TESTS_DIR}/_fixtures.sh"
T="test_14_backup_on_overwrite"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)
_staging="${_work}/staging"
mkdir -p "${_staging}"

# Prereq stubs
_stub_bin="${_work}/bin"
mkdir -p "${_stub_bin}"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/databricks"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/jq"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/curl"
chmod +x "${_stub_bin}/databricks" "${_stub_bin}/jq" "${_stub_bin}/curl"
_rpy=$(command -v python3 2>/dev/null) || true
if [ -n "${_rpy}" ]; then
  ln -sf "${_rpy}" "${_stub_bin}/python3"
else
  printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/python3" && chmod +x "${_stub_bin}/python3"
fi

_cc_src="${_work}/cc_src"
mk_claude_bundle "${_cc_src}" off
_target_dir="${_staging}/etc/claude-code"
_target_msj="${_target_dir}/managed-settings.json"

_run_install() {
  PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
    --os linux --agents claude-code \
    --claude-source "${_cc_src}" \
    --target-root "${_staging}" \
    "$@" \
    > "${_work}/out.txt" 2> "${_work}/err.txt"
}

# ---------------------------------------------------------------------------
# (1) First install — no .bak-* files
# ---------------------------------------------------------------------------
_exit=0
_run_install || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(1) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
_bak_count=$(find "${_target_dir}" -name '*.bak-*' 2>/dev/null | wc -l | tr -d ' ')
if [ "${_bak_count}" != "0" ]; then
  printf 'FAIL: %s/(1) — expected no .bak-* on first install, found %s\n' "${T}" "${_bak_count}"
  find "${_target_dir}" -name '*.bak-*'
  exit 1
fi
printf '  ok (1): first install creates no .bak-* files\n'

# ---------------------------------------------------------------------------
# (2) Byte-identical re-install — no .bak-*, output contains "unchanged:"
# ---------------------------------------------------------------------------
_exit=0
_run_install || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(2) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
_bak_count=$(find "${_target_dir}" -name '*.bak-*' 2>/dev/null | wc -l | tr -d ' ')
if [ "${_bak_count}" != "0" ]; then
  printf 'FAIL: %s/(2) — expected no .bak-* on identical re-install, found %s\n' "${T}" "${_bak_count}"
  find "${_target_dir}" -name '*.bak-*'
  exit 1
fi
if ! grep -q 'unchanged:' "${_work}/out.txt"; then
  printf 'FAIL: %s/(2) — expected "unchanged:" in output\n' "${T}"
  cat "${_work}/out.txt"
  exit 1
fi
printf '  ok (2): identical re-install produces no .bak-*, prints "unchanged:"\n'

# ---------------------------------------------------------------------------
# (3) Changed content — one .bak-* with OLD bytes, live file has NEW bytes
# ---------------------------------------------------------------------------
_old_content="$(cat "${_target_msj}")"
_new_content='{ "env": {}, "model": "databricks/changed" }'
printf '%s\n' "${_new_content}" > "${_cc_src}/managed-settings.json"

_exit=0
_run_install || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(3) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

_bak_count=$(find "${_target_dir}" -name '*.bak-*' 2>/dev/null | wc -l | tr -d ' ')
if [ "${_bak_count}" != "1" ]; then
  printf 'FAIL: %s/(3) — expected exactly one .bak-* file, found %s\n' "${T}" "${_bak_count}"
  find "${_target_dir}" -name '*.bak-*'
  exit 1
fi

# The .bak-* file must contain the OLD bytes.
_bak_file=$(find "${_target_dir}" -name '*.bak-*' | head -n1)
_bak_content="$(cat "${_bak_file}")"
if [ "${_bak_content}" != "${_old_content}" ]; then
  printf 'FAIL: %s/(3) — .bak-* does not contain old bytes\n' "${T}"
  printf '  expected: %s\n' "${_old_content}"
  printf '  got:      %s\n' "${_bak_content}"
  exit 1
fi

# The live file must contain the NEW bytes.
_live_content="$(cat "${_target_msj}")"
if [ "${_live_content}" != "${_new_content}" ]; then
  printf 'FAIL: %s/(3) — live file does not contain new bytes\n' "${T}"
  printf '  expected: %s\n' "${_new_content}"
  printf '  got:      %s\n' "${_live_content}"
  exit 1
fi
printf '  ok (3): changed install backs up old bytes, places new bytes\n'

# ---------------------------------------------------------------------------
# (4) --dry-run over changed content — [plan] backup line, no new .bak-*
# ---------------------------------------------------------------------------
# Change the source again so the installed file and source differ.
printf '{ "env": {}, "model": "databricks/dry-run-change" }\n' > "${_cc_src}/managed-settings.json"

_bak_before=$(find "${_target_dir}" -name '*.bak-*' 2>/dev/null | wc -l | tr -d ' ')

_exit=0
_run_install --dry-run || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(4) — expected exit 0 from --dry-run, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

if ! grep -q '\[plan\] backup' "${_work}/out.txt"; then
  printf 'FAIL: %s/(4) — expected "[plan] backup" in dry-run output\n' "${T}"
  cat "${_work}/out.txt"
  exit 1
fi

_bak_after=$(find "${_target_dir}" -name '*.bak-*' 2>/dev/null | wc -l | tr -d ' ')
if [ "${_bak_after}" != "${_bak_before}" ]; then
  printf 'FAIL: %s/(4) — dry-run must not create .bak-* files (before=%s after=%s)\n' \
    "${T}" "${_bak_before}" "${_bak_after}"
  exit 1
fi
printf '  ok (4): --dry-run prints [plan] backup and creates no files\n'

printf 'PASS: %s\n' "${T}"
