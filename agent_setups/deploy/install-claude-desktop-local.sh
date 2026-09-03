#!/usr/bin/env sh
# install-claude-desktop-local.sh — per-user placement of Claude Desktop helper scripts.
#
# Claude Desktop reads an OPERATOR-IMPORTED config, so there is no config file to
# place in a config dir. The credential (and optional OTEL) helper the imported
# claude-setup.json references by absolute path must exist on disk, though. This
# script places those helper scripts into a user-writable directory so you can test
# the config locally without root or MDM.
#
# The generated claude-setup.json's credential.command is baked at generation time.
# So --target-dir MUST match the directory the JSON points at. The `make
# claude-desktop-install-local` target keeps the two in sync by generating the
# bundle with --install-dir-<os> set to this same directory.
#
# Target (a user-writable helper dir; matches the JSON's baked absolute path):
#   macOS default: $HOME/Library/Application Support/ClaudeDesktop
#   Linux default: $HOME/.config/claude-desktop
#
# Generate the source bundle first (per OS), then import the JSON in the app:
#   make claude-desktop-install-local
#
# Usage:
#   install-claude-desktop-local.sh --source <bundle-dir> --target-dir <dir> [OPTIONS]
#
# Options:
#   --source <dir>      Generated per-OS bundle dir
#                       (default: <repo>/agent_setups/generated/claude-desktop/macos)
#   --target-dir <dir>  Helper directory (must match the JSON's baked command path)
#   --dry-run           Print planned actions, touch nothing (exit 0)
#   --no-backup         Overwrite existing helper scripts without a timestamped backup
#   --print-target      Print the resolved target directory and exit 0
#   -h, --help          Show this message
#
# Exit codes:
#   0  success / --dry-run / --print-target
#   1  usage error
#   4  source bundle (databricks-token.sh) not found
#   5  copy or permission failure
set -eu

# ---------------------------------------------------------------------------
# Resolve the repo-relative default source (this file is agent_setups/deploy/…)
# ---------------------------------------------------------------------------
_self_dir="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_SOURCE="${_self_dir}/../generated/claude-desktop/macos"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SOURCE="${DEFAULT_SOURCE}"
TARGET_DIR=""
DRY_RUN=0
NO_BACKUP=0
PRINT_TARGET=0

# The helper scripts a macOS/Linux bundle carries. databricks-token.sh is required
# (the credential.command target); otel-headers-helper.sh is optional (telemetry on).
REQUIRED_HELPER="databricks-token.sh"
OPTIONAL_HELPERS="otel-headers-helper.sh"

# ---------------------------------------------------------------------------
# Logging helpers (match install.sh)
# ---------------------------------------------------------------------------
_info() { printf '[claude-desktop-local] %s\n' "$*"; }
_warn() { printf '[claude-desktop-local] WARN: %s\n' "$*" >&2; }
_fatal() {
  _f_code="$1"; shift
  printf '[claude-desktop-local] FATAL: %s\n' "$*" >&2
  exit "${_f_code}"
}

_usage() {
  cat <<'EOF'
Usage: install-claude-desktop-local.sh --source <bundle-dir> --target-dir <dir> [OPTIONS]

Places the Claude Desktop helper scripts (databricks-token.sh + optional
otel-headers-helper.sh) into a user-writable directory so you can test an
imported claude-setup.json locally. It does NOT place claude-setup.json — you
import that in the app.

Options:
  --source <dir>      Generated per-OS bundle dir
                      (default: <repo>/agent_setups/generated/claude-desktop/macos)
  --target-dir <dir>  Helper directory (must match the JSON's baked command path)
  --dry-run           Print planned actions, touch nothing (exit 0)
  --no-backup         Overwrite existing helper scripts without a timestamped backup
  --print-target      Print the resolved target directory and exit 0
  -h, --help          Show this message

Exit codes:
  0  success / --dry-run / --print-target
  1  usage error
  4  source bundle (databricks-token.sh) not found
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

if [ -z "${TARGET_DIR}" ]; then
  _fatal 1 "--target-dir is required (the dir the JSON's credential.command points at)."
fi

# ---------------------------------------------------------------------------
# --print-target
# ---------------------------------------------------------------------------
if [ "${PRINT_TARGET}" = "1" ]; then
  printf '%s\n' "${TARGET_DIR}"
  exit 0
fi

# ---------------------------------------------------------------------------
# Source must exist (exit 4)
# ---------------------------------------------------------------------------
if [ ! -f "${SOURCE}/${REQUIRED_HELPER}" ]; then
  _fatal 4 "Source helper not found: ${SOURCE}/${REQUIRED_HELPER}
  Generate the bundle first (or run: make claude-desktop-install-local)."
fi

# ---------------------------------------------------------------------------
# Consistency check: the JSON's baked command path must point into TARGET_DIR.
# The command value is an absolute path; its directory must equal TARGET_DIR.
# ---------------------------------------------------------------------------
_cfg="${SOURCE}/claude-setup.json"
if [ -f "${_cfg}" ]; then
  # Extract the first "command": "<path>" value. Generator output is stable and
  # predictable, so a plain sed is enough (no jq dependency).
  _cmd="$(sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${_cfg}" | head -n 1)"
  if [ -n "${_cmd}" ]; then
    _cmd_dir="$(dirname "${_cmd}")"
    if [ "${_cmd_dir}" != "${TARGET_DIR}" ]; then
      _warn "The JSON's credential.command dir does not match --target-dir:"
      _warn "  JSON command : ${_cmd}"
      _warn "  target-dir   : ${TARGET_DIR}"
      _warn "  The imported config will look for the helper elsewhere. Regenerate with"
      _warn "  --install-dir-<os> \"${TARGET_DIR}\" (make claude-desktop-install-local keeps them in sync)."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
_info "=== Claude Desktop local helper install ==="
_info "  source : ${SOURCE}"
_info "  target : ${TARGET_DIR}"
if [ "${DRY_RUN}" = "1" ]; then
  _info "  (DRY-RUN MODE -- no files will be written)"
fi

# _place <basename>: back up an existing copy (unless suppressed), then copy 755.
_place() {
  _p_name="$1"
  _p_src="${SOURCE}/${_p_name}"
  _p_dst="${TARGET_DIR}/${_p_name}"
  if [ -f "${_p_dst}" ] && [ "${NO_BACKUP}" = "0" ]; then
    _p_bak="${_p_dst}.bak-$(date -u '+%Y%m%dT%H%M%SZ' 2>/dev/null || date -u | tr ' :' '__')"
    if [ "${DRY_RUN}" = "1" ]; then
      _info "  [plan] backup \"${_p_dst}\" -> \"${_p_bak}\""
    else
      cp -- "${_p_dst}" "${_p_bak}" || _fatal 5 "backup failed: ${_p_dst}"
      _info "  backed up existing -> ${_p_bak}"
    fi
  fi
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] copy \"${_p_src}\" -> \"${_p_dst}\""
    _info "  [plan] chmod 755 \"${_p_dst}\""
  else
    cp -- "${_p_src}" "${_p_dst}" || _fatal 5 "copy failed: ${_p_src} -> ${_p_dst}"
    chmod 755 "${_p_dst}" || _fatal 5 "chmod 755 failed: ${_p_dst}"
    _info "  installed ${_p_name}"
  fi
}

if [ "${DRY_RUN}" = "1" ]; then
  _info "  [plan] mkdir -p \"${TARGET_DIR}\""
else
  mkdir -p -- "${TARGET_DIR}" || _fatal 5 "mkdir failed: ${TARGET_DIR}"
fi

_place "${REQUIRED_HELPER}"
for _h in ${OPTIONAL_HELPERS}; do
  [ -f "${SOURCE}/${_h}" ] && _place "${_h}"
done

# ---------------------------------------------------------------------------
# Next steps (import + auth)
# ---------------------------------------------------------------------------
printf '\nHelper scripts placed. To test the config:\n'
printf '  1. Start Claude Desktop.\n'
printf '  2. Help -> Troubleshooting -> Enable Developer Mode.\n'
printf '  3. Developer -> Configure third-party inference -> import:\n'
printf '       %s\n' "${SOURCE}/claude-setup.json"
printf '  4. Test the connection.\n'
printf 'Authenticate once (browser OAuth):\n'
printf '  databricks auth login --host <host> --profile <profile>\n\n'

exit 0
