#!/usr/bin/env sh
# build-ug-bootstrap-pkg.sh — build the macOS installer package that gets ug onto a device.
#
# This is the third package. It carries the tool, not any agent's config, so it
# versions on its own cadence:
#
#   ug-bootstrap-<version>.pkg    -> the SSO bootstrap and its LaunchAgent
#   coding-agents-<version>.pkg   -> Claude Code + Codex managed configs
#   claude-desktop-<version>.pkg  -> Claude Desktop helper scripts
#
# Why it packages neither ug nor uv. Installing ug is a PER-USER action: `uv tool
# install` writes into the invoking user's home. A package script runs as root, so it
# would install into /var/root and the developer would get nothing. At imaging time
# there is no console user at all, which is exactly when an MDM install runs.
#
# So the ug install is deferred to first login, where the LaunchAgent runs it in the
# user's own session. ug then lands where `uv tool install` puts it, which is also
# where `ug upgrade` writes and where the credential helper looks first, so nothing
# competes.
#
# uv is a PREREQUISITE, for the same reasons ug is not packaged: it installs per-user
# and it self-updates (`uv self update`), so a packaged copy would go stale and fight
# its own updater. IT owns uv as part of the macOS baseline, alongside databricks,
# python3, jq, and curl. The bootstrap resolves uv by absolute path, and reports it
# clearly when absent.
#
# Usage:
#   build-ug-bootstrap-pkg.sh --source <claude-desktop/macos> [OPTIONS]
#
# Options:
#   --source <dir>      Generated macOS Claude Desktop bundle. It holds the
#                       host-baked ug-sso-bootstrap.sh and its plist. Required.
#   --out <path>        Output .pkg path (default: dist/ug-bootstrap-<version>.pkg)
#   --version <v>       Package version (default: 0.0.0-dev)
#   --identifier <id>   Package identifier (default: com.databricks.unity-gateway.ug-bootstrap)
#   --sign <identity>   Developer ID Installer identity to sign with (optional)
#   --no-autoload       Do not load the LaunchAgent in postinstall
#   -h, --help          Show this message
#
# Exit codes:
#   0 success   1 usage error   3 missing tool   4 missing source file   5 build failure
set -eu

SOURCE=""
OUT=""
VERSION="0.0.0-dev"
IDENTIFIER="com.databricks.unity-gateway.ug-bootstrap"
SIGN_ID=""
AUTOLOAD=1

SSO_BOOTSTRAP="ug-sso-bootstrap.sh"
LAUNCHAGENT="ug-sso-bootstrap.plist"
LAUNCHAGENT_DIR="/Library/LaunchAgents"
# The bootstrap script is placed beside the Claude Desktop helpers, because that is
# the directory its LaunchAgent plist names.
HELPER_DIR="/Library/Application Support/ClaudeDesktop"

_info() { printf '[build-pkg] %s\n' "$*"; }
_warn() { printf '[build-pkg] WARN: %s\n' "$*" >&2; }
_fatal() { _code="$1"; shift; printf '[build-pkg] FATAL: %s\n' "$*" >&2; exit "${_code}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --source)      shift; SOURCE="${1:?--source requires a value}" ;;
    --out)         shift; OUT="${1:?--out requires a value}" ;;
    --version)     shift; VERSION="${1:?--version requires a value}" ;;
    --identifier)  shift; IDENTIFIER="${1:?--identifier requires a value}" ;;
    --sign)        shift; SIGN_ID="${1:?--sign requires a value}" ;;
    --no-autoload) AUTOLOAD=0 ;;
    -h|--help)     sed -n '2,42p' "$0"; exit 1 ;;
    *)             _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

[ -n "${SOURCE}" ] || _fatal 1 "--source is required (the generated claude-desktop/macos dir)."
[ -d "${SOURCE}" ] || _fatal 4 "Source dir not found: ${SOURCE}"
command -v pkgbuild >/dev/null 2>&1 || _fatal 3 "pkgbuild not found. It ships with the macOS Command Line Tools."

[ -f "${SOURCE}/${SSO_BOOTSTRAP}" ] || _fatal 4 \
  "No ${SSO_BOOTSTRAP} in '${SOURCE}'. Generate a macOS bundle WITHOUT --no-sso-bootstrap."
[ -f "${SOURCE}/${LAUNCHAGENT}" ] || _fatal 4 \
  "No ${LAUNCHAGENT} in '${SOURCE}'. Generate a macOS bundle WITHOUT --no-sso-bootstrap."

[ -n "${OUT}" ] || OUT="dist/ug-bootstrap-${VERSION}.pkg"
mkdir -p "$(dirname "${OUT}")"

STAGE="$(mktemp -d)"
SCRIPTDIR="$(mktemp -d)"
# shellcheck disable=SC2064  # expand the paths now, so cleanup works after a cd
trap "rm -rf '${STAGE}' '${SCRIPTDIR}'" EXIT INT TERM

_info "staging payload"

# stage_file <src> <dest-abs-path> <mode>
stage_file() {
  _sf_src="$1"; _sf_dest="$2"; _sf_mode="$3"
  mkdir -p "${STAGE}$(dirname "${_sf_dest}")"
  # -X drops the extended attributes that CAN be dropped, such as quarantine flags.
  # com.apple.provenance survives on macOS 15+: see the note by the payload listing.
  cp -X "${_sf_src}" "${STAGE}${_sf_dest}"
  xattr -c "${STAGE}${_sf_dest}" 2>/dev/null || true
  chmod "${_sf_mode}" "${STAGE}${_sf_dest}"
  _info "  payload: ${_sf_dest} (${_sf_mode})"
}

stage_file "${SOURCE}/${SSO_BOOTSTRAP}"     "${HELPER_DIR}/${SSO_BOOTSTRAP}"      755
# launchd refuses a group- or world-writable agent plist, so 644 is not cosmetic.
stage_file "${SOURCE}/${LAUNCHAGENT}"       "${LAUNCHAGENT_DIR}/${LAUNCHAGENT}"   644

xattr -rc "${STAGE}" 2>/dev/null || true

# --- postinstall -------------------------------------------------------------
# Load the agent for the user who is logged in now, so the ug install and the SSO
# login start without waiting for a logout. Best-effort: at imaging time there is no
# console user, and the agent then loads at the first real login.
if [ "${AUTOLOAD}" = "1" ]; then
  cat > "${SCRIPTDIR}/postinstall" <<'POST'
#!/bin/sh
# Generated by unity-gateway-setup. Load the SSO-bootstrap LaunchAgent for the user
# who is logged in now. Never fail the install over this.
set -u
PLIST="/Library/LaunchAgents/ug-sso-bootstrap.plist"
[ -f "$PLIST" ] || exit 0

# The console owner is the user with the GUI session. A uid below 501 means no real
# user is logged in (imaging, or the login window), so there is no GUI session to
# load into. The agent then loads at the first real login.
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
  # Read the Label back from the plist rather than hardcoding one, so
  # --launchagent-label still boots out the right job on a reinstall.
  AGENT_LABEL="$(plutil -extract Label raw -o - "${SOURCE}/${LAUNCHAGENT}" 2>/dev/null || true)"
  [ -n "${AGENT_LABEL}" ] || _fatal 4 \
    "Could not read the Label key from '${SOURCE}/${LAUNCHAGENT}'."
  # LC_ALL=C keeps sed from choking on a non-ASCII byte in the label.
  LC_ALL=C sed -i '' "s|__AGENT_LABEL__|${AGENT_LABEL}|g" "${SCRIPTDIR}/postinstall"
  _info "  script : postinstall (loads the LaunchAgent for the console user)"
  _info "  label  : ${AGENT_LABEL}"
  chmod 755 "${SCRIPTDIR}/postinstall"
  PKG_SCRIPTS="--scripts ${SCRIPTDIR}"
else
  PKG_SCRIPTS=""
  _info "  script : none (--no-autoload)"
fi

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
# AppleDouble encodings of the com.apple.provenance attribute that macOS 15 and later
# stamp on every file, and that neither `xattr -c` nor `ditto --noextattr` can clear.
# On install the payload is decoded and the attribute is restored on the real file,
# so no ._ file lands on disk. So hide them from the listing.
_info "payload:"
pkgutil --payload-files "${OUT}" | grep -v '/\._' | sed 's/^/  /'

_info "Done: ${OUT}"
_info "Install headlessly:  sudo installer -pkg '${OUT}' -target /"
_info "At first login the agent installs ug for that user, then runs the SSO login."
_info "That needs uv on the device. uv is a prerequisite, and IT owns it."
