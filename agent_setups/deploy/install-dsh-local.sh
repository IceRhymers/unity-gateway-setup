#!/usr/bin/env sh
# install-dsh-local.sh — per-user placement for a LOCAL DeepSeek Harness (dsh) install.
#
# DSH is configured per user from its Harness home ($DSH_HOME, default ~/.dsh):
# there is no root-managed system path like the other agents have, so DSH has no
# managed mode. This script copies a generated home patch and its token plugin
# into $DSH_HOME, so a developer routes a local dsh through the gateway with no
# root and no MDM.
#
# Target (the DSH Harness home):
#   ${DSH_HOME:-$HOME/.dsh}/cordis.patch.yml
#   ${DSH_HOME:-$HOME/.dsh}/databricks-token-refresh.mjs
#
# Generate the source first:
#   make agent-dsh                                   # -> agent_setups/generated/dsh/cordis.patch.yml
# or run this through the one-step target:
#   make dsh-install-local
#
# Usage:
#   install-dsh-local.sh [OPTIONS]
#
# Options:
#   --source <file>     Generated cordis.patch.yml
#                       (default: <repo>/agent_setups/generated/dsh/cordis.patch.yml)
#   --target-dir <dir>  DSH Harness home
#                       (default: ${DSH_HOME:-$HOME/.dsh})
#   --dry-run           Print planned actions, touch nothing (exit 0)
#   --no-backup         Overwrite an existing cordis.patch.yml without a timestamped backup
#   --print-target      Print the resolved target patch path and exit 0
#   -h, --help          Show this message
#
# Exit codes:
#   0  success / --dry-run / --print-target
#   1  usage error
#   4  source cordis.patch.yml not found
#   5  copy or permission failure
set -eu

# ---------------------------------------------------------------------------
# Resolve the repo-relative default source (this file is agent_setups/deploy/…)
# ---------------------------------------------------------------------------
_self_dir="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_SOURCE="${_self_dir}/../generated/dsh/cordis.patch.yml"

PLUGIN_NAME="databricks-token-refresh.mjs"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SOURCE="${DEFAULT_SOURCE}"
TARGET_DIR="${DSH_HOME:-${HOME}/.dsh}"
DRY_RUN=0
NO_BACKUP=0
PRINT_TARGET=0

# ---------------------------------------------------------------------------
# Logging helpers (match install.sh)
# ---------------------------------------------------------------------------
_info() { printf '[dsh-local] %s\n' "$*"; }
_warn() { printf '[dsh-local] WARN: %s\n' "$*" >&2; }
_fatal() {
  _f_code="$1"; shift
  printf '[dsh-local] FATAL: %s\n' "$*" >&2
  exit "${_f_code}"
}

_usage() {
  cat <<'EOF'
Usage: install-dsh-local.sh [OPTIONS]

Per-user placement for a LOCAL DeepSeek Harness (dsh) install. Copies a generated
home patch and its token plugin into $DSH_HOME. No root, no MDM.

Options:
  --source <file>     Generated cordis.patch.yml
                      (default: <repo>/agent_setups/generated/dsh/cordis.patch.yml)
  --target-dir <dir>  DSH Harness home (default: ${DSH_HOME:-$HOME/.dsh})
  --dry-run           Print planned actions, touch nothing (exit 0)
  --no-backup         Overwrite an existing cordis.patch.yml without a timestamped backup
  --print-target      Print the resolved target patch path and exit 0
  -h, --help          Show this message

Exit codes:
  0  success / --dry-run / --print-target
  1  usage error
  4  source cordis.patch.yml not found
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

TARGET="${TARGET_DIR}/cordis.patch.yml"

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
  _fatal 4 "Source cordis.patch.yml not found: ${SOURCE}
  Generate it first: make agent-dsh
  (or run the one-step target: make dsh-install-local)"
fi

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
_info "=== DeepSeek Harness local install ==="
_info "  source : ${SOURCE}"
_info "  target : ${TARGET}"
if [ "${DRY_RUN}" = "1" ]; then
  _info "  (DRY-RUN MODE -- no files will be written)"
fi

# Back up an existing patch unless suppressed.
if [ -f "${TARGET}" ] && [ "${NO_BACKUP}" = "0" ]; then
  _backup="${TARGET}.bak-$(date -u '+%Y%m%dT%H%M%SZ' 2>/dev/null || date -u | tr ' :' '__')"
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] backup existing \"${TARGET}\" -> \"${_backup}\""
  else
    cp -- "${TARGET}" "${_backup}" || _fatal 5 "backup failed: ${TARGET} -> ${_backup}"
    _info "  backed up existing patch -> ${_backup}"
  fi
fi

# The token plugin sits beside the source patch. The patch references it by a
# relative path, so it must land beside cordis.patch.yml in $DSH_HOME.
_plugin_src="$(dirname "${SOURCE}")/${PLUGIN_NAME}"
_plugin_target="${TARGET_DIR}/${PLUGIN_NAME}"

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
  _info "  installed cordis.patch.yml"
  if [ -f "${_plugin_src}" ]; then
    cp -- "${_plugin_src}" "${_plugin_target}" || _fatal 5 "copy failed: ${_plugin_src} -> ${_plugin_target}"
    chmod 644 "${_plugin_target}" || _fatal 5 "chmod 644 failed: ${_plugin_target}"
    _info "  installed ${PLUGIN_NAME}"
  else
    _warn "${PLUGIN_NAME} not found beside the source; the token refresh will not load without it."
    _warn "  Re-generate: make agent-dsh"
  fi
fi

# ---------------------------------------------------------------------------
# Auth reminder (the plugin mints tokens; the developer logs in once)
# ---------------------------------------------------------------------------
printf '\nLocal install complete. The %s plugin mints a fresh Databricks\n' "${PLUGIN_NAME}"
printf 'token on a timer and stores it in the DATABRICKS_GATEWAY_TOKEN credential\n'
printf 'reference. Do NOT set DATABRICKS_GATEWAY_TOKEN in your shell: DSH treats the\n'
printf 'launch environment as read-only and it would shadow the stored token.\n'
printf 'Authenticate once (the CLI then refreshes silently):\n'
printf '  databricks auth login --host <host> --profile <profile>\n'
printf 'Verify the composed tree: dsh --profile headless --dump-config\n\n'

exit 0
