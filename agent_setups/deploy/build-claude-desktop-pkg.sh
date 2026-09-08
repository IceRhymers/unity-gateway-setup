#!/usr/bin/env sh
# build-claude-desktop-pkg.sh — build the macOS installer package for Claude Desktop.
#
# A configuration profile (.mobileconfig) carries SETTINGS ONLY. It cannot place a
# file or run a command. So a Claude Desktop rollout needs two artifacts:
#
#   1. this .pkg          -> the helper scripts, the SSO bootstrap, the LaunchAgent
#   2. the .mobileconfig  -> the Claude Desktop settings, exported by the app itself
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
#   --no-autoload         Do not load the LaunchAgent in postinstall
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
AUTOLOAD=1

CRED_HELPER="databricks-token.sh"
OTEL_HELPER="otel-headers-helper.sh"
SSO_BOOTSTRAP="ug-sso-bootstrap.sh"
LAUNCHAGENT="ug-sso-bootstrap.plist"
LAUNCHAGENT_DIR="/Library/LaunchAgents"

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
    --no-autoload) AUTOLOAD=0 ;;
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
install_file "${SSO_BOOTSTRAP}" 755 0

# The LaunchAgent belongs in /Library/LaunchAgents, not the helper dir, so launchd
# loads it for every user who logs in. launchd refuses a group- or world-writable
# agent plist, so mode 644 is not cosmetic.
HAVE_AGENT=0
if [ -f "${SOURCE}/${LAUNCHAGENT}" ]; then
  mkdir -p "${STAGE}${LAUNCHAGENT_DIR}"
  cp -X "${SOURCE}/${LAUNCHAGENT}" "${STAGE}${LAUNCHAGENT_DIR}/${LAUNCHAGENT}"
  xattr -c "${STAGE}${LAUNCHAGENT_DIR}/${LAUNCHAGENT}" 2>/dev/null || true
  chmod 644 "${STAGE}${LAUNCHAGENT_DIR}/${LAUNCHAGENT}"
  HAVE_AGENT=1
  _info "  payload: ${LAUNCHAGENT_DIR}/${LAUNCHAGENT} (644)"
else
  _warn "No ${LAUNCHAGENT} in the bundle. The package will place no login trigger."
  _warn "  Regenerate without --no-sso-bootstrap to include it."
fi

# --- postinstall -------------------------------------------------------------
# Loading the agent here means the developer is prompted right after the package
# lands, instead of waiting for the next login. It is best-effort: at imaging time
# there is no console user, and the agent then loads at the first real login.
if [ "${HAVE_AGENT}" = "1" ] && [ "${AUTOLOAD}" = "1" ]; then
  cat > "${SCRIPTDIR}/postinstall" <<'POST'
#!/bin/sh
# Generated by unity-gateway-setup. Load the SSO-bootstrap LaunchAgent for the
# user who is logged in now, so the one-time browser login can happen without a
# logout. Never fail the install over this.
set -u
PLIST="/Library/LaunchAgents/ug-sso-bootstrap.plist"
[ -f "$PLIST" ] || exit 0

# The console owner is the user with the GUI session. A uid below 501 means no
# real user is logged in (imaging, or the login window), so there is no GUI
# session to load into. The agent then loads at the first real login.
uid="$(stat -f %u /dev/console 2>/dev/null || echo 0)"
case "$uid" in
  ''|*[!0-9]*) exit 0 ;;
esac
[ "$uid" -ge 501 ] || exit 0

# bootout first so a reinstall reloads the current plist rather than keeping the
# already-loaded job. Both calls are best-effort.
launchctl bootout "gui/$uid/__AGENT_LABEL__" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$PLIST" 2>/dev/null || true
exit 0
POST
  # The Label is whatever the generator baked into the plist (--launchagent-label
  # can change it), so read it back rather than hardcoding one. A wrong Label here
  # would make the bootout a no-op, and a reinstall would keep the stale job.
  AGENT_LABEL="$(plutil -extract Label raw -o - "${SOURCE}/${LAUNCHAGENT}" 2>/dev/null || true)"
  [ -n "${AGENT_LABEL}" ] || _fatal 4 \
    "Could not read the Label key from '${SOURCE}/${LAUNCHAGENT}'."
  # LC_ALL=C keeps sed from choking on a non-ASCII byte in the label.
  LC_ALL=C sed -i '' "s|__AGENT_LABEL__|${AGENT_LABEL}|g" "${SCRIPTDIR}/postinstall"
  _info "  label  : ${AGENT_LABEL}"
  chmod 755 "${SCRIPTDIR}/postinstall"
  _info "  script : postinstall (loads the LaunchAgent for the console user)"
  PKG_SCRIPTS="--scripts ${SCRIPTDIR}"
else
  PKG_SCRIPTS=""
  [ "${AUTOLOAD}" = "1" ] || _info "  script : none (--no-autoload)"
fi

# Clear whatever is clearable on the directories too. com.apple.provenance stays.
xattr -rc "${STAGE}" 2>/dev/null || true

_info "building ${OUT}"
# shellcheck disable=SC2086  # PKG_SCRIPTS is an intentional word-split flag pair
pkgbuild \
  --root "${STAGE}" \
  --identifier "${IDENTIFIER}" \
  --version "${VERSION}" \
  --install-location / \
  ${PKG_SCRIPTS} \
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
