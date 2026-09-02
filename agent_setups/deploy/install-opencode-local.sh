#!/usr/bin/env sh
# install-opencode-local.sh — per-user placement for a LOCAL (non-managed) opencode install.
#
# The system install.sh is the root/managed placement authority. It deliberately
# SKIPS user-mode opencode configs, because a per-user file is not a root-managed
# system placement. This script is its complement: it copies a generated
# user-mode opencode.json into the per-user opencode config dir, so a developer
# can route a local opencode through the gateway without root or MDM.
#
# Target (opencode's per-user global config dir):
#   ${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json
#
# Generate the source first (user mode):
#   make agent-opencode ARGS=--user-config           # -> agent_setups/generated/opencode/opencode.json
# or run this through the one-step target:
#   make opencode-install-local
#
# Usage:
#   install-opencode-local.sh [OPTIONS]
#
# Options:
#   --source <file>     Generated user-mode opencode.json
#                       (default: <repo>/agent_setups/generated/opencode/opencode.json)
#   --target-dir <dir>  opencode config dir
#                       (default: ${XDG_CONFIG_HOME:-$HOME/.config}/opencode)
#   --dry-run           Print planned actions, touch nothing (exit 0)
#   --no-backup         Overwrite an existing opencode.json without a timestamped backup
#   --print-target      Print the resolved target file path and exit 0
#   -h, --help          Show this message
#
# Exit codes:
#   0  success / --dry-run / --print-target
#   1  usage error
#   4  source opencode.json not found
#   5  copy or permission failure
set -eu

# ---------------------------------------------------------------------------
# Resolve the repo-relative default source (this file is agent_setups/deploy/…)
# ---------------------------------------------------------------------------
_self_dir="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_SOURCE="${_self_dir}/../generated/opencode/opencode.json"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SOURCE="${DEFAULT_SOURCE}"
TARGET_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode"
DRY_RUN=0
NO_BACKUP=0
PRINT_TARGET=0

# ---------------------------------------------------------------------------
# Logging helpers (match install.sh)
# ---------------------------------------------------------------------------
_info() { printf '[opencode-local] %s\n' "$*"; }
_warn() { printf '[opencode-local] WARN: %s\n' "$*" >&2; }
_fatal() {
  _f_code="$1"; shift
  printf '[opencode-local] FATAL: %s\n' "$*" >&2
  exit "${_f_code}"
}

_usage() {
  cat <<'EOF'
Usage: install-opencode-local.sh [OPTIONS]

Per-user placement for a LOCAL (non-managed) opencode install. Copies a generated
user-mode opencode.json into the per-user opencode config dir. No root, no MDM.

Options:
  --source <file>     Generated user-mode opencode.json
                      (default: <repo>/agent_setups/generated/opencode/opencode.json)
  --target-dir <dir>  opencode config dir (default: ${XDG_CONFIG_HOME:-$HOME/.config}/opencode)
  --dry-run           Print planned actions, touch nothing (exit 0)
  --no-backup         Overwrite an existing opencode.json without a timestamped backup
  --print-target      Print the resolved target file path and exit 0
  -h, --help          Show this message

Exit codes:
  0  success / --dry-run / --print-target
  1  usage error
  4  source opencode.json not found
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

TARGET="${TARGET_DIR}/opencode.json"

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
  _fatal 4 "Source opencode.json not found: ${SOURCE}
  Generate it first: make agent-opencode ARGS=--user-config
  (or run the one-step target: make opencode-install-local)"
fi

# A user-mode config has no .mobileconfig beside it. If one IS present, the source
# is a managed bundle; the JSON is identical, so placement is still valid, but the
# managed dir / MDM profile would override this per-user file. Warn, do not block.
if [ -f "$(dirname "${SOURCE}")/ai.opencode.managed.mobileconfig" ]; then
  _warn "Source dir also holds ai.opencode.managed.mobileconfig (a managed bundle)."
  _warn "  A deployed managed config would override this per-user file. For a pure"
  _warn "  local install, regenerate in user mode: make agent-opencode ARGS=--user-config"
fi

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
_info "=== opencode local install ==="
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

# The auth plugin sits beside the source opencode.json. The user-mode config
# references the plugin by an absolute path. This installer rewrites that path to
# the resolved target dir after it places the plugin (see the rewrite step below).
_plugin_src="$(dirname "${SOURCE}")/databricks-auth.ts"
_plugin_target="${TARGET_DIR}/databricks-auth.ts"

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
  _info "  installed opencode.json"
  if [ -f "${_plugin_src}" ]; then
    cp -- "${_plugin_src}" "${_plugin_target}" || _fatal 5 "copy failed: ${_plugin_src} -> ${_plugin_target}"
    chmod 644 "${_plugin_target}" || _fatal 5 "chmod 644 failed: ${_plugin_target}"
    _info "  installed databricks-auth.ts"
  else
    _warn "databricks-auth.ts not found beside the source; opencode auth will fail without it."
    _warn "  Re-generate: make agent-opencode ARGS=--user-config"
  fi
fi

# ---------------------------------------------------------------------------
# Rewrite the installed opencode.json plugin entry to the resolved target dir.
#
# The generator bakes a best-effort absolute default (its generation-time XDG
# dir). This step rewrites that path to the REAL target dir, so it is correct
# even when --target-dir or XDG_CONFIG_HOME differ from the generation default.
# The rule: replace any plugin entry whose basename is databricks-auth.ts with
# "${TARGET_DIR}/databricks-auth.ts". Leave unrelated entries untouched.
#
# This needs python3 (the generator already requires it). If python3 is not
# present, print a warning and SKIP the rewrite. Do not fail the install: the
# generator's baked default still works for the default dir.
# ---------------------------------------------------------------------------
_abs_plugin="${TARGET_DIR}/databricks-auth.ts"
if command -v python3 >/dev/null 2>&1; then
  # In dry-run nothing was copied, so read the source to plan the change. In a
  # real run, read the installed target file.
  if [ "${DRY_RUN}" = "1" ]; then
    _rewrite_in="${SOURCE}"
  else
    _rewrite_in="${TARGET}"
  fi
  python3 - "${_rewrite_in}" "${TARGET}" "${_abs_plugin}" "${DRY_RUN}" <<'PY' || _warn "plugin rewrite step failed; edit the plugin entry in the target opencode.json by hand if needed."
import json, os, sys

in_path, out_path, abs_plugin, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
try:
    with open(in_path) as f:
        cfg = json.load(f)
except (OSError, ValueError) as exc:
    print("[opencode-local] WARN: could not read %s for plugin rewrite (%s); skipped" % (in_path, exc), file=sys.stderr)
    sys.exit(0)
old = cfg.get("plugin")
if not isinstance(old, list):
    print("[opencode-local] WARN: opencode.json has no plugin array; skipped rewrite", file=sys.stderr)
    sys.exit(0)
new = [
    abs_plugin if isinstance(e, str) and os.path.basename(e) == "databricks-auth.ts" else e
    for e in old
]
if new == old:
    print("[opencode-local]   plugin path already resolved: %s" % (old,))
    sys.exit(0)
if dry:
    print("[opencode-local]   [plan] rewrite plugin %s -> %s" % (old, new))
    sys.exit(0)
cfg["plugin"] = new
with open(out_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print("[opencode-local]   rewrote plugin -> %s" % (new,))
PY
else
  _warn "python3 not found; skipped the absolute-path rewrite of the plugin reference."
  _warn "  The generated default works for the default dir. For a custom target dir,"
  _warn "  edit the plugin entry in \"${TARGET}\" to \"${_abs_plugin}\"."
fi

# ---------------------------------------------------------------------------
# Auth reminder (the plugin mints tokens; the developer logs in once)
# ---------------------------------------------------------------------------
printf '\nLocal install complete. The databricks-auth.ts plugin mints a fresh\n'
printf 'Databricks token on every request, so no environment variable is needed.\n'
printf 'The Databricks CLI refreshes access tokens silently, so routine expiry\n'
printf 'needs no login. Authenticate once (the plugin also auto-runs this if needed):\n'
printf '  databricks auth login --host <host> --profile <profile>\n\n'

exit 0
