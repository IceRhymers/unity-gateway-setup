#!/usr/bin/env sh
# assert_matrix_agreement.sh — AC-A1: generator<->installer agreement for both agents.
#
# For each OS (linux, macos) and each agent (claude-code, codex):
#   - Parse the fixture to extract the directory each script is baked into.
#   - Compare with: install.sh --os <os> --agent <agent> --print-target-dir
#   - Fail loudly on mismatch.
#
# Fixture source: existing repo fixtures under agent_setups/generated/ (telemetry+hooks ON).
#   Claude:  generated/claude-code/{linux,macos}/managed-settings.json
#             -> otelHeadersHelper path + first emit_hook_events hook command
#   Codex:   generated/container/codex/etc/managed_config.toml
#             -> first hooks command containing emit_hook_events.sh
#
# Claude parsing: strip surrounding \" and trailing subcommand before dirname.
# Codex  parsing: strip trailing subcommand before dirname.
# No whitespace-split: dirname works correctly on space-containing macOS path.
#
# We use the EXISTING repo fixtures (telemetry-on, both agents, both OSes).
# Regeneration was not required: the fixtures already contain otelHeadersHelper
# and emit_hook_events.sh references, satisfying the non-vacuous-pass requirement.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
GENERATED_DIR="${TESTS_DIR}/../../generated"
T="assert_matrix_agreement"

CC_LINUX_JSON="${GENERATED_DIR}/claude-code/linux/managed-settings.json"
CC_MACOS_JSON="${GENERATED_DIR}/claude-code/macos/managed-settings.json"
CX_MANAGED_TOML="${GENERATED_DIR}/container/codex/etc/managed_config.toml"

# Verify fixtures exist and are telemetry-ON (contain emit_hook_events references)
for _f in "${CC_LINUX_JSON}" "${CC_MACOS_JSON}" "${CX_MANAGED_TOML}"; do
  if [ ! -f "${_f}" ]; then
    printf 'FAIL: %s — fixture not found: %s\n' "${T}" "${_f}"
    exit 1
  fi
done
if ! grep -q 'emit_hook_events' "${CC_LINUX_JSON}"; then
  printf 'FAIL: %s — linux fixture is telemetry-OFF (vacuous pass); regenerate with --telemetry on\n' "${T}"
  exit 1
fi
if ! grep -q 'emit_hook_events' "${CC_MACOS_JSON}"; then
  printf 'FAIL: %s — macos fixture is telemetry-OFF (vacuous pass); regenerate with --telemetry on\n' "${T}"
  exit 1
fi
if ! grep -q 'emit_hook_events' "${CX_MANAGED_TOML}"; then
  printf 'FAIL: %s — codex fixture has no emit_hook_events hooks; regenerate with --hook-telemetry on\n' "${T}"
  exit 1
fi

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

# Extract dirname of otelHeadersHelper path from managed-settings.json.
# Line format:  "otelHeadersHelper": "/path/to/otel-headers-helper.sh",
_otel_dir() {
  _od_path=$(grep '"otelHeadersHelper"' "$1" \
    | sed 's/.*"otelHeadersHelper": *"\(.*\)".*/\1/')
  dirname "${_od_path}"
}

# Extract dirname of first emit_hook_events.sh hook command from managed-settings.json.
# Raw JSON line:  "command": "\"/path/to/emit_hook_events.sh\" subcmd"
# Step 1: strip up to and including the opening \"  -> /path/...sh\" subcmd"
# Step 2: strip from the closing \" onwards          -> /path/...sh
# No whitespace-split; dirname handles spaces in macOS path correctly.
_hook_dir() {
  _hd_path=$(grep '"command".*emit_hook_events' "$1" | head -1 \
    | sed 's/.*"command": *"\\"//' \
    | sed 's/\\".*//')
  dirname "${_hd_path}"
}

# Extract dirname of first emit_hook_events.sh command from managed_config.toml.
# TOML line:  command = "/etc/codex/emit_hook_events.sh subcmd"
# Use 'command = "' as grep anchor to skip comment lines that also mention the filename.
# Step 1: extract quoted string content
# Step 2: strip trailing space + subcommand
_codex_hook_dir() {
  _chd_raw=$(grep 'command = ".*emit_hook_events' "$1" | head -1 \
    | sed 's/.*command = "\([^"]*\)".*/\1/')
  _chd_script=$(printf '%s' "${_chd_raw}" | sed 's/ .*//')
  dirname "${_chd_script}"
}

# ---------------------------------------------------------------------------
# Assert helper
# ---------------------------------------------------------------------------
_assert_eq() {
  # $1=label  $2=actual  $3=expected
  if [ "$2" != "$3" ]; then
    printf 'FAIL: %s\n  case    : %s\n  actual  : "%s"\n  expected: "%s"\n' \
      "${T}" "$1" "$2" "$3"
    exit 1
  fi
  printf '  ok: %-48s -> "%s"\n' "$1" "$2"
}

# ---------------------------------------------------------------------------
# Linux claude-code
# ---------------------------------------------------------------------------
_ptd_linux_cc=$(sh "${INSTALL_SH}" --os linux --agent claude-code --print-target-dir)

_otel_linux_cc=$(_otel_dir "${CC_LINUX_JSON}")
_hook_linux_cc=$(_hook_dir "${CC_LINUX_JSON}")

_assert_eq "linux/claude-code otelHeadersHelper dir" "${_otel_linux_cc}" "${_ptd_linux_cc}"
_assert_eq "linux/claude-code hook cmd dir"           "${_hook_linux_cc}" "${_ptd_linux_cc}"

# ---------------------------------------------------------------------------
# macOS claude-code  (path contains a space — must not be whitespace-split)
# ---------------------------------------------------------------------------
_ptd_macos_cc=$(sh "${INSTALL_SH}" --os macos --agent claude-code --print-target-dir)

_otel_macos_cc=$(_otel_dir "${CC_MACOS_JSON}")
_hook_macos_cc=$(_hook_dir "${CC_MACOS_JSON}")

_assert_eq "macos/claude-code otelHeadersHelper dir" "${_otel_macos_cc}" "${_ptd_macos_cc}"
_assert_eq "macos/claude-code hook cmd dir"           "${_hook_macos_cc}" "${_ptd_macos_cc}"

# ---------------------------------------------------------------------------
# Codex — both OSes (same /etc/codex on both)
# ---------------------------------------------------------------------------
_ptd_linux_cx=$(sh "${INSTALL_SH}" --os linux --agent codex --print-target-dir)
_ptd_macos_cx=$(sh "${INSTALL_SH}" --os macos --agent codex --print-target-dir)

_hook_codex=$(_codex_hook_dir "${CX_MANAGED_TOML}")

_assert_eq "linux/codex hook cmd dir"  "${_hook_codex}" "${_ptd_linux_cx}"
_assert_eq "macos/codex hook cmd dir"  "${_hook_codex}" "${_ptd_macos_cx}"

printf 'PASS: %s\n' "${T}"
