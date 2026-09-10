#!/usr/bin/env sh
# build-coding-agents-pkg.sh — build the macOS installer package for the coding agents.
#
# Claude Code and Codex each read a root-owned managed file that an MDM tool must
# place. This package places them. It is the sibling of
# build-claude-desktop-pkg.sh, and the two version independently so you can stage
# or roll back one agent without touching the other.
#
#   this .pkg                     -> Claude Code + Codex managed configs
#   claude-desktop-<version>.pkg  -> Claude Desktop helpers + SSO LaunchAgent
#   .mobileconfig                 -> Claude Desktop settings, exported by the app
#
# Every MDM deploys a .pkg. So does `installer -pkg` over SSH, which is how the VM
# runbook rehearses the same payload without an MDM.
#
# This package does NOT carry ug. ug has its own distribution (`uv tool install`,
# plus `ug upgrade`), and its state is per-user, so a machine-wide package would
# fight both. Deploy ug as its own Jamf Package or Policy.
#
# Usage:
#   build-coding-agents-pkg.sh --source <generated-dir> [OPTIONS]
#
# Options:
#   --source <dir>        Generated bundle root, holding claude-code/<os>/ and
#                         codex/etc/. Required.
#   --out <path>          Output .pkg path (default: dist/coding-agents-<version>.pkg)
#   --version <v>         Package version (default: 0.0.0-dev)
#   --identifier <id>     Package identifier (default: com.databricks.unity-gateway.coding-agents)
#   --skip-claude-code    Do not place the Claude Code payload
#   --skip-codex          Do not place the Codex payload
#   --sign <identity>     Developer ID Installer identity to sign with (optional)
#   -h, --help            Show this message
#
# Exit codes:
#   0 success   1 usage error   3 missing tool   4 missing source file   5 build failure
set -eu

SOURCE=""
OUT=""
VERSION="0.0.0-dev"
IDENTIFIER="com.databricks.unity-gateway.coding-agents"
SIGN_ID=""
WANT_CLAUDE_CODE=1
WANT_CODEX=1

# Claude Code reads a per-OS bundle. A .pkg only ever targets macOS, so the macOS
# bundle is the only correct source.
CC_OS="macos"
CC_DIR="/Library/Application Support/ClaudeCode"
CX_DIR="/etc/codex"

_info() { printf '[build-pkg] %s\n' "$*"; }
_warn() { printf '[build-pkg] WARN: %s\n' "$*" >&2; }
_fatal() { _code="$1"; shift; printf '[build-pkg] FATAL: %s\n' "$*" >&2; exit "${_code}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --source)           shift; SOURCE="${1:?--source requires a value}" ;;
    --out)              shift; OUT="${1:?--out requires a value}" ;;
    --version)          shift; VERSION="${1:?--version requires a value}" ;;
    --identifier)       shift; IDENTIFIER="${1:?--identifier requires a value}" ;;
    --skip-claude-code) WANT_CLAUDE_CODE=0 ;;
    --skip-codex)       WANT_CODEX=0 ;;
    --sign)             shift; SIGN_ID="${1:?--sign requires a value}" ;;
    -h|--help)          sed -n '2,37p' "$0"; exit 1 ;;
    *)                  _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

[ -n "${SOURCE}" ] || _fatal 1 "--source is required (the generated bundle root)."
[ -d "${SOURCE}" ] || _fatal 4 "Source dir not found: ${SOURCE}"
command -v pkgbuild >/dev/null 2>&1 || _fatal 3 "pkgbuild not found. It ships with the macOS Command Line Tools."

if [ "${WANT_CLAUDE_CODE}" = "0" ] && [ "${WANT_CODEX}" = "0" ]; then
  _fatal 1 "--skip-claude-code and --skip-codex together leave nothing to package."
fi

CC_SRC="${SOURCE}/claude-code/${CC_OS}"
CX_SRC="${SOURCE}/codex/etc"

[ -n "${OUT}" ] || OUT="dist/coding-agents-${VERSION}.pkg"
mkdir -p "$(dirname "${OUT}")"

STAGE="$(mktemp -d)"
# shellcheck disable=SC2064  # expand the path now, so cleanup works after a cd
trap "rm -rf '${STAGE}'" EXIT INT TERM

# install_file <src-dir> <basename> <dest-abs-dir> <mode> <required 0|1>
install_file() {
  _if_src="$1"; _if_name="$2"; _if_dest="$3"; _if_mode="$4"; _if_req="$5"
  if [ -f "${_if_src}/${_if_name}" ]; then
    mkdir -p "${STAGE}${_if_dest}"
    # -X drops the extended attributes that CAN be dropped, such as quarantine
    # flags. com.apple.provenance survives on macOS 15+ and is harmless: see the
    # note beside the payload listing below.
    cp -X "${_if_src}/${_if_name}" "${STAGE}${_if_dest}/${_if_name}"
    xattr -c "${STAGE}${_if_dest}/${_if_name}" 2>/dev/null || true
    chmod "${_if_mode}" "${STAGE}${_if_dest}/${_if_name}"
    _info "  payload: ${_if_dest}/${_if_name} (${_if_mode})"
  elif [ "${_if_req}" = "1" ]; then
    _fatal 4 "Required file missing from the bundle: ${_if_src}/${_if_name}"
  else
    _info "  skip   : ${_if_name} (not in the bundle)"
  fi
}

_info "staging payload"

# --- Claude Code -------------------------------------------------------------
if [ "${WANT_CLAUDE_CODE}" = "1" ]; then
  [ -d "${CC_SRC}" ] || _fatal 4 \
    "No Claude Code ${CC_OS} bundle at '${CC_SRC}'. Generate one first: make agent-claude-code"
  install_file "${CC_SRC}" "managed-settings.json"   "${CC_DIR}" 644 1
  install_file "${CC_SRC}" "otel-headers-helper.sh"  "${CC_DIR}" 755 0
  install_file "${CC_SRC}" "emit_hook_events.sh"     "${CC_DIR}" 755 0
else
  _info "  skip   : Claude Code (--skip-claude-code)"
fi

# --- Codex -------------------------------------------------------------------
# Codex has two modes. install.sh SILENTLY skips a user-mode bundle (config.toml at
# the codex root, no etc/managed_config.toml), so a package built from one would
# deploy nothing and still exit green. Fail loud instead, exactly as deploy-package
# does.
if [ "${WANT_CODEX}" = "1" ]; then
  if [ ! -f "${CX_SRC}/managed_config.toml" ]; then
    if [ -f "${SOURCE}/codex/config.toml" ]; then
      _fatal 4 \
        "'${SOURCE}/codex' is user-mode (no etc/managed_config.toml). A package built
             from it would place nothing. Regenerate codex in managed mode:
               make agent-codex OUT_DIR=${SOURCE}"
    fi
    _fatal 4 \
      "No Codex managed bundle at '${CX_SRC}'. Generate one first: make agent-codex"
  fi
  install_file "${CX_SRC}" "managed_config.toml" "${CX_DIR}" 644 1
  install_file "${CX_SRC}" "requirements.toml"   "${CX_DIR}" 644 1
  install_file "${CX_SRC}" "emit_hook_events.sh" "${CX_DIR}" 755 0
else
  _info "  skip   : Codex (--skip-codex)"
fi

# Clear whatever is clearable on the directories too. com.apple.provenance stays.
xattr -rc "${STAGE}" 2>/dev/null || true

# No postinstall. Neither agent runs a daemon that needs a reload: Claude Code reads
# managed-settings.json at the next launch, and Codex reads managed_config.toml at
# the next run.
_info "building ${OUT}"
pkgbuild \
  --root "${STAGE}" \
  --identifier "${IDENTIFIER}" \
  --version "${VERSION}" \
  --install-location / \
  "${OUT}" >/dev/null || _fatal 5 "pkgbuild failed."

if [ -n "${SIGN_ID}" ]; then
  command -v productsign >/dev/null 2>&1 || _fatal 3 "productsign not found."
  _info "signing with '${SIGN_ID}'"
  productsign --sign "${SIGN_ID}" "${OUT}" "${OUT}.signed" \
    || _fatal 5 "productsign failed."
  mv "${OUT}.signed" "${OUT}"
else
  _warn "Package is UNSIGNED. Jamf and installer(8) accept it, but Gatekeeper"
  _warn "  blocks a double-click install. Pass --sign for distribution."
fi

# `pkgutil --payload-files` lists a ._<name> entry beside each real file. Those are
# NOT litter, and they are not removable. macOS 15 and later stamp every file with a
# com.apple.provenance extended attribute that neither `xattr -c` nor
# `ditto --noextattr` can clear, and pkgbuild encodes any xattr as an AppleDouble
# companion inside the payload. On install the payload is decoded and the attribute
# is restored on the real file, so no ._ file lands on disk. Verified with
# `pkgutil --expand-full`, whose extracted tree contains the real files only.
_info "payload:"
pkgutil --payload-files "${OUT}" | grep -v '/\._' | sed 's/^/  /'

_info "Done: ${OUT}"
_info "Install headlessly:  sudo installer -pkg '${OUT}' -target /"
_info "Deploy with an MDM:  upload it as a package, and scope it to your devices."
