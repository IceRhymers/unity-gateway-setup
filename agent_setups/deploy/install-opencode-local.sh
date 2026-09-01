#!/usr/bin/env sh
# install-opencode-local.sh — per-user placement for a LOCAL (non-managed) opencode install.
#
# The system install.sh is the root/managed placement authority. It deliberately
# SKIPS user-mode opencode configs, because a per-user file is not a root-managed
# system placement. This script is its complement: it copies a generated
# user-mode opencode.json into the per-user opencode config dir, so a developer
# can route a local opencode through the gateway without root or MDM.
#
# Target (opencode's per-user global config dir):
#   ${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json
#
# Generate the source first (user mode):
#   make agent-opencode ARGS=--user-config           # -> agent_setups/generated/opencode/opencode.json
# or run this through the one-step target:
#   make opencode-install-local
#
# Usage:
#   install-opencode-local.sh [OPTIONS]
#
# Options:
#   --source <file>     Generated user-mode opencode.json
#                       (default: <repo>/agent_setups/generated/opencode/opencode.json)
#   --target-dir <dir>  opencode config dir
#                       (default: ${XDG_CONFIG_HOME:-$HOME/.config}/opencode)
#   --dry-run           Print planned actions, touch nothing (exit 0)
#   --no-backup         Overwrite an existing opencode.json without a timestamped backup
#   --print-target      Print the resolved target file path and exit 0
#   -h, --help          Show this message
#
# Exit codes:
#   0  success / --dry-run / --print-target
#   1  usage error
#   4  source opencode.json not found
#   5  copy or permission failure
set -eu

# ---------------------------------------------------------------------------
# Resolve the repo-relative default source (this file is agent_setups/deploy/…)
# ---------------------------------------------------------------------------
_self_dir="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_SOURCE="${_self_dir}/../generated/opencode/opencode.json"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SOURCE="${DEFAULT_SOURCE}"
TARGET_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode"
DRY_RUN=0
NO_BACKUP=0
PRINT_TARGET=0

# ---------------------------------------------------------------------------
# Logging helpers (match install.sh)
# ---------------------------------------------------------------------------
_info() { printf '[opencode-local] %s\n' "$*"; }
_warn() { printf '[opencode-local] WARN: %s\n' "$*" >&2; }
_fatal() {
  _f_code="$1"; shift
  printf '[opencode-local] FATAL: %s\n' "$*" >&2
  exit "${_f_code}"
}

_usage() {
  cat <<'EOF'
Usage: install-opencode-local.sh [OPTIONS]

Per-user placement for a LOCAL (non-managed) opencode install. Copies a generated
user-mode opencode.json into the per-user opencode config dir. No root, no MDM.

Options:
  --source <file>     Generated user-mode opencode.json
                      (default: <repo>/agent_setups/generated/opencode/opencode.json)
  --target-dir <dir>  opencode config dir (default: ${XDG_CONFIG_HOME:-$HOME/.config}/opencode)
  --dry-run           Print planned actions, touch nothing (exit 0)
  --no-backup         Overwrite an existing opencode.json without a timestamped backup
  --print-target      Print the resolved target file path and exit 0
  -h, --help          Show this message

Exit codes:
  0  success / --dry-run / --print-target
  1  usage error
  4  source opencode.json not found
  5  copy/permission failure
EOF
  exit 1
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --source)        shift; SOURCE="${1:?--source requires a value}" ;;
    --target-dir)    shift; TARGET_DIR="${1:?--target-dir requires a value}" ;;
    --dry-run)       DRY_RUN=1 ;;
    --no-backup)     NO_BACKUP=1 ;;
    --print-target)  PRINT_TARGET=1 ;;
    -h|--help)       _usage ;;
    *)               _warn "Unknown option: $1"; _usage ;;
  esac
  shift
done

TARGET="${TARGET_DIR}/opencode.json"

# ---------------------------------------------------------------------------
# --print-target
# ---------------------------------------------------------------------------
if [ "${PRINT_TARGET}" = "1" ]; then
  printf '%s\n' "${TARGET}"
  exit 0
fi

# ---------------------------------------------------------------------------
# Source must exist (exit 4)
# ---------------------------------------------------------------------------
if [ ! -f "${SOURCE}" ]; then
  _fatal 4 "Source opencode.json not found: ${SOURCE}
  Generate it first: make agent-opencode ARGS=--user-config
  (or run the one-step target: make opencode-install-local)"
fi

# A user-mode config has no .mobileconfig beside it. If one IS present, the source
# is a managed bundle; the JSON is identical, so placement is still valid, but the
# managed dir / MDM profile would override this per-user file. Warn, do not block.
if [ -f "$(dirname "${SOURCE}")/ai.opencode.managed.mobileconfig" ]; then
  _warn "Source dir also holds ai.opencode.managed.mobileconfig (a managed bundle)."
  _warn "  A deployed managed config would override this per-user file. For a pure"
  _warn "  local install, regenerate in user mode: make agent-opencode ARGS=--user-config"
fi

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
_info "=== opencode local install ==="
_info "  source : ${SOURCE}"
_info "  target : ${TARGET}"
if [ "${DRY_RUN}" = "1" ]; then
  _info "  (DRY-RUN MODE -- no files will be written)"
fi

# Back up an existing config unless suppressed.
if [ -f "${TARGET}" ] && [ "${NO_BACKUP}" = "0" ]; then
  _backup="${TARGET}.bak-$(date -u '+%Y%m%dT%H%M%SZ' 2>/dev/null || date -u | tr ' :' '__')"
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] backup existing \"${TARGET}\" -> \"${_backup}\""
  else
    cp -- "${TARGET}" "${_backup}" || _fatal 5 "backup failed: ${TARGET} -> ${_backup}"
    _info "  backed up existing config -> ${_backup}"
  fi
fi

# The auth plugin sits beside the source opencode.json. opencode.json references
# it by a relative path, so it must land beside opencode.json in the target dir.
_plugin_src="$(dirname "${SOURCE}")/databricks-auth.ts"
_plugin_target="${TARGET_DIR}/databricks-auth.ts"

if [ "${DRY_RUN}" = "1" ]; then
  _info "  [plan] mkdir -p \"${TARGET_DIR}\""
  _info "  [plan] copy   \"${SOURCE}\" -> \"${TARGET}\""
  _info "  [plan] chmod 644 \"${TARGET}\""
  if [ -f "${_plugin_src}" ]; then
    _info "  [plan] copy   \"${_plugin_src}\" -> \"${_plugin_target}\""
    _info "  [plan] chmod 644 \"${_plugin_target}\""
  fi
else
  mkdir -p -- "${TARGET_DIR}" || _fatal 5 "mkdir failed: ${TARGET_DIR}"
  cp -- "${SOURCE}" "${TARGET}" || _fatal 5 "copy failed: ${SOURCE} -> ${TARGET}"
  chmod 644 "${TARGET}" || _fatal 5 "chmod 644 failed: ${TARGET}"
  _info "  installed opencode.json"
  if [ -f "${_plugin_src}" ]; then
    cp -- "${_plugin_src}" "${_plugin_target}" || _fatal 5 "copy failed: ${_plugin_src} -> ${_plugin_target}"
    chmod 644 "${_plugin_target}" || _fatal 5 "chmod 644 failed: ${_plugin_target}"
    _info "  installed databricks-auth.ts"
  else
    _warn "databricks-auth.ts not found beside the source; opencode auth will fail without it."
    _warn "  Re-generate: make agent-opencode ARGS=--user-config"
  fi
fi

# ---------------------------------------------------------------------------
# Auth reminder (the plugin mints tokens; the developer logs in once)
# ---------------------------------------------------------------------------
printf '\nLocal install complete. The databricks-auth.ts plugin mints a fresh\n'
printf 'Databricks token on every request, so no environment variable is needed.\n'
printf 'The Databricks CLI refreshes access tokens silently, so routine expiry\n'
printf 'needs no login. Authenticate once (the plugin also auto-runs this if needed):\n'
printf '  databricks auth login --host <host> --profile <profile>\n\n'

exit 0
