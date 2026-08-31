#!/usr/bin/env sh
# test_02_dry_run.sh — --dry-run must touch nothing and exit 0.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
GENERATED_DIR="${TESTS_DIR}/../../generated"
T="test_02_dry_run"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)
_staging="${_work}/staging"
mkdir -p "${_staging}"

# Stub prereqs so the prereq check passes (warnings only in dry-run, but stubs = clean output)
_stub_bin="${_work}/bin"
mkdir -p "${_stub_bin}"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/databricks"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/jq"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/curl"
chmod +x "${_stub_bin}/databricks" "${_stub_bin}/jq" "${_stub_bin}/curl"
# Symlink real python3
_rpy=$(command -v python3 2>/dev/null) || true
if [ -n "${_rpy}" ]; then
  ln -sf "${_rpy}" "${_stub_bin}/python3"
else
  printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/python3" && chmod +x "${_stub_bin}/python3"
fi

# Use real claude-code linux fixture (has managed-settings.json — required even in dry-run)
_cc_src="${GENERATED_DIR}/claude-code/linux"

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src}" \
  --target-root "${_staging}" \
  --dry-run > "${_work}/out.txt" 2>&1 || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt"
  exit 1
fi

# Staging dir must be completely empty — dry-run writes nothing
_found=$(find "${_staging}" -mindepth 1 | head -1)
if [ -n "${_found}" ]; then
  printf 'FAIL: %s — staging dir not empty after dry-run:\n' "${T}"
  find "${_staging}" -mindepth 1
  exit 1
fi

printf '  ok: dry-run exited 0 and staging dir is empty\n'
printf 'PASS: %s\n' "${T}"
