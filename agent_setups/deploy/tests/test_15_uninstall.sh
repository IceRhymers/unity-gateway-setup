#!/usr/bin/env sh
# test_15_uninstall.sh — --uninstall lifecycle: manifest tracking, probing
# fallback, dry-run, preservation of non-managed files, and no-op re-uninstall.
#
# (1) install telemetry-on -> 3 files + marker; marker files= lists all 3.
# (2) add unrelated user-notes.txt.
# (3) --uninstall (no --source) -> exit 0, 3 files + marker gone,
#     user-notes.txt remains, dir remains, "Left...in place" notice.
# (4) re-uninstall -> "nothing placed" exit 0.
# (5) install telemetry-on, delete files= line -> probing fallback removes
#     optional files too.
# (6) fresh empty staging dir --uninstall -> warn exit 0.
# (7) --uninstall --dry-run -> [plan] rm lines, no files removed.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
# shellcheck disable=SC1091
. "${TESTS_DIR}/_fixtures.sh"
T="test_15_uninstall"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

# Prereq stubs (needed only for install phase, not for --uninstall).
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
mk_claude_bundle "${_cc_src}" on

# ---------------------------------------------------------------------------
# (1) Install telemetry-on: 3 files + marker; marker files= lists all 3
# ---------------------------------------------------------------------------
_staging="${_work}/staging1"
mkdir -p "${_staging}"
_target_dir="${_staging}/etc/claude-code"

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src}" \
  --target-root "${_staging}" \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(1) install — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

_marker="${_target_dir}/.unity-gateway-version"
for _f in managed-settings.json otel-headers-helper.sh emit_hook_events.sh; do
  if [ ! -f "${_target_dir}/${_f}" ]; then
    printf 'FAIL: %s/(1) — expected file %s after install\n' "${T}" "${_f}"
    exit 1
  fi
done
if [ ! -f "${_marker}" ]; then
  printf 'FAIL: %s/(1) — marker not found after install\n' "${T}"
  exit 1
fi

# Marker must list all 3 basenames.
_files_line="$(grep '^files=' "${_marker}" | cut -d= -f2-)"
for _f in managed-settings.json otel-headers-helper.sh emit_hook_events.sh; do
  case " ${_files_line} " in
    *" ${_f} "*) ;;
    *) printf 'FAIL: %s/(1) — files= line missing "%s"\n  files= was: %s\n' "${T}" "${_f}" "${_files_line}"; exit 1 ;;
  esac
done
printf '  ok (1): 3 files + marker placed; files= lists all 3\n'

# ---------------------------------------------------------------------------
# (2) Add an unrelated user file that uninstall must not touch
# ---------------------------------------------------------------------------
printf 'user notes\n' > "${_target_dir}/user-notes.txt"
printf '  ok (2): user-notes.txt added\n'

# ---------------------------------------------------------------------------
# (3) --uninstall (no --source): 3 placed files + marker gone; user file remains
# ---------------------------------------------------------------------------
_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --target-root "${_staging}" \
  --uninstall \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(3) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

for _f in managed-settings.json otel-headers-helper.sh emit_hook_events.sh; do
  if [ -e "${_target_dir}/${_f}" ]; then
    printf 'FAIL: %s/(3) — %s was not removed by --uninstall\n' "${T}" "${_f}"
    exit 1
  fi
done
if [ -e "${_marker}" ]; then
  printf 'FAIL: %s/(3) — marker was not removed by --uninstall\n' "${T}"
  exit 1
fi
if [ ! -f "${_target_dir}/user-notes.txt" ]; then
  printf 'FAIL: %s/(3) — user-notes.txt was removed (must be preserved)\n' "${T}"
  exit 1
fi
if [ ! -d "${_target_dir}" ]; then
  printf 'FAIL: %s/(3) — install dir was removed (must remain when non-empty)\n' "${T}"
  exit 1
fi
# Must print "Left ... in place" on stderr because user-notes.txt is still there.
if ! grep -q 'Left.*in place' "${_work}/err.txt"; then
  printf 'FAIL: %s/(3) — expected "Left...in place" notice on stderr\n' "${T}"
  printf '  stderr:\n'; cat "${_work}/err.txt"
  exit 1
fi
printf '  ok (3): --uninstall removes 3 files + marker; preserves user-notes.txt; prints notice\n'

# ---------------------------------------------------------------------------
# (4) Re-uninstall on already-clean dir: "nothing placed" warning, exit 0
# ---------------------------------------------------------------------------
_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --target-root "${_staging}" \
  --uninstall \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(4) — expected exit 0 on re-uninstall, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if ! grep -q 'Nothing placed' "${_work}/err.txt"; then
  printf 'FAIL: %s/(4) — expected "Nothing placed" warning on re-uninstall\n' "${T}"
  printf '  stderr:\n'; cat "${_work}/err.txt"
  exit 1
fi
printf '  ok (4): re-uninstall warns and exits 0\n'

# ---------------------------------------------------------------------------
# (5) Probing fallback: install, then delete files= line; uninstall must still
#     remove optional files via probing.
# ---------------------------------------------------------------------------
_staging5="${_work}/staging5"
mkdir -p "${_staging5}"
_target5="${_staging5}/etc/claude-code"

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src}" \
  --target-root "${_staging5}" \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(5) install — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

# Remove the files= line from the marker.
_marker5="${_target5}/.unity-gateway-version"
grep -v '^files=' "${_marker5}" > "${_work}/marker_tmp" && mv "${_work}/marker_tmp" "${_marker5}"

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --target-root "${_staging5}" \
  --uninstall \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(5) uninstall — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

# Probing fallback must have warned.
if ! grep -q 'probing fallback' "${_work}/err.txt"; then
  printf 'FAIL: %s/(5) — expected "probing fallback" warning on stderr\n' "${T}"
  printf '  stderr:\n'; cat "${_work}/err.txt"
  exit 1
fi

# All 3 files and the marker must be gone.
for _f in managed-settings.json otel-headers-helper.sh emit_hook_events.sh .unity-gateway-version; do
  if [ -e "${_target5}/${_f}" ]; then
    printf 'FAIL: %s/(5) — %s was not removed by probing-fallback uninstall\n' "${T}" "${_f}"
    exit 1
  fi
done
printf '  ok (5): probing fallback removes all optional files when files= line is absent\n'

# ---------------------------------------------------------------------------
# (6) Fresh empty staging dir: --uninstall warns and exits 0
# ---------------------------------------------------------------------------
_staging6="${_work}/staging6"
mkdir -p "${_staging6}"

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --target-root "${_staging6}" \
  --uninstall \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(6) — expected exit 0 on fresh staging, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if ! grep -q 'Nothing placed' "${_work}/err.txt"; then
  printf 'FAIL: %s/(6) — expected "Nothing placed" warning on fresh empty staging\n' "${T}"
  printf '  stderr:\n'; cat "${_work}/err.txt"
  exit 1
fi
printf '  ok (6): fresh empty staging --uninstall warns and exits 0\n'

# ---------------------------------------------------------------------------
# (7) --uninstall --dry-run: prints [plan] rm lines; removes nothing
# ---------------------------------------------------------------------------
_staging7="${_work}/staging7"
mkdir -p "${_staging7}"
_target7="${_staging7}/etc/claude-code"

# First install so there is something to plan-remove.
_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src}" \
  --target-root "${_staging7}" \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(7) install — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --target-root "${_staging7}" \
  --uninstall --dry-run \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(7) dry-run uninstall — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

if ! grep -q '\[plan\] rm' "${_work}/out.txt"; then
  printf 'FAIL: %s/(7) — expected "[plan] rm" in dry-run uninstall output\n' "${T}"
  cat "${_work}/out.txt"
  exit 1
fi

# Files must still exist.
if [ ! -f "${_target7}/managed-settings.json" ]; then
  printf 'FAIL: %s/(7) — managed-settings.json was removed by dry-run (must not be)\n' "${T}"
  exit 1
fi
if [ ! -f "${_target7}/.unity-gateway-version" ]; then
  printf 'FAIL: %s/(7) — marker was removed by dry-run (must not be)\n' "${T}"
  exit 1
fi
printf '  ok (7): --uninstall --dry-run prints [plan] rm; removes nothing\n'

printf 'PASS: %s\n' "${T}"
