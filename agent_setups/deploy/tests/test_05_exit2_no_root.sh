#!/usr/bin/env sh
# test_05_exit2_no_root.sh — running without --target-root as non-root must exit 2.
# Guard: if the runner IS root, skip this case.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
T="test_05_exit2_no_root"

# Guard: skip when running as root
if [ "$(id -u)" = "0" ]; then
  printf 'SKIP: %s — runner is root; exit-2 path cannot be exercised\n' "${T}"
  exit 0
fi

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

# Run WITHOUT --target-root (and as non-root, confirmed above).
# The root check fires before source/prereq checks, so no source needed.
_exit=0
sh "${INSTALL_SH}" --os linux --agents claude-code \
  > "${_work}/out.txt" 2>&1 || _exit=$?

if [ "${_exit}" != "2" ]; then
  printf 'FAIL: %s — expected exit 2, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt"
  exit 1
fi

printf '  ok: non-root without --target-root -> exit 2\n'
printf 'PASS: %s\n' "${T}"
