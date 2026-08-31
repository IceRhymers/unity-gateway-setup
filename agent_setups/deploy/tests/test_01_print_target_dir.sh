#!/usr/bin/env sh
# test_01_print_target_dir.sh — verify --print-target-dir matrix for all OS/agent combos.
# Covers AC-A1 mechanism input: the path install.sh would write into.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
T="test_01_print_target_dir"

_assert_ptd() {
  # $1=os  $2=agent  $3=expected_raw  $4=target_root_prefix (may be empty)
  _ap_os="$1"
  _ap_agent="$2"
  _ap_exp_raw="$3"
  _ap_troot="$4"

  if [ -n "${_ap_troot}" ]; then
    _ap_got=$(sh "${INSTALL_SH}" \
      --os "${_ap_os}" --agent "${_ap_agent}" \
      --target-root "${_ap_troot}" --print-target-dir)
    _ap_exp="${_ap_troot}${_ap_exp_raw}"
  else
    _ap_got=$(sh "${INSTALL_SH}" \
      --os "${_ap_os}" --agent "${_ap_agent}" --print-target-dir)
    _ap_exp="${_ap_exp_raw}"
  fi

  if [ "${_ap_got}" != "${_ap_exp}" ]; then
    printf 'FAIL: %s — os=%s agent=%s got="%s" expected="%s"\n' \
      "${T}" "${_ap_os}" "${_ap_agent}" "${_ap_got}" "${_ap_exp}"
    exit 1
  fi
  printf '  ok: --os %-6s --agent %-12s -> "%s"\n' \
    "${_ap_os}" "${_ap_agent}" "${_ap_got}"
}

# Base matrix (no --target-root)
_assert_ptd linux  claude-code "/etc/claude-code"                          ""
_assert_ptd linux  codex       "/etc/codex"                                ""
_assert_ptd macos  claude-code "/Library/Application Support/ClaudeCode"   ""
_assert_ptd macos  codex       "/etc/codex"                                ""

# With --target-root prefix (must prepend exactly)
_assert_ptd linux  claude-code "/etc/claude-code"                          "/staging/pfx"
_assert_ptd macos  claude-code "/Library/Application Support/ClaudeCode"   "/staging/pfx2"

printf 'PASS: %s\n' "${T}"
