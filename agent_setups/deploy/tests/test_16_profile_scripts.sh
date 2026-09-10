#!/usr/bin/env sh
# test_16_profile_scripts.sh — the profile preflight scripts and their Makefile wiring.
#
# The regression this pins: `make login-profile` passed --host "$(HOST)" unconditionally,
# so an empty HOST became `--host ""`. The scripts used ${1:?...}, which errors on an
# EMPTY value as well as an unset one, so the target died inside argument parsing:
#
#   login-profile.sh: line 49: 1: --host requires a value
#
# That made the interactive prompt unreachable through the one command an operator runs.
# Two properties keep it fixed, and both are checked here.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(cd "${TESTS_DIR}/../../.." && pwd)"
LOGIN_SH="${TESTS_DIR}/../login-profile.sh"
ENSURE_SH="${TESTS_DIR}/../ensure-profile.sh"
T="test_16_profile_scripts"
_fail=0

_check() {
  # _check <description> <expected-exit> <command...>
  _desc="$1"; _want="$2"; shift 2
  _got=0
  "$@" >/dev/null 2>&1 </dev/null || _got=$?
  if [ "${_got}" = "${_want}" ]; then
    printf '  ok: %s\n' "${_desc}"
  else
    printf 'FAIL: %s — expected exit %s, got %s\n' "${_desc}" "${_want}" "${_got}"
    _fail=1
  fi
}

# --- 1. Makefile wiring: an empty HOST must not become `--host ""` ---------------
# `make -n` needs no credentials, so this half is hermetic.
_recipe="$(cd "${REPO_ROOT}" && make -n login-profile 2>/dev/null || printf '')"
case "${_recipe}" in
  *'--host'*)
    printf 'FAIL: %s — make login-profile passes --host with an empty HOST:\n  %s\n' "${T}" "${_recipe}"
    _fail=1 ;;
  '')
    printf 'FAIL: %s — make -n login-profile produced no recipe\n' "${T}"
    _fail=1 ;;
  *)
    printf '  ok: an empty HOST omits --host entirely\n' ;;
esac

# The flag must still appear when HOST IS set, or the target silently ignores it.
_recipe_h="$(cd "${REPO_ROOT}" && make -n login-profile HOST=https://ws.example.com 2>/dev/null || printf '')"
case "${_recipe_h}" in
  *'--host "https://ws.example.com"'*)
    printf '  ok: a set HOST is passed through as --host\n' ;;
  *)
    printf 'FAIL: %s — HOST was set but --host is missing:\n  %s\n' "${T}" "${_recipe_h}"
    _fail=1 ;;
esac

# --- 2. Script argument handling ------------------------------------------------
# These reach the CLI/jq prerequisite check first, so skip when either is absent.
if ! command -v databricks >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
  printf 'SKIP: %s — databricks CLI or jq absent; script-level cases not exercised\n' "${T}"
  [ "${_fail}" = "0" ] || exit 1
  printf 'PASS: %s (Makefile wiring only)\n' "${T}"
  exit 0
fi

# An EMPTY --host must fall through to the usage error (exit 1), never abort inside
# argument parsing. stdin is closed by _check, so the prompt is correctly skipped.
_check 'login-profile: empty --host gives the usage error' 1 \
  sh "${LOGIN_SH}" --profile ai_dev_tools_test_absent --host '' --dry-run
_check 'ensure-profile: empty --host still checks the profile' 4 \
  sh "${ENSURE_SH}" --profile ai_dev_tools_test_absent --host ''

# A missing host with stdin closed must fail, not block on a read nobody answers.
_check 'login-profile: no host and no tty fails instead of hanging' 1 \
  sh "${LOGIN_SH}" --profile ai_dev_tools_test_absent --dry-run

# --host with nothing after it is still an error: that value is genuinely unset.
_check 'login-profile: --host with no value is an error' 1 \
  sh "${LOGIN_SH}" --profile ai_dev_tools_test_absent --host

# Host shape is validated before anything is written.
_check 'login-profile: host without a scheme is refused' 1 \
  sh "${LOGIN_SH}" --profile ai_dev_tools_test_absent --host ws.example.com --dry-run
_check 'login-profile: host carrying shell syntax is refused' 1 \
  sh "${LOGIN_SH}" --profile ai_dev_tools_test_absent --host 'https://x.example.com;id' --dry-run

[ "${_fail}" = "0" ] || exit 1
printf 'PASS: %s\n' "${T}"
