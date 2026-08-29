#!/usr/bin/env sh
# test_08_codex_managed_user.sh — codex managed-vs-user detection.
#
# Case A: source has etc/managed_config.toml -> managed install (files copied)
# Case B: source has only config.toml at root -> user-mode warn + skip (no files installed)
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
T="test_08_codex_managed_user"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

# Prereq stubs (codex-only agents; no emit_hook_events.sh in hand-crafted sources)
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
# Case A: managed mode (etc/managed_config.toml present)
# Hand-craft a minimal managed bundle — installer only checks existence and copies.
# ---------------------------------------------------------------------------
_managed_src="${_work}/managed_src"
mkdir -p "${_managed_src}/etc"
printf 'model_provider = "databricks"\n' > "${_managed_src}/etc/managed_config.toml"
printf 'allow_managed_hooks_only = true\n' > "${_managed_src}/etc/requirements.toml"
# (No emit_hook_events.sh — optional file; tests minimal managed install)

_stage_a="${_work}/stage_a"
mkdir -p "${_stage_a}"

_exit_a=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents codex \
  --codex-source "${_managed_src}" \
  --target-root "${_stage_a}" \
  > "${_work}/out_a.txt" 2>&1 || _exit_a=$?

if [ "${_exit_a}" != "0" ]; then
  printf 'FAIL: %s/A — managed mode expected exit 0, got %d\n' "${T}" "${_exit_a}"
  cat "${_work}/out_a.txt"
  exit 1
fi

# managed_config.toml and requirements.toml must exist in target
_cx_target_a="${_stage_a}/etc/codex"
if [ ! -f "${_cx_target_a}/managed_config.toml" ]; then
  printf 'FAIL: %s/A — managed_config.toml not installed to %s\n' "${T}" "${_cx_target_a}"
  exit 1
fi
if [ ! -f "${_cx_target_a}/requirements.toml" ]; then
  printf 'FAIL: %s/A — requirements.toml not installed to %s\n' "${T}" "${_cx_target_a}"
  exit 1
fi
printf '  ok A: managed mode -> managed_config.toml and requirements.toml installed\n'

# ---------------------------------------------------------------------------
# Case B: user-mode (only config.toml at bundle root — not etc/managed_config.toml)
# Installer must warn and skip; exit 0; NO files written to target.
# ---------------------------------------------------------------------------
_user_src="${_work}/user_src"
mkdir -p "${_user_src}"
printf 'model_provider = "databricks"\n' > "${_user_src}/config.toml"

_stage_b="${_work}/stage_b"
mkdir -p "${_stage_b}"

_exit_b=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents codex \
  --codex-source "${_user_src}" \
  --target-root "${_stage_b}" \
  > "${_work}/out_b.txt" 2>&1 || _exit_b=$?

if [ "${_exit_b}" != "0" ]; then
  printf 'FAIL: %s/B — user-mode expected exit 0, got %d\n' "${T}" "${_exit_b}"
  cat "${_work}/out_b.txt"
  exit 1
fi

# No codex files must have been written to the target
_cx_target_b="${_stage_b}/etc/codex"
if [ -f "${_cx_target_b}/managed_config.toml" ] || \
   [ -f "${_cx_target_b}/requirements.toml" ]; then
  printf 'FAIL: %s/B — user-mode should not install files, but found files in %s\n' \
    "${T}" "${_cx_target_b}"
  exit 1
fi

# stderr/stdout should contain a user-mode warning
if ! grep -qi 'user.mode\|user-mode\|not.*root-managed\|skipping' "${_work}/out_b.txt"; then
  printf 'FAIL: %s/B — expected user-mode skip warning in output\n' "${T}"
  cat "${_work}/out_b.txt"
  exit 1
fi
printf '  ok B: user-mode config.toml -> warned and skipped, no files installed\n'

printf 'PASS: %s\n' "${T}"
