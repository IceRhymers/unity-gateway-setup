#!/usr/bin/env sh
# test_03_staged_install.sh — real staged install: modes 644/755, chown-skip notice,
# and .unity-gateway-version marker exists at 644.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
. "${TESTS_DIR}/_fixtures.sh"
T="test_03_staged_install"

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

# Synthetic telemetry-ON claude-code bundle (managed-settings.json + both scripts).
_cc_src="${_work}/cc_src"
mk_claude_bundle "${_cc_src}" on

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src}" \
  --target-root "${_staging}" \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

_target="${_staging}/etc/claude-code"

# Portable mode-check: try GNU stat first, fall back to BSD stat.
# Returns 3-digit octal like 644 or 755.
_file_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    stat -c '%a' "$1"
  else
    # BSD/macOS: stat -f '%p' gives 100644; take last 3 chars via awk
    stat -f '%p' "$1" 2>/dev/null | awk '{print substr($0, length($0)-2)}'
  fi
}

_assert_mode() {
  # $1=file  $2=expected_octal (e.g. 644 or 755)
  _am_got=$(_file_mode "$1")
  if [ "${_am_got}" != "$2" ]; then
    printf 'FAIL: %s — mode of %s = %s, expected %s\n' "${T}" "$1" "${_am_got}" "$2"
    exit 1
  fi
}

# managed-settings.json must exist and be 644
_msj="${_target}/managed-settings.json"
if [ ! -f "${_msj}" ]; then
  printf 'FAIL: %s — managed-settings.json not found at %s\n' "${T}" "${_msj}"
  exit 1
fi
_assert_mode "${_msj}" 644
printf '  ok: managed-settings.json exists at mode 644\n'

# otel-headers-helper.sh must be 755
_otel="${_target}/otel-headers-helper.sh"
if [ ! -f "${_otel}" ]; then
  printf 'FAIL: %s — otel-headers-helper.sh not found\n' "${T}"
  exit 1
fi
_assert_mode "${_otel}" 755
printf '  ok: otel-headers-helper.sh exists at mode 755\n'

# emit_hook_events.sh must be 755
_emit="${_target}/emit_hook_events.sh"
if [ ! -f "${_emit}" ]; then
  printf 'FAIL: %s — emit_hook_events.sh not found\n' "${T}"
  exit 1
fi
_assert_mode "${_emit}" 755
printf '  ok: emit_hook_events.sh exists at mode 755\n'

# .unity-gateway-version marker must exist and be 644
_marker="${_target}/.unity-gateway-version"
if [ ! -f "${_marker}" ]; then
  printf 'FAIL: %s — .unity-gateway-version marker not found\n' "${T}"
  exit 1
fi
_assert_mode "${_marker}" 644
printf '  ok: .unity-gateway-version marker exists at mode 644\n'

# stderr must contain the chown-skip notice
if ! grep -q 'non-root: skipping ownership changes' "${_work}/err.txt"; then
  printf 'FAIL: %s — expected chown-skip notice on stderr\n' "${T}"
  printf '  stderr was:\n'
  cat "${_work}/err.txt"
  exit 1
fi
printf '  ok: chown-skip notice present on stderr\n'

printf 'PASS: %s\n' "${T}"
