#!/usr/bin/env sh
# test_07_idempotent_upgrade.sh — marker log: installed / unchanged / upgraded.
# Run 1 with VERSION=v1  -> "installed"
# Run 2 same VERSION=v1  -> "unchanged"
# Run 3 with VERSION=v2  -> "upgraded"
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
. "${TESTS_DIR}/_fixtures.sh"
T="test_07_idempotent_upgrade"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

# ---------------------------------------------------------------------------
# Source bundle: uses --source convention so VERSION is read from SOURCE/VERSION.
# Lay out: <bundle_root>/claude-code/linux/managed-settings.json
#          <bundle_root>/VERSION
# ---------------------------------------------------------------------------
_bundle="${_work}/bundle"
mk_claude_bundle "${_bundle}/claude-code/linux" off
printf 'v1\n' > "${_bundle}/VERSION"

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

# ---------------------------------------------------------------------------
# Run 1 — expect "installed"
# ---------------------------------------------------------------------------
_exit1=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --source "${_bundle}" --os linux --agents claude-code \
  --target-root "${_staging}" \
  > "${_work}/out1.txt" 2>&1 || _exit1=$?

if [ "${_exit1}" != "0" ]; then
  printf 'FAIL: %s/run1 — expected exit 0, got %d\n' "${T}" "${_exit1}"
  cat "${_work}/out1.txt"
  exit 1
fi
if ! grep -q 'marker.*installed' "${_work}/out1.txt"; then
  printf 'FAIL: %s/run1 — expected "installed" in marker line\n' "${T}"
  grep 'marker' "${_work}/out1.txt" || true
  exit 1
fi
printf '  ok run1 (VERSION=v1): marker says "installed"\n'

# ---------------------------------------------------------------------------
# Run 2 — same VERSION, expect "unchanged"
# ---------------------------------------------------------------------------
_exit2=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --source "${_bundle}" --os linux --agents claude-code \
  --target-root "${_staging}" \
  > "${_work}/out2.txt" 2>&1 || _exit2=$?

if [ "${_exit2}" != "0" ]; then
  printf 'FAIL: %s/run2 — expected exit 0, got %d\n' "${T}" "${_exit2}"
  cat "${_work}/out2.txt"
  exit 1
fi
if ! grep -q 'marker.*unchanged' "${_work}/out2.txt"; then
  printf 'FAIL: %s/run2 — expected "unchanged" in marker line\n' "${T}"
  grep 'marker' "${_work}/out2.txt" || true
  exit 1
fi
printf '  ok run2 (VERSION=v1 again): marker says "unchanged"\n'

# ---------------------------------------------------------------------------
# Run 3 — change VERSION to v2, expect "upgraded"
# ---------------------------------------------------------------------------
printf 'v2\n' > "${_bundle}/VERSION"
_exit3=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --source "${_bundle}" --os linux --agents claude-code \
  --target-root "${_staging}" \
  > "${_work}/out3.txt" 2>&1 || _exit3=$?

if [ "${_exit3}" != "0" ]; then
  printf 'FAIL: %s/run3 — expected exit 0, got %d\n' "${T}" "${_exit3}"
  cat "${_work}/out3.txt"
  exit 1
fi
if ! grep -q 'marker.*upgraded' "${_work}/out3.txt"; then
  printf 'FAIL: %s/run3 — expected "upgraded" in marker line\n' "${T}"
  grep 'marker' "${_work}/out3.txt" || true
  exit 1
fi
printf '  ok run3 (VERSION=v2): marker says "upgraded"\n'

printf 'PASS: %s\n' "${T}"
