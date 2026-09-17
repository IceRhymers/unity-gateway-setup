#!/usr/bin/env sh
# demo-install.sh — install every unity-gateway macOS package in one step.
#
# `make packages` builds three packages. A demo machine needs all three, and
# `installer -pkg` takes one package per run. This script runs `installer` once
# for each package, in a fixed order, so an operator runs one command.
#
# Run it ON the target machine, from the directory that holds the packages. The
# `make packages` target copies this script into dist/, beside the packages it
# installs. Copy that whole directory to the machine, then run this script there.
#
#   sudo ./demo-install.sh
#
# It installs these packages, in this order:
#
#   1. coding-agents-<version>.pkg   Claude Code + Codex managed configs
#   2. claude-desktop-<version>.pkg  Claude Desktop helper scripts
#   3. ug-bootstrap-<version>.pkg    the ug SSO bootstrap + its LaunchAgent
#
# The order is fixed for a repeatable demo. The packages share no file, so any
# order installs the same result.
#
# It finds all three packages before it installs the first one. A missing package
# stops the script before it changes the machine.
#
# What this script does NOT do:
#
#   - It does not install the .mobileconfig. The Claude Desktop app exports that
#     profile, and an MDM deploys it. See runbooks/claude-desktop-mdm.md.
#   - It does not install uv, ug, the databricks CLI, or Codex. The packages and
#     their LaunchAgent own that work.
#   - It does not authenticate. Each developer runs `databricks auth login` once,
#     in a browser. Nobody can push that step.
#
# To remove what this script installs, run `uninstall-pkgs.sh`.
#
# Usage:
#   sudo demo-install.sh [OPTIONS]
#
# Options:
#   --dir <dir>       Directory that holds the packages (default: this script's dir)
#   --version <v>     Install this exact version, when several versions are present
#   --dry-run         Print the planned installs, change nothing (exit 0)
#   -h, --help        Show this message
#
# Exit codes:
#   0 success (or --dry-run)   1 usage error   2 not root   3 missing tool
#   4 a package is missing or ambiguous   5 an install failed
set -eu

# The packages, in install order. Each name is the basename prefix that
# build-*-pkg.sh writes, before the "-<version>.pkg" suffix.
PKG_NAMES="coding-agents claude-desktop ug-bootstrap"

PKG_PREFIX="com.databricks.unity-gateway."

_self_dir="$(cd "$(dirname "$0")" && pwd)"

DIR="${_self_dir}"
VERSION=""
DRY_RUN=0

_info() { printf '[demo-install] %s\n' "$*"; }
_warn() { printf '[demo-install] WARN: %s\n' "$*" >&2; }
_fatal() { _c="$1"; shift; printf '[demo-install] FATAL: %s\n' "$*" >&2; exit "${_c}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)      shift; DIR="${1:?--dir requires a value}" ;;
    --version)  shift; VERSION="${1:?--version requires a value}" ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  sed -n '2,48p' "$0"; exit 1 ;;
    *)          _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

[ -d "${DIR}" ] || _fatal 1 "Not a directory: ${DIR}"
DIR="$(cd "${DIR}" && pwd)"

command -v installer >/dev/null 2>&1 || _fatal 3 "installer not found. This script is macOS only."
command -v pkgutil   >/dev/null 2>&1 || _fatal 3 "pkgutil not found. This script is macOS only."

if [ "${DRY_RUN}" = "0" ] && [ "$(id -u)" != "0" ]; then
  _fatal 2 "Run as root: sudo $0"
fi

# ---------------------------------------------------------------------------
# Resolve one package name to one file. Sets PKG_PATH on success.
#
# An unmatched glob in sh stays literal, so every candidate needs a -f test.
# Two versions of one package in the same directory is an operator error: the
# script names both files and stops, rather than picking one.
# ---------------------------------------------------------------------------
_resolve_pkg() {
  _rp_name="$1"
  PKG_PATH=""
  _rp_n=0
  for _rp_c in "${DIR}/${_rp_name}-"*.pkg; do
    [ -f "${_rp_c}" ] || continue
    if [ -n "${VERSION}" ] && [ "${_rp_c}" != "${DIR}/${_rp_name}-${VERSION}.pkg" ]; then
      continue
    fi
    _rp_n=$((_rp_n + 1))
    PKG_PATH="${_rp_c}"
  done

  if [ "${_rp_n}" = "0" ]; then
    return 1
  fi

  if [ "${_rp_n}" -gt 1 ]; then
    _warn "The directory holds ${_rp_n} ${_rp_name} packages:"
    for _rp_c in "${DIR}/${_rp_name}-"*.pkg; do
      [ -f "${_rp_c}" ] && printf '    %s\n' "${_rp_c}" >&2
    done
    _fatal 4 "Pass --version <v> to choose one, or delete the old package."
  fi

  return 0
}

# ---------------------------------------------------------------------------
# Preflight: find every package first, so a missing file stops the script
# before it installs anything.
# ---------------------------------------------------------------------------
_info "source : ${DIR}"
[ -n "${VERSION}" ] && _info "version: ${VERSION}"

PLAN=""
MISSING=""
for _name in ${PKG_NAMES}; do
  if _resolve_pkg "${_name}"; then
    _info "  found  : $(basename "${PKG_PATH}")"
    PLAN="${PLAN:+${PLAN} }${PKG_PATH}"
  else
    _warn "  missing: ${_name}-${VERSION:-<version>}.pkg"
    MISSING="${MISSING:+${MISSING} }${_name}"
  fi
done

if [ -n "${MISSING}" ]; then
  _warn "Build the packages first, then copy dist/ to this machine:"
  _warn "  make packages"
  _fatal 4 "Missing in ${DIR}: ${MISSING}"
fi

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
for _pkg in ${PLAN}; do
  if [ "${DRY_RUN}" = "1" ]; then
    _info "[plan] installer -pkg \"${_pkg}\" -target /"
    continue
  fi
  _info ""
  _info "=== $(basename "${_pkg}") ==="
  installer -pkg "${_pkg}" -target / || _fatal 5 "installer failed: ${_pkg}"
done

if [ "${DRY_RUN}" = "1" ]; then
  _info ""
  _info "Dry run. Nothing changed."
  exit 0
fi

# ---------------------------------------------------------------------------
# Report the receipts, so the operator sees what landed
# ---------------------------------------------------------------------------
_info ""
_info "Receipts:"
pkgutil --pkgs | grep "^${PKG_PREFIX}" | sed 's/^/  /' || _warn "No receipt matched ${PKG_PREFIX}*."

_info ""
_info "Done. Two steps remain, and this script cannot do either one:"
_info "  1. Deploy the Claude Desktop .mobileconfig through your MDM."
_info "  2. Each developer runs: databricks auth login --host <url> --profile <profile>"
_info ""
_info "The ug SSO bootstrap LaunchAgent installs ug at the next login."
_info "To remove every package again, run: sudo ./uninstall-pkgs.sh"
