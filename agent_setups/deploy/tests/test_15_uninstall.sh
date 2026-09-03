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
# (8) upgrade orphan fix: install telemetry-on, re-install telemetry-off, then
#     --uninstall -> emit_hook_events.sh is unioned into marker and removed
#     (not orphaned); dir ends empty.
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

# ---------------------------------------------------------------------------
# (8) Upgrade orphan fix: install telemetry-ON, re-install telemetry-OFF,
#     --uninstall -> emit_hook_events.sh (and otel-headers-helper.sh) are
#     unioned into marker and removed; dir ends empty.
# ---------------------------------------------------------------------------
_staging8="${_work}/staging8"
mkdir -p "${_staging8}"
_target8="${_staging8}/etc/claude-code"

_cc_src8on="${_work}/cc_src8on"
mk_claude_bundle "${_cc_src8on}" on

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src8on}" \
  --target-root "${_staging8}" \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(8) install-on — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

# Re-install telemetry-OFF: source has only managed-settings.json, no optional files.
_cc_src8off="${_work}/cc_src8off"
mk_claude_bundle "${_cc_src8off}" off

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src8off}" \
  --target-root "${_staging8}" \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(8) install-off — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

# Marker must still list emit_hook_events.sh (union from first install).
_marker8="${_target8}/.unity-gateway-version"
_files8="$(grep '^files=' "${_marker8}" | cut -d= -f2-)"
for _f in otel-headers-helper.sh emit_hook_events.sh; do
  case " ${_files8} " in
    *" ${_f} "*) ;;
    *) printf 'FAIL: %s/(8a) — "%s" missing from files= after telemetry-OFF re-install\n' \
         "${T}" "${_f}"
       printf '  files= was: %s\n' "${_files8}"
       exit 1 ;;
  esac
done
printf '  ok (8a): marker files= lists optional files after telemetry-OFF re-install (union)\n'

# --uninstall must remove all 3 placed files (including emit_hook_events.sh, otel-headers-helper.sh).
_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --target-root "${_staging8}" \
  --uninstall \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?
if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(8b) uninstall — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

for _f in managed-settings.json otel-headers-helper.sh emit_hook_events.sh .unity-gateway-version; do
  if [ -e "${_target8}/${_f}" ]; then
    printf 'FAIL: %s/(8b) — "%s" not removed by --uninstall (orphaned)\n' "${T}" "${_f}"
    exit 1
  fi
done

# No user files were added; dir must be removed by rmdir.
if [ -d "${_target8}" ]; then
  printf 'FAIL: %s/(8b) — target dir still exists (expected removal when empty)\n' "${T}"
  ls -la "${_target8}" || true
  exit 1
fi
printf '  ok (8b): --uninstall removes emit_hook_events.sh + otel-headers-helper.sh; dir removed\n'

printf 'PASS: %s\n' "${T}"
