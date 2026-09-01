#!/usr/bin/env sh
# test_10_opencode_managed_user.sh — opencode managed-vs-user detection.
#
# Case A: source has ai.opencode.managed.mobileconfig -> managed install (files copied)
# Case B: source has only opencode.json at root -> user-mode warn + skip (no files installed)
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
T="test_10_opencode_managed_user"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

# Prereq stubs (opencode-only agents; no emit_hook_events.sh in hand-crafted sources)
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
# Case A: managed mode (ai.opencode.managed.mobileconfig present, linux)
# Hand-craft a minimal managed bundle — installer only checks existence and copies.
# On linux the .mobileconfig is not placed (macOS hard-lock only), but opencode.json is.
# ---------------------------------------------------------------------------
_managed_src="${_work}/managed_src"
mkdir -p "${_managed_src}"
printf '{ "enabled_providers": ["databricks-oss"], "plugin": ["./databricks-auth.ts"] }\n' > "${_managed_src}/opencode.json"
printf '// databricks-auth.ts stub\n' > "${_managed_src}/databricks-auth.ts"
printf '<?xml version="1.0"?>\n<plist></plist>\n' > "${_managed_src}/ai.opencode.managed.mobileconfig"

_stage_a="${_work}/stage_a"
mkdir -p "${_stage_a}"

_exit_a=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents opencode \
  --opencode-source "${_managed_src}" \
  --target-root "${_stage_a}" \
  > "${_work}/out_a.txt" 2>&1 || _exit_a=$?

if [ "${_exit_a}" != "0" ]; then
  printf 'FAIL: %s/A — managed mode expected exit 0, got %d\n' "${T}" "${_exit_a}"
  cat "${_work}/out_a.txt"
  exit 1
fi

# opencode.json must exist in target (linux managed dir)
_oc_target_a="${_stage_a}/etc/opencode"
if [ ! -f "${_oc_target_a}/opencode.json" ]; then
  printf 'FAIL: %s/A — opencode.json not installed to %s\n' "${T}" "${_oc_target_a}"
  exit 1
fi
# the auth plugin must sit beside opencode.json (config references it relatively)
if [ ! -f "${_oc_target_a}/databricks-auth.ts" ]; then
  printf 'FAIL: %s/A — databricks-auth.ts not installed beside opencode.json in %s\n' "${T}" "${_oc_target_a}"
  exit 1
fi
printf '  ok A: managed mode -> opencode.json + databricks-auth.ts installed to %s\n' "${_oc_target_a}"

# ---------------------------------------------------------------------------
# Case A2: managed mode on macOS -> the .mobileconfig is also staged
# ---------------------------------------------------------------------------
_stage_a2="${_work}/stage_a2"
mkdir -p "${_stage_a2}"

_exit_a2=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os macos --agents opencode \
  --opencode-source "${_managed_src}" \
  --target-root "${_stage_a2}" \
  > "${_work}/out_a2.txt" 2>&1 || _exit_a2=$?

if [ "${_exit_a2}" != "0" ]; then
  printf 'FAIL: %s/A2 — macOS managed mode expected exit 0, got %d\n' "${T}" "${_exit_a2}"
  cat "${_work}/out_a2.txt"
  exit 1
fi

_oc_target_a2="${_stage_a2}/Library/Application Support/opencode"
if [ ! -f "${_oc_target_a2}/opencode.json" ]; then
  printf 'FAIL: %s/A2 — opencode.json not installed to %s\n' "${T}" "${_oc_target_a2}"
  exit 1
fi
if [ ! -f "${_oc_target_a2}/ai.opencode.managed.mobileconfig" ]; then
  printf 'FAIL: %s/A2 — .mobileconfig not staged to %s\n' "${T}" "${_oc_target_a2}"
  exit 1
fi
if [ ! -f "${_oc_target_a2}/databricks-auth.ts" ]; then
  printf 'FAIL: %s/A2 — databricks-auth.ts not staged to %s\n' "${T}" "${_oc_target_a2}"
  exit 1
fi
printf '  ok A2: macOS managed mode -> opencode.json + databricks-auth.ts + .mobileconfig staged\n'

# ---------------------------------------------------------------------------
# Case B: user-mode (only opencode.json at bundle root — no .mobileconfig)
# Installer must warn and skip; exit 0; NO files written to target.
# ---------------------------------------------------------------------------
_user_src="${_work}/user_src"
mkdir -p "${_user_src}"
printf '{ "enabled_providers": ["databricks"] }\n' > "${_user_src}/opencode.json"

_stage_b="${_work}/stage_b"
mkdir -p "${_stage_b}"

_exit_b=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents opencode \
  --opencode-source "${_user_src}" \
  --target-root "${_stage_b}" \
  > "${_work}/out_b.txt" 2>&1 || _exit_b=$?

if [ "${_exit_b}" != "0" ]; then
  printf 'FAIL: %s/B — user-mode expected exit 0, got %d\n' "${T}" "${_exit_b}"
  cat "${_work}/out_b.txt"
  exit 1
fi

# No opencode files must have been written to the target
_oc_target_b="${_stage_b}/etc/opencode"
if [ -f "${_oc_target_b}/opencode.json" ]; then
  printf 'FAIL: %s/B — user-mode should not install files, but found files in %s\n' \
    "${T}" "${_oc_target_b}"
  exit 1
fi

# stderr/stdout should contain a user-mode warning
if ! grep -qi 'user.mode\|user-mode\|not.*root-managed\|skipping' "${_work}/out_b.txt"; then
  printf 'FAIL: %s/B — expected user-mode skip warning in output\n' "${T}"
  cat "${_work}/out_b.txt"
  exit 1
fi
printf '  ok B: user-mode opencode.json -> warned and skipped, no files installed\n'

printf 'PASS: %s\n' "${T}"
