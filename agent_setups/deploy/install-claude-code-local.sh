#!/usr/bin/env sh
# install-claude-code-local.sh — per-user placement for a LOCAL (non-managed) Claude Code install.
#
# The system install.sh is the root/managed placement authority: it writes a
# root-owned managed-settings.json to an OS system path (MDM-enforced). This
# script is its complement: it copies a generated user-mode settings.json into the
# per-user Claude Code config dir, so a developer can route a local Claude Code
# through the gateway without root or MDM.
#
# Target (Claude Code's per-user config dir):
#   $HOME/.claude/settings.json
#
# Generate the source first (user mode):
#   make agent-claude-code ARGS=--user-config      # -> agent_setups/generated/claude-code/user/settings.json
# or run this through the one-step target:
#   make claude-code-install-local
#
# Usage:
#   install-claude-code-local.sh [OPTIONS]
#
# Options:
#   --source <file>     Generated user-mode settings.json
#                       (default: <repo>/agent_setups/generated/claude-code/user/settings.json)
#   --target-dir <dir>  Claude Code config dir (default: $HOME/.claude)
#   --dry-run           Print planned actions, touch nothing (exit 0)
#   --no-backup         Overwrite an existing settings.json without a timestamped backup
#   --print-target      Print the resolved target file path and exit 0
#   -h, --help          Show this message
#
# Exit codes:
#   0  success / --dry-run / --print-target
#   1  usage error
#   4  source settings.json not found
#   5  copy or permission failure
set -eu

# ---------------------------------------------------------------------------
# Resolve the repo-relative default source (this file is agent_setups/deploy/…)
# ---------------------------------------------------------------------------
_self_dir="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_SOURCE="${_self_dir}/../generated/claude-code/user/settings.json"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SOURCE="${DEFAULT_SOURCE}"
TARGET_DIR="${HOME}/.claude"
DRY_RUN=0
NO_BACKUP=0
PRINT_TARGET=0

# The helper scripts that ship beside settings.json in a user-mode bundle. Each
# lands beside settings.json (settings.json references them by absolute path) and
# gets mode 755. They are optional: they exist only when telemetry is enabled.
HELPERS="otel-headers-helper.sh emit_hook_events.sh"

# ---------------------------------------------------------------------------
# Logging helpers (match install.sh)
# ---------------------------------------------------------------------------
_info() { printf '[claude-code-local] %s\n' "$*"; }
_warn() { printf '[claude-code-local] WARN: %s\n' "$*" >&2; }
_fatal() {
  _f_code="$1"; shift
  printf '[claude-code-local] FATAL: %s\n' "$*" >&2
  exit "${_f_code}"
}

_usage() {
  cat <<'EOF'
Usage: install-claude-code-local.sh [OPTIONS]

Per-user placement for a LOCAL (non-managed) Claude Code install. Copies a
generated user-mode settings.json (and any helper scripts) into the per-user
Claude Code config dir. No root, no MDM.

Options:
  --source <file>     Generated user-mode settings.json
                      (default: <repo>/agent_setups/generated/claude-code/user/settings.json)
  --target-dir <dir>  Claude Code config dir (default: $HOME/.claude)
  --dry-run           Print planned actions, touch nothing (exit 0)
  --no-backup         Overwrite an existing settings.json without a timestamped backup
  --print-target      Print the resolved target file path and exit 0
  -h, --help          Show this message

Exit codes:
  0  success / --dry-run / --print-target
  1  usage error
  4  source settings.json not found
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

TARGET="${TARGET_DIR}/settings.json"

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
  _fatal 4 "Source settings.json not found: ${SOURCE}
  Generate it first: make agent-claude-code ARGS=--user-config
  (or run the one-step target: make claude-code-install-local)"
fi

# A user-mode bundle has settings.json (not managed-settings.json) at its root. If
# a managed-settings.json sits beside the source, the source is likely a managed
# bundle whose helper paths point at a root system dir, not this per-user dir.
if [ -f "$(dirname "${SOURCE}")/managed-settings.json" ]; then
  _warn "Source dir also holds managed-settings.json (a managed bundle)."
  _warn "  A managed bundle's helper paths point at a root system dir, not this"
  _warn "  per-user dir. For a local install, regenerate in user mode:"
  _warn "  make agent-claude-code ARGS=--user-config"
fi

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
_info "=== Claude Code local install ==="
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
  for _h in ${HELPERS}; do
    if [ -f "${_src_dir}/${_h}" ]; then
      _info "  [plan] copy   \"${_src_dir}/${_h}\" -> \"${TARGET_DIR}/${_h}\""
      _info "  [plan] chmod 755 \"${TARGET_DIR}/${_h}\""
    fi
  done
else
  mkdir -p -- "${TARGET_DIR}" || _fatal 5 "mkdir failed: ${TARGET_DIR}"
  cp -- "${SOURCE}" "${TARGET}" || _fatal 5 "copy failed: ${SOURCE} -> ${TARGET}"
  chmod 644 "${TARGET}" || _fatal 5 "chmod 644 failed: ${TARGET}"
  _info "  installed settings.json"
  # The helper scripts (settings.json references them by absolute path) install
  # beside settings.json and must be executable.
  for _h in ${HELPERS}; do
    if [ -f "${_src_dir}/${_h}" ]; then
      cp -- "${_src_dir}/${_h}" "${TARGET_DIR}/${_h}" || _fatal 5 "copy failed: ${_src_dir}/${_h} -> ${TARGET_DIR}/${_h}"
      chmod 755 "${TARGET_DIR}/${_h}" || _fatal 5 "chmod 755 failed: ${TARGET_DIR}/${_h}"
      _info "  installed ${_h}"
    fi
  done
fi

# ---------------------------------------------------------------------------
# Auth reminder (apiKeyHelper mints tokens; the developer logs in once)
# ---------------------------------------------------------------------------
printf '\nLocal install complete. The apiKeyHelper in settings.json mints a fresh\n'
printf 'Databricks token per request, so no environment variable is needed.\n'
printf 'The Databricks CLI refreshes access tokens silently, so routine expiry\n'
printf 'needs no login. Authenticate once:\n'
printf '  databricks auth login --host <host> --profile <profile>\n'
printf 'Verify inside Claude Code with /status (Setting sources -> User settings).\n\n'

exit 0
