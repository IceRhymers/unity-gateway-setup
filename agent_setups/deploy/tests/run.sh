#!/usr/bin/env sh
# run.sh — execute all install.sh test scripts and the AC-A1 matrix assertion.
# Exit non-zero if any test fails.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"

_pass=0
_fail=0

for _script in \
    "${TESTS_DIR}/test_01_print_target_dir.sh" \
    "${TESTS_DIR}/test_02_dry_run.sh" \
    "${TESTS_DIR}/test_03_staged_install.sh" \
    "${TESTS_DIR}/test_04_exit4_missing_settings.sh" \
    "${TESTS_DIR}/test_05_exit2_no_root.sh" \
    "${TESTS_DIR}/test_06_prereq_criticality.sh" \
    "${TESTS_DIR}/test_07_idempotent_upgrade.sh" \
    "${TESTS_DIR}/test_08_codex_managed_user.sh" \
    "${TESTS_DIR}/test_09_agents_validation.sh" \
    "${TESTS_DIR}/test_10_opencode_managed_user.sh" \
    "${TESTS_DIR}/test_11_opencode_local_install.sh" \
    "${TESTS_DIR}/test_12_codex_local_install.sh" \
    "${TESTS_DIR}/test_13_claude_code_local_install.sh" \
    "${TESTS_DIR}/test_14_backup_on_overwrite.sh" \
    "${TESTS_DIR}/test_15_uninstall.sh" \
    "${TESTS_DIR}/test_16_smoke_gating.sh" \
    "${TESTS_DIR}/assert_matrix_agreement.sh"; do
  [ -f "${_script}" ] || { printf 'SKIP: %s (not found)\n' "${_script}"; continue; }
  _name="$(basename -- "${_script}")"
  printf '\n=== %s ===\n' "${_name}"
  if sh "${_script}"; then
    _pass=$(( _pass + 1 ))
  else
    _fail=$(( _fail + 1 ))
  fi
done

printf '\n=== Results: %d passed, %d failed ===\n' "${_pass}" "${_fail}"

[ "${_fail}" = "0" ]
