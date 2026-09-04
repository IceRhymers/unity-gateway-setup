#!/bin/sh
# test_13_ere_pattern_hygiene.sh — self-policing for a defect class.
#
# 1. No ERE grep (grep -E) in the production bootstrap scripts may use the
#    sequences \| or \s. In ERE, alternation is a bare |; a backslashed \| is a
#    literal pipe and never matches. \s is not POSIX; use [[:space:]].
# 2. No test may use grep -c, whose exit status is 1 on a zero count and so
#    misfires under set -e and in && chains. Capture the printed count instead.
#
# The scanner uses grep -F (fixed strings) for the forbidden sequences, so it
# does not trip its own rule 1.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
T="test_13_ere_pattern_hygiene"

# Production scripts only. The tests directory is excluded on purpose: test
# files carry the forbidden sequences as data (in patterns and messages).
_scripts="${BOOTSTRAP_DIR}/bootstrap-state.sh ${BOOTSTRAP_DIR}/with-state.sh ${BOOTSTRAP_DIR}/lakebase-env.sh ${BOOTSTRAP_DIR}/lib/lakebase.sh"

# 1. ERE grep lines that contain \| or \s.
# shellcheck disable=SC2086
_ere=$(grep -nE 'grep +-[A-Za-z]*E' ${_scripts} 2>/dev/null || true)
_bad=$(printf '%s\n' "${_ere}" | grep -F -e '\|' -e '\s' || true)
if [ -n "${_bad}" ]; then
  printf 'FAIL: %s — ERE grep pattern uses a backslashed pipe or \\s:\n%s\n' "${T}" "${_bad}"
  exit 1
fi

# 2. Any test that uses grep -c (this file excepted; it names it as data).
_cbad=$(grep -rnE 'grep +-[A-Za-z]*c' "${TESTS_DIR}" --include='*.sh' 2>/dev/null \
        | grep -vF 'test_13_ere_pattern_hygiene.sh' || true)
if [ -n "${_cbad}" ]; then
  printf 'FAIL: %s — a test uses grep -c (rule 6); capture the count instead:\n%s\n' "${T}" "${_cbad}"
  exit 1
fi

printf '  ok: no backslashed-pipe/\\s ERE patterns and no grep -c in tests\n'
printf 'PASS: %s\n' "${T}"
