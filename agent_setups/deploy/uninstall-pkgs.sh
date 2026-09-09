#!/usr/bin/env sh
# uninstall-pkgs.sh — remove the unity-gateway macOS installer packages.
#
# macOS has no native package uninstaller. A .pkg leaves a receipt, and the receipt
# lists what it placed, so this script reads the receipts and removes those files.
#
# `install.sh --uninstall` does NOT cover a package install. It reads the version
# marker that install.sh writes, and the package path never writes one. So use this
# script for anything installed with `installer -pkg` or through an MDM.
#
# It removes the files each receipt lists, unloads the SSO LaunchAgent first, then
# forgets the receipt. It only unlinks regular files and then tries `rmdir` on the
# directories, so a shared directory with other content is always left in place.
#
# Usage:
#   sudo uninstall-pkgs.sh [OPTIONS]
#
# Options:
#   --dry-run             Print what would be removed, touch nothing
#   --purge-user-state    Also clear ug's per-user state (see the note below)
#   --target-root <p>     Prefix for staged testing (default: "")
#   -h, --help            Show this message
#
# What --purge-user-state clears, for the logged-in user:
#   - `ug revert`, which restores the agent config files ug backed up
#   - ~/.ucode, ug's saved workspace and model state
#   - ~/Library/Logs/ug-sso-bootstrap.log
#   - the ug tool that uv installed
#
# It deliberately does NOT touch ~/.databrickscfg, because that file holds every
# workspace profile a developer has. To drop one workspace's credentials, run
# `databricks auth logout --profile <name>` yourself and name the profile.
#
# Exit codes:
#   0 success (or --dry-run)   1 usage error   2 not root   6 a removal failed
set -eu

DRY_RUN=0
PURGE_USER=0
TARGET_ROOT=""
PKG_PREFIX="com.databricks.unity-gateway."

_info() { printf '[uninstall] %s\n' "$*"; }
_warn() { printf '[uninstall] WARN: %s\n' "$*" >&2; }
_fatal() { _c="$1"; shift; printf '[uninstall] FATAL: %s\n' "$*" >&2; exit "${_c}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)          DRY_RUN=1 ;;
    --purge-user-state) PURGE_USER=1 ;;
    --target-root)      shift; TARGET_ROOT="${1:?--target-root requires a value}" ;;
    -h|--help)          sed -n '2,38p' "$0"; exit 1 ;;
    *)                  _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

command -v pkgutil >/dev/null 2>&1 || _fatal 1 "pkgutil not found. This script is macOS only."

if [ "${DRY_RUN}" = "0" ] && [ -z "${TARGET_ROOT}" ] && [ "$(id -u)" != "0" ]; then
  _fatal 2 "Run as root: sudo $0"
fi

# The receipts this repo's packages leave behind.
PKGS="$(pkgutil --pkgs 2>/dev/null | grep "^${PKG_PREFIX}" || true)"
if [ -z "${PKGS}" ]; then
  _info "No ${PKG_PREFIX}* packages are installed."
  # --purge-user-state still has work to do: a developer may have removed the
  # packages already and now wants ug's own state cleared before a redeploy.
  if [ "${PURGE_USER}" = "0" ]; then
    _info "Nothing to do. Pass --purge-user-state to clear ug per-user state too."
    exit 0
  fi
else
  _info "Packages found:"
  printf '%s\n' "${PKGS}" | sed 's/^/  /'
fi
[ "${DRY_RUN}" = "1" ] && _info "(dry run: nothing will be removed)"

# --- 1. Unload the SSO LaunchAgent, before its plist disappears --------------
# launchd keeps a loaded job until it is booted out, so removing the plist alone
# leaves the agent running until the next logout.
_console_uid="$(stat -f %u /dev/console 2>/dev/null || echo 0)"
_console_user="$(stat -f %Su /dev/console 2>/dev/null || echo '')"
_plist="${TARGET_ROOT}/Library/LaunchAgents/ug-sso-bootstrap.plist"
if [ -f "${_plist}" ]; then
  _label="$(plutil -extract Label raw -o - "${_plist}" 2>/dev/null || true)"
  if [ -n "${_label}" ] && [ "${_console_uid}" -ge 501 ] 2>/dev/null; then
    if [ "${DRY_RUN}" = "1" ]; then
      _info "  [plan] launchctl bootout gui/${_console_uid}/${_label}"
    else
      launchctl bootout "gui/${_console_uid}/${_label}" 2>/dev/null || true
      _info "  unloaded: ${_label}"
    fi
  fi
fi

# --- 2. Remove the files each receipt lists ----------------------------------
# Only regular files. Directories are handled afterwards with rmdir, so a shared
# directory that still holds other content is never removed.
# The file listing runs in a pipeline, so its `while` body is a subshell and cannot
# set a variable in this shell. Record failures in a temp file instead.
_failmark="$(mktemp)"
# shellcheck disable=SC2064  # expand now, so cleanup survives a later cd
trap "rm -f '${_failmark}'" EXIT INT TERM

for _pkg in ${PKGS}; do
  _info "=== ${_pkg} ==="
  # `._*` entries are AppleDouble companions that pkgbuild records for the
  # com.apple.provenance attribute. They never land on disk, so skip them.
  pkgutil --only-files --files "${_pkg}" 2>/dev/null | grep -v '/\._\|^\._' | while IFS= read -r _rel; do
    [ -n "${_rel}" ] || continue
    _abs="${TARGET_ROOT}/${_rel}"
    if [ ! -e "${_abs}" ]; then
      _info "  skip   : ${_abs} (already gone)"
      continue
    fi
    if [ "${DRY_RUN}" = "1" ]; then
      _info "  [plan] rm \"${_abs}\""
    elif rm -f -- "${_abs}" 2>/dev/null; then
      _info "  removed: ${_abs}"
    else
      _warn "Failed to remove '${_abs}'"
      echo failed >>"${_failmark}"
    fi
  done
done
_failed=0
[ -s "${_failmark}" ] && _failed=1

# --- 3. Remove the directories we own, only when empty -----------------------
for _d in \
  "${TARGET_ROOT}/Library/Application Support/ClaudeDesktop" \
  "${TARGET_ROOT}/Library/Application Support/ClaudeCode" \
  "${TARGET_ROOT}/etc/codex"
do
  [ -d "${_d}" ] || continue
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] rmdir \"${_d}\" (only if empty)"
  elif rmdir -- "${_d}" 2>/dev/null; then
    _info "  removed empty dir: ${_d}"
  else
    _info "  kept   : ${_d} (not empty)"
  fi
done

# --- 4. Forget the receipts --------------------------------------------------
for _pkg in ${PKGS}; do
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] pkgutil --forget ${_pkg}"
  else
    if pkgutil --forget "${_pkg}" >/dev/null 2>&1; then
      _info "  forgot receipt: ${_pkg}"
    else
      _warn "Could not forget ${_pkg}"
    fi
  fi
done

# --- 5. Optional: ug's per-user state ---------------------------------------
if [ "${PURGE_USER}" = "1" ]; then
  _info "=== per-user ug state for '${_console_user}' ==="
  if [ -z "${_console_user}" ] || [ "${_console_uid}" -lt 501 ] 2>/dev/null; then
    _warn "No logged-in user to purge state for. Skipping."
  else
    _home="$(eval echo "~${_console_user}")"
    # A login shell for this user does not necessarily have ~/.local/bin on PATH, and
    # uv warns about exactly that after `uv tool install`. So set PATH explicitly, or
    # `ug` and `uv` are not found and every step reports itself skipped.
    _upath="${_home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    # uv installed the tool under its distribution name. That is `ucode` today and
    # `unity-gateway` after the rename, so try both and let the wrong one no-op.
    for _cmd in \
      "ug revert" \
      "rm -rf ${_home}/.ucode" \
      "rm -f ${_home}/Library/Logs/ug-sso-bootstrap.log" \
      "uv tool uninstall ucode" \
      "uv tool uninstall unity-gateway"
    do
      if [ "${DRY_RUN}" = "1" ]; then
        _info "  [plan] (as ${_console_user}) ${_cmd}"
      else
        if su - "${_console_user}" -c "PATH='${_upath}' ${_cmd}" >/dev/null 2>&1; then
          _info "  ran: ${_cmd}"
        else
          _info "  skipped (not applicable): ${_cmd}"
        fi
      fi
    done
    _info "  NOT touched: ${_home}/.databrickscfg — it holds every workspace profile."
    _info "  To drop one workspace, run: databricks auth logout --profile <name>"
  fi
fi

if [ "${_failed}" = "1" ]; then
  _fatal 6 "Some files could not be removed. Receipts were still forgotten where possible."
fi

_info ""
_info "Done. To redeploy: regenerate the bundles, run 'make packages', and install again."
