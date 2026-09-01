#!/usr/bin/env sh
# install-codex-local.sh — per-user placement for a LOCAL (non-managed) Codex install.
#
# The system install.sh is the root/managed placement authority: it writes a
# root-owned /etc/codex/managed_config.toml that OVERRIDES each user's config. It
# deliberately SKIPS a user-mode Codex bundle, because a per-user file is not a
# root-managed system placement. This script is its complement: it copies a
# generated user-mode config.toml into the per-user $CODEX_HOME, so a developer
# can route a local Codex through the gateway without root or MDM.
#
# Target (Codex's per-user config dir):
#   ${CODEX_HOME:-$HOME/.codex}/config.toml
#
# Generate the source first (user mode):
#   make agent-codex ARGS=--user-config       # -> agent_setups/generated/codex/config.toml
# or run this through the one-step target:
#   make codex-install-local
#
# Usage:
#   install-codex-local.sh [OPTIONS]
#
# Options:
#   --source <file>     Generated user-mode config.toml
#                       (default: <repo>/agent_setups/generated/codex/config.toml)
#   --target-dir <dir>  Codex config dir (default: ${CODEX_HOME:-$HOME/.codex})
#   --dry-run           Print planned actions, touch nothing (exit 0)
#   --no-backup         Overwrite an existing config.toml without a timestamped backup
#   --print-target      Print the resolved target file path and exit 0
#   -h, --help          Show this message
#
# Exit codes:
#   0  success / --dry-run / --print-target
#   1  usage error
#   4  source config.toml not found
#   5  copy or permission failure
set -eu

# ---------------------------------------------------------------------------
# Resolve the repo-relative default source (this file is agent_setups/deploy/…)
# ---------------------------------------------------------------------------
_self_dir="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_SOURCE="${_self_dir}/../generated/codex/config.toml"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SOURCE="${DEFAULT_SOURCE}"
TARGET_DIR="${CODEX_HOME:-${HOME}/.codex}"
DRY_RUN=0
NO_BACKUP=0
PRINT_TARGET=0

# The hook files that ship beside config.toml in a user-mode bundle. They exist
# only when hook telemetry is enabled. hooks.json gets mode 644; the emitter 755.
HELPER_644="hooks.json"
HELPER_755="emit_hook_events.sh"

# ---------------------------------------------------------------------------
# Logging helpers (match install.sh)
# ---------------------------------------------------------------------------
_info() { printf '[codex-local] %s\n' "$*"; }
_warn() { printf '[codex-local] WARN: %s\n' "$*" >&2; }
_fatal() {
  _f_code="$1"; shift
  printf '[codex-local] FATAL: %s\n' "$*" >&2
  exit "${_f_code}"
}

_usage() {
  cat <<'EOF'
Usage: install-codex-local.sh [OPTIONS]

Per-user placement for a LOCAL (non-managed) Codex install. Copies a generated
user-mode config.toml (and any hook files) into the per-user $CODEX_HOME. No
root, no MDM.

Options:
  --source <file>     Generated user-mode config.toml
                      (default: <repo>/agent_setups/generated/codex/config.toml)
  --target-dir <dir>  Codex config dir (default: ${CODEX_HOME:-$HOME/.codex})
  --dry-run           Print planned actions, touch nothing (exit 0)
  --no-backup         Overwrite an existing config.toml without a timestamped backup
  --print-target      Print the resolved target file path and exit 0
  -h, --help          Show this message

Exit codes:
  0  success / --dry-run / --print-target
  1  usage error
  4  source config.toml not found
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

TARGET="${TARGET_DIR}/config.toml"

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
  _fatal 4 "Source config.toml not found: ${SOURCE}
  Generate it first: make agent-codex ARGS=--user-config
  (or run the one-step target: make codex-install-local)"
fi

# A user-mode bundle has config.toml at its root. A managed bundle instead has
# etc/managed_config.toml. If a managed bundle sits beside the source, the managed
# /etc/codex config would override this per-user file at runtime.
if [ -f "$(dirname "${SOURCE}")/etc/managed_config.toml" ]; then
  _warn "Source dir also holds etc/managed_config.toml (a managed bundle)."
  _warn "  A deployed managed /etc/codex config would override this per-user file."
  _warn "  For a pure local install, regenerate in user mode:"
  _warn "  make agent-codex ARGS=--user-config"
fi

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
_info "=== Codex local install ==="
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

_src_dir="$(dirname "${SOURCE}")"

if [ "${DRY_RUN}" = "1" ]; then
  _info "  [plan] mkdir -p \"${TARGET_DIR}\""
  _info "  [plan] copy   \"${SOURCE}\" -> \"${TARGET}\""
  _info "  [plan] chmod 644 \"${TARGET}\""
  if [ -f "${_src_dir}/${HELPER_644}" ]; then
    _info "  [plan] copy   \"${_src_dir}/${HELPER_644}\" -> \"${TARGET_DIR}/${HELPER_644}\""
    _info "  [plan] chmod 644 \"${TARGET_DIR}/${HELPER_644}\""
  fi
  if [ -f "${_src_dir}/${HELPER_755}" ]; then
    _info "  [plan] copy   \"${_src_dir}/${HELPER_755}\" -> \"${TARGET_DIR}/${HELPER_755}\""
    _info "  [plan] chmod 755 \"${TARGET_DIR}/${HELPER_755}\""
  fi
else
  mkdir -p -- "${TARGET_DIR}" || _fatal 5 "mkdir failed: ${TARGET_DIR}"
  cp -- "${SOURCE}" "${TARGET}" || _fatal 5 "copy failed: ${SOURCE} -> ${TARGET}"
  chmod 644 "${TARGET}" || _fatal 5 "chmod 644 failed: ${TARGET}"
  _info "  installed config.toml"
  # hooks.json references the emitter; both must sit in $CODEX_HOME beside config.toml.
  if [ -f "${_src_dir}/${HELPER_644}" ]; then
    cp -- "${_src_dir}/${HELPER_644}" "${TARGET_DIR}/${HELPER_644}" || _fatal 5 "copy failed: ${_src_dir}/${HELPER_644} -> ${TARGET_DIR}/${HELPER_644}"
    chmod 644 "${TARGET_DIR}/${HELPER_644}" || _fatal 5 "chmod 644 failed: ${TARGET_DIR}/${HELPER_644}"
    _info "  installed ${HELPER_644}"
  fi
  if [ -f "${_src_dir}/${HELPER_755}" ]; then
    cp -- "${_src_dir}/${HELPER_755}" "${TARGET_DIR}/${HELPER_755}" || _fatal 5 "copy failed: ${_src_dir}/${HELPER_755} -> ${TARGET_DIR}/${HELPER_755}"
    chmod 755 "${TARGET_DIR}/${HELPER_755}" || _fatal 5 "chmod 755 failed: ${TARGET_DIR}/${HELPER_755}"
    _info "  installed ${HELPER_755}"
  fi
fi

# ---------------------------------------------------------------------------
# Auth reminder (the auth command mints tokens; the developer logs in once)
# ---------------------------------------------------------------------------
printf '\nLocal install complete. The auth command in config.toml mints a fresh\n'
printf 'Databricks token per request, so no environment variable is needed.\n'
printf 'The Databricks CLI refreshes access tokens silently, so routine expiry\n'
printf 'needs no login. Authenticate once:\n'
printf '  databricks auth login --host <host> --profile <profile>\n'
if [ -f "${_src_dir}/${HELPER_755}" ]; then
  printf 'Codex user hooks require per-user trust. Trust them in Codex, or launch\n'
  printf 'with --dangerously-bypass-hook-trust, for the reporting hooks to run.\n'
fi
printf 'Verify with: codex doctor\n\n'

exit 0
