#!/bin/sh
# test_12_wrapper_executable.sh — with-state.sh must be executable and carry a
# shebang. The Makefile invokes it directly (TF_WRAP), so a missing +x bit
# breaks every make tf-* target in a fresh clone.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
WRAPPER="${BOOTSTRAP_DIR}/with-state.sh"
T="test_12_wrapper_executable"

if [ ! -f "${WRAPPER}" ]; then
  printf 'FAIL: %s — with-state.sh not found: %s\n' "${T}" "${WRAPPER}"
  exit 1
fi

if [ ! -x "${WRAPPER}" ]; then
  printf 'FAIL: %s — with-state.sh is not executable; make tf-* would break in a fresh clone\n' "${T}"
  exit 1
fi

_first=$(head -n 1 "${WRAPPER}")
case "${_first}" in
  '#!'*) ;;
  *)
    printf 'FAIL: %s — with-state.sh has no shebang (first line: %s)\n' "${T}" "${_first}"
    exit 1 ;;
esac

printf '  ok: with-state.sh is executable and has a shebang\n'
printf 'PASS: %s\n' "${T}"
