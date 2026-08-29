#!/usr/bin/env sh
# test_04_exit4_missing_settings.sh — missing managed-settings.json must exit 4.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
T="test_04_exit4_missing_settings"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)
_staging="${_work}/staging"
mkdir -p "${_staging}"

# Source dir with NO managed-settings.json
_empty_src="${_work}/empty_src"
mkdir -p "${_empty_src}"

# Prereq stubs (prereq check runs before source check)
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

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_empty_src}" \
  --target-root "${_staging}" \
  > "${_work}/out.txt" 2>&1 || _exit=$?

if [ "${_exit}" != "4" ]; then
  printf 'FAIL: %s — expected exit 4, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt"
  exit 1
fi

printf '  ok: missing managed-settings.json -> exit 4\n'
printf 'PASS: %s\n' "${T}"
