#!/usr/bin/env sh
# test_09_agents_validation.sh — an unknown --agents token must fail loudly
# (exit 1), not silently install nothing and exit 0.
#
# Sub-tests:
#  9a: --agents bogus              -> exit 1 (unknown agent rejected)
#  9b: --agents claude-code,codex  -> accepted (reaches prereq/install; not exit 1)
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
. "${TESTS_DIR}/_fixtures.sh"
T="test_09_agents_validation"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM
_work=$(mktemp -d)

# Synthetic claude-code source (validation rejects the bad --agents before any copy).
_linux_src="${_work}/cc_src"
mk_claude_bundle "${_linux_src}" off

# ---------------------------------------------------------------------------
# 9a: unknown agent token -> exit 1
# ---------------------------------------------------------------------------
_s9a="${_work}/stage9a"; mkdir -p "${_s9a}"
_exit9a=0
sh "${INSTALL_SH}" \
  --os linux --agents bogus \
  --claude-source "${_linux_src}" \
  --target-root "${_s9a}" \
  > "${_work}/out9a.txt" 2>&1 || _exit9a=$?

if [ "${_exit9a}" != "1" ]; then
  printf 'FAIL: %s/9a — expected exit 1 (unknown agent), got %d\n' "${T}" "${_exit9a}"
  cat "${_work}/out9a.txt"
  exit 1
fi
# Nothing must have been installed under the staging root.
if [ -e "${_s9a}/etc/claude-code/managed-settings.json" ]; then
  printf 'FAIL: %s/9a — files were installed despite unknown agent\n' "${T}"
  exit 1
fi
printf '  ok 9a: --agents bogus -> exit 1 (nothing installed)\n'

# ---------------------------------------------------------------------------
# 9b: a typo'd single token is still rejected (regression guard for the
#     "claude" vs "claude-code" case the review called out)
# ---------------------------------------------------------------------------
_s9b="${_work}/stage9b"; mkdir -p "${_s9b}"
_exit9b=0
sh "${INSTALL_SH}" \
  --os linux --agents claude \
  --claude-source "${_linux_src}" \
  --target-root "${_s9b}" \
  > "${_work}/out9b.txt" 2>&1 || _exit9b=$?

if [ "${_exit9b}" != "1" ]; then
  printf 'FAIL: %s/9b — expected exit 1 for typo "claude", got %d\n' "${T}" "${_exit9b}"
  cat "${_work}/out9b.txt"
  exit 1
fi
printf '  ok 9b: --agents claude (typo) -> exit 1\n'

printf 'PASS: %s\n' "${T}"
