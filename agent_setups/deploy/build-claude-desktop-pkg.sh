#!/usr/bin/env sh
# build-claude-desktop-pkg.sh — build the macOS installer package for Claude Desktop.
#
# A configuration profile (.mobileconfig) carries SETTINGS ONLY. It cannot place a
# file or run a command. So a Claude Desktop rollout needs two artifacts:
#
#   1. this .pkg          -> the credential + OTEL helper scripts
#   2. ug-bootstrap.pkg   -> uv, the SSO bootstrap, and its LaunchAgent
#   3. the .mobileconfig  -> the Claude Desktop settings, exported by the app itself
#
# The SSO bootstrap and its LaunchAgent moved to ug-bootstrap.pkg, because they carry
# the TOOL rather than this agent's config, and so version on their own cadence. A
# Claude Desktop deployment needs both packages.
#
# Every MDM (Jamf, Intune, Kandji) deploys a .pkg. So does `installer -pkg` over
# SSH, which is how the VM runbook rehearses the same payload without an MDM.
#
# This package does NOT carry ug. ug has its own distribution, and the bootstrap
# script logs and exits when ug is absent, then retries at the next login. So the
# two packages may arrive in either order.
#
# Usage:
#   build-claude-desktop-pkg.sh --source <bundle-dir> [OPTIONS]
#
# Options:
#   --source <dir>        Generated macOS bundle (claude-desktop/macos). Required.
#   --out <path>          Output .pkg path (default: dist/claude-desktop-<version>.pkg)
#   --version <v>         Package version (default: 0.0.0-dev)
#   --identifier <id>     Package identifier (default: com.databricks.unity-gateway.claude-desktop)
#   --install-dir <dir>   Absolute helper dir inside the payload
#                         (default: /Library/Application Support/ClaudeDesktop)
#   --sign <identity>     Developer ID Installer identity to sign with (optional)
#   -h, --help            Show this message
#
# Exit codes:
#   0 success   1 usage error   3 missing tool   4 missing source file   5 build failure
set -eu

SOURCE=""
OUT=""
VERSION="0.0.0-dev"
IDENTIFIER="com.databricks.unity-gateway.claude-desktop"
INSTALL_DIR="/Library/Application Support/ClaudeDesktop"
SIGN_ID=""

CRED_HELPER="databricks-token.sh"
OTEL_HELPER="otel-headers-helper.sh"

_info() { printf '[build-pkg] %s\n' "$*"; }
_warn() { printf '[build-pkg] WARN: %s\n' "$*" >&2; }
_fatal() { _code="$1"; shift; printf '[build-pkg] FATAL: %s\n' "$*" >&2; exit "${_code}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --source)      shift; SOURCE="${1:?--source requires a value}" ;;
    --out)         shift; OUT="${1:?--out requires a value}" ;;
    --version)     shift; VERSION="${1:?--version requires a value}" ;;
    --identifier)  shift; IDENTIFIER="${1:?--identifier requires a value}" ;;
    --install-dir) shift; INSTALL_DIR="${1:?--install-dir requires a value}" ;;
    --sign)        shift; SIGN_ID="${1:?--sign requires a value}" ;;
    -h|--help)     sed -n '2,32p' "$0"; exit 1 ;;
    *)             _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

[ -n "${SOURCE}" ] || _fatal 1 "--source is required (the generated claude-desktop/macos dir)."
[ -d "${SOURCE}" ] || _fatal 4 "Source dir not found: ${SOURCE}"
command -v pkgbuild >/dev/null 2>&1 || _fatal 3 "pkgbuild not found. It ships with the macOS Command Line Tools."

# The credential helper is the one file without which the package is pointless.
[ -f "${SOURCE}/${CRED_HELPER}" ] || _fatal 4 \
  "No ${CRED_HELPER} in '${SOURCE}'. Generate a macOS bundle first: make agent-claude-desktop"

[ -n "${OUT}" ] || OUT="dist/claude-desktop-${VERSION}.pkg"
mkdir -p "$(dirname "${OUT}")"

STAGE="$(mktemp -d)"
SCRIPTDIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand the paths now, so cleanup works after a cd
trap "rm -rf '${STAGE}' '${SCRIPTDIR}'" EXIT INT TERM

_info "staging payload"
mkdir -p "${STAGE}${INSTALL_DIR}"

install_file() {
  # $1=basename  $2=mode  $3=required(0|1)
  if [ -f "${SOURCE}/$1" ]; then
    # -X drops the extended attributes that CAN be dropped, such as quarantine
    # flags. com.apple.provenance survives on macOS 15+ and is harmless: see the
    # note beside the payload listing below.
    cp -X "${SOURCE}/$1" "${STAGE}${INSTALL_DIR}/$1"
    xattr -c "${STAGE}${INSTALL_DIR}/$1" 2>/dev/null || true
    chmod "$2" "${STAGE}${INSTALL_DIR}/$1"
    _info "  payload: ${INSTALL_DIR}/$1 ($2)"
  elif [ "$3" = "1" ]; then
    _fatal 4 "Required file missing from the bundle: $1"
  else
    _info "  skip   : $1 (not in the bundle)"
  fi
}

install_file "${CRED_HELPER}"  755 1
install_file "${OTEL_HELPER}"  755 0

xattr -rc "${STAGE}" 2>/dev/null || true

# No postinstall. The SSO bootstrap and its LaunchAgent ship in ug-bootstrap.pkg, and
# that package's postinstall loads the agent.
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
#
# So hide them from the listing rather than failing the build over them.
_info "payload:"
pkgutil --payload-files "${OUT}" | grep -v '/\._' | sed 's/^/  /'

_info "Done: ${OUT}"
_info "Install headlessly:  sudo installer -pkg '${OUT}' -target /"
_info "Deploy with an MDM:  upload it as a package, and scope it to your devices."
