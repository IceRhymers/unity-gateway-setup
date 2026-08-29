#!/usr/bin/env sh
# test_06_prereq_criticality.sh — verify prereq criticality matrix using fake PATH.
#
# Sub-tests:
#  6a: telemetry-OFF bundle, python3 absent  -> exit 3 (python3 always critical)
#  6b: telemetry-OFF bundle, python3 present -> exit 0
#  6c: telemetry-OFF bundle, python3+curl present, jq absent -> exit 0 (jq NOT critical without emitter)
#  6d: telemetry-ON  bundle, python3+curl present, jq absent -> exit 3 (jq critical WITH emitter)
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
GENERATED_DIR="${TESTS_DIR}/../../generated"
T="test_06_prereq_criticality"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

# ---------------------------------------------------------------------------
# Helper: populate a dir with symlinks to all system utilities install.sh
# needs.  Caller adds python3 / databricks / jq / curl stubs as required.
# ---------------------------------------------------------------------------
_mk_sys_bin() {
  _msb="${1}"
  mkdir -p "${_msb}"
  # sh must be symlinked because `PATH=fakedir sh script` uses the fake PATH to
  # find sh itself (POSIX: env-prefix overrides PATH for the command lookup).
  for _u in sh uname id mkdir cp chmod chown cat grep cut date awk \
            shasum sha256sum basename dirname; do
    _rp=$(command -v "${_u}" 2>/dev/null) || true
    [ -n "${_rp}" ] && ln -sf "${_rp}" "${_msb}/${_u}"
  done
}

# ---------------------------------------------------------------------------
# Build source bundles
# ---------------------------------------------------------------------------
_linux_src="${GENERATED_DIR}/claude-code/linux"

# Telemetry-OFF bundle: managed-settings.json only, no shell scripts
_off_bundle="${_work}/off_bundle"
mkdir -p "${_off_bundle}"
cp "${_linux_src}/managed-settings.json" "${_off_bundle}/"
# Confirm: no emit or otel scripts in telemetry-off bundle
_emit_present=0
[ -f "${_off_bundle}/emit_hook_events.sh" ] && _emit_present=1
if [ "${_emit_present}" = "1" ]; then
  printf 'FAIL: %s — off_bundle setup error: emit_hook_events.sh unexpectedly present\n' "${T}"
  exit 1
fi

# Telemetry-ON bundle: use the real linux fixture (has all three files)
_on_bundle="${_linux_src}"
if [ ! -f "${_on_bundle}/emit_hook_events.sh" ]; then
  printf 'FAIL: %s — telemetry-ON fixture missing emit_hook_events.sh\n' "${T}"
  exit 1
fi

# ---------------------------------------------------------------------------
# Sub-test 6a: telemetry-OFF bundle, NO python3 -> exit 3
# ---------------------------------------------------------------------------
_b6a="${_work}/bin6a"
_mk_sys_bin "${_b6a}"
printf '#!/bin/sh\nexit 0\n' > "${_b6a}/databricks" && chmod +x "${_b6a}/databricks"
# jq and curl: not in PATH (irrelevant since telemetry-off, but should not be critical)
# python3: deliberately absent

_s6a="${_work}/stage6a"
mkdir -p "${_s6a}"
_exit6a=0
PATH="${_b6a}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_off_bundle}" \
  --target-root "${_s6a}" \
  > "${_work}/out6a.txt" 2>&1 || _exit6a=$?

if [ "${_exit6a}" != "3" ]; then
  printf 'FAIL: %s/6a — expected exit 3 (python3 missing), got %d\n' "${T}" "${_exit6a}"
  cat "${_work}/out6a.txt"
  exit 1
fi
if ! grep -q 'python3' "${_work}/out6a.txt"; then
  printf 'FAIL: %s/6a — output does not mention python3\n' "${T}"
  cat "${_work}/out6a.txt"
  exit 1
fi
printf '  ok 6a: telemetry-off, no python3 -> exit 3 (python3 cited)\n'

# ---------------------------------------------------------------------------
# Sub-test 6b: telemetry-OFF bundle, python3 + all prereqs present -> exit 0
# ---------------------------------------------------------------------------
_b6b="${_work}/bin6b"
_mk_sys_bin "${_b6b}"
printf '#!/bin/sh\nexit 0\n' > "${_b6b}/databricks" && chmod +x "${_b6b}/databricks"
printf '#!/bin/sh\nexit 0\n' > "${_b6b}/jq"          && chmod +x "${_b6b}/jq"
printf '#!/bin/sh\nexit 0\n' > "${_b6b}/curl"         && chmod +x "${_b6b}/curl"
_rpy=$(command -v python3 2>/dev/null) || true
if [ -n "${_rpy}" ]; then
  ln -sf "${_rpy}" "${_b6b}/python3"
else
  printf '#!/bin/sh\nexit 0\n' > "${_b6b}/python3" && chmod +x "${_b6b}/python3"
fi

_s6b="${_work}/stage6b"
mkdir -p "${_s6b}"
_exit6b=0
PATH="${_b6b}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_off_bundle}" \
  --target-root "${_s6b}" \
  > "${_work}/out6b.txt" 2>&1 || _exit6b=$?

if [ "${_exit6b}" != "0" ]; then
  printf 'FAIL: %s/6b — expected exit 0, got %d\n' "${T}" "${_exit6b}"
  cat "${_work}/out6b.txt"
  exit 1
fi
printf '  ok 6b: telemetry-off, python3 present -> exit 0\n'

# ---------------------------------------------------------------------------
# Sub-test 6c: telemetry-OFF bundle, python3+databricks+curl present, jq ABSENT
#              -> exit 0 (jq not critical without emitter)
# ---------------------------------------------------------------------------
_b6c="${_work}/bin6c"
_mk_sys_bin "${_b6c}"
printf '#!/bin/sh\nexit 0\n' > "${_b6c}/databricks" && chmod +x "${_b6c}/databricks"
printf '#!/bin/sh\nexit 0\n' > "${_b6c}/curl"        && chmod +x "${_b6c}/curl"
# NO jq
if [ -n "${_rpy}" ]; then
  ln -sf "${_rpy}" "${_b6c}/python3"
else
  printf '#!/bin/sh\nexit 0\n' > "${_b6c}/python3" && chmod +x "${_b6c}/python3"
fi

_s6c="${_work}/stage6c"
mkdir -p "${_s6c}"
_exit6c=0
PATH="${_b6c}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_off_bundle}" \
  --target-root "${_s6c}" \
  > "${_work}/out6c.txt" 2>&1 || _exit6c=$?

if [ "${_exit6c}" != "0" ]; then
  printf 'FAIL: %s/6c — expected exit 0 (jq not critical without emitter), got %d\n' \
    "${T}" "${_exit6c}"
  cat "${_work}/out6c.txt"
  exit 1
fi
printf '  ok 6c: telemetry-off, jq absent -> exit 0 (jq not critical)\n'

# ---------------------------------------------------------------------------
# Sub-test 6d: telemetry-ON bundle, python3+databricks+curl present, jq ABSENT
#              -> exit 3 (jq critical WITH emitter)
# ---------------------------------------------------------------------------
_b6d="${_work}/bin6d"
_mk_sys_bin "${_b6d}"
printf '#!/bin/sh\nexit 0\n' > "${_b6d}/databricks" && chmod +x "${_b6d}/databricks"
printf '#!/bin/sh\nexit 0\n' > "${_b6d}/curl"        && chmod +x "${_b6d}/curl"
# NO jq
if [ -n "${_rpy}" ]; then
  ln -sf "${_rpy}" "${_b6d}/python3"
else
  printf '#!/bin/sh\nexit 0\n' > "${_b6d}/python3" && chmod +x "${_b6d}/python3"
fi

_s6d="${_work}/stage6d"
mkdir -p "${_s6d}"
_exit6d=0
PATH="${_b6d}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_on_bundle}" \
  --target-root "${_s6d}" \
  > "${_work}/out6d.txt" 2>&1 || _exit6d=$?

if [ "${_exit6d}" != "3" ]; then
  printf 'FAIL: %s/6d — expected exit 3 (jq missing, bundle HAS emitter), got %d\n' \
    "${T}" "${_exit6d}"
  cat "${_work}/out6d.txt"
  exit 1
fi
if ! grep -q 'jq' "${_work}/out6d.txt"; then
  printf 'FAIL: %s/6d — output does not mention jq\n' "${T}"
  cat "${_work}/out6d.txt"
  exit 1
fi
printf '  ok 6d: telemetry-on, jq absent -> exit 3 (jq cited)\n'

# ---------------------------------------------------------------------------
# Sub-test 6e: telemetry-OFF bundle, python3 ABSENT, but DATABRICKS_BEARER set
#              -> exit 0 (python3 downgraded to informational in bearer-only mode)
# ---------------------------------------------------------------------------
_b6e="${_work}/bin6e"
_mk_sys_bin "${_b6e}"
printf '#!/bin/sh\nexit 0\n' > "${_b6e}/databricks" && chmod +x "${_b6e}/databricks"
# python3 deliberately absent (same as 6a), but bearer token is set

_s6e="${_work}/stage6e"
mkdir -p "${_s6e}"
_exit6e=0
DATABRICKS_BEARER="dummy-token" PATH="${_b6e}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_off_bundle}" \
  --target-root "${_s6e}" \
  > "${_work}/out6e.txt" 2>&1 || _exit6e=$?

if [ "${_exit6e}" != "0" ]; then
  printf 'FAIL: %s/6e — expected exit 0 (BEARER set, python3 not critical), got %d\n' \
    "${T}" "${_exit6e}"
  cat "${_work}/out6e.txt"
  exit 1
fi
printf '  ok 6e: bearer-only, no python3 -> exit 0 (python3 downgraded)\n'

printf 'PASS: %s\n' "${T}"
