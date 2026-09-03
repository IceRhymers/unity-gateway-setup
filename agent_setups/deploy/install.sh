#!/usr/bin/env sh
# install.sh — single runtime placement authority for unity-gateway agent configs.
#
# Deploys generated Claude Code, Codex, and opencode config bundles to system
# paths. Run as root for production deployment, or with --target-root for
# unprivileged staging and unit tests.
#
# Usage:
#   install.sh [OPTIONS]
#
# Options:
#   --dry-run               Print planned actions, touch nothing (exit 0)
#   --agents <list>         Comma-separated: claude-code,codex,opencode  (default: claude-code,codex,opencode)
#   --profile <name>        Databricks profile (default: fevm-west; Phase-B hint only)
#   --source <root>         Tarball root: <root>/claude-code/<os>/ + <root>/codex/ + <root>/opencode/
#                           (default: .)
#   --claude-source <dir>   Dir holding Claude files directly; overrides --source
#                           (managed-settings.json, otel-headers-helper.sh, emit_hook_events.sh)
#   --codex-source <dir>    Dir holding codex/ tree; overrides --source
#                           (etc/managed_config.toml for managed mode)
#   --opencode-source <dir> Dir holding opencode/ tree; overrides --source
#                           (opencode.json + ai.opencode.managed.mobileconfig for managed mode)
#   --target-root <prefix>  Install prefix for unprivileged staging (default: "")
#   --os macos|linux        Force OS (default: autodetect via uname -s)
#   --agent claude-code|codex|opencode  Pair with --print-target-dir
#   --print-target-dir      Print resolved install dir for --os/--agent and exit 0
#   --uninstall             Remove placed files and version marker; exit 0 on success
#   --smoke-test            After install, probe the gateway endpoint (exit 7 on non-200)
#   --host <url>            Gateway base URL for --smoke-test (default: derived from installed configs)
#   -h, --help              Show this message
#
# Exit codes:
#   0  success / --dry-run / --print-target-dir / --uninstall (all removed)
#   1  usage error
#   2  not root (and --target-root not set)
#   3  critical prerequisite missing
#   4  required source file missing (managed-settings.json)
#   5  copy or permission failure
#   6  uninstall: file removal or marker removal failed (marker left intact for retry)
#   7  smoke test: gateway returned a non-200 response
set -eu

# ---------------------------------------------------------------------------
# Defaults — TARGET_ROOT initialized empty; always expand as ${TARGET_ROOT:-}
# ---------------------------------------------------------------------------
TARGET_ROOT=""
DRY_RUN=0
AGENTS="claude-code,codex,opencode"
PROFILE="fevm-west"
SOURCE="."
CLAUDE_SOURCE=""
CODEX_SOURCE=""
OPENCODE_SOURCE=""
OS_OVERRIDE=""
PRINT_AGENT=""
PRINT_TARGET_DIR=0
UNINSTALL=0
SMOKE_TEST=0
HOST=""
_BACKUP_SEQ=0
_smoke_failed=0

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
_log()  { printf '[install] %s\n' "$*" >&2; }
_info() { printf '[install] %s\n' "$*"; }
_warn() { printf '[install] WARN: %s\n' "$*" >&2; }
_fatal() {
  _f_code="$1"; shift
  printf '[install] FATAL: %s\n' "$*" >&2
  exit "${_f_code}"
}

# Returns 0 if <needle> is a comma-delimited token in <haystack>.
_contains() {
  case ",$1," in
    *",$2,"*) return 0 ;;
    *)        return 1 ;;
  esac
}

_usage() {
  cat <<'EOF'
Usage: install.sh [OPTIONS]

Single root-run placement authority for unity-gateway agent configs.
Deploys Claude Code, Codex, and opencode config bundles to system paths.
Run as root (production) or with --target-root (unprivileged staging).

Options:
  --dry-run               Print planned actions, touch nothing (exit 0)
  --agents <list>         Comma-separated agents: claude-code,codex,opencode (default: claude-code,codex,opencode)
  --profile <name>        Databricks profile name (default: fevm-west; Phase-B hint only)
  --source <root>         Tarball root: expects <root>/claude-code/<os>/, <root>/codex/, <root>/opencode/
                          (default: .)
  --claude-source <dir>   Dir holding Claude files directly (overrides --source)
  --codex-source <dir>    Dir holding codex/ tree (overrides --source)
  --opencode-source <dir> Dir holding opencode/ tree (overrides --source)
  --target-root <prefix>  Install prefix for unprivileged staging (default: "")
  --os macos|linux        Force OS (default: autodetect via uname -s)
  --agent claude-code|codex|opencode
                          Pair with --print-target-dir to select which agent
  --print-target-dir      Print resolved install dir for --os/--agent, then exit 0
  --uninstall             Remove placed files and version marker (needs --os and --target-root;
                          does not require --source). Exit 6 on removal failure.
  --smoke-test            After install, POST a ping to the gateway. Exit 7 on non-200.
                          Requires curl and a valid token. Skipped if host or token
                          cannot be resolved.
  --host <url>            Gateway base URL for --smoke-test (default: derived from
                          ANTHROPIC_BASE_URL in managed-settings.json or equivalent)
  -h, --help              Show this message

Exit codes:
  0  success / --dry-run / --print-target-dir / --uninstall (all removed)
  1  usage error
  2  not root (and --target-root not set)
  3  critical prereq missing
  4  required source file missing (managed-settings.json)
  5  copy/permission failure
  6  uninstall: removal or marker failure (marker left intact for retry)
  7  smoke test: gateway returned non-200
EOF
  exit 1
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)           DRY_RUN=1 ;;
    --agents)            shift; AGENTS="${1:?--agents requires a value}" ;;
    --profile)           shift; PROFILE="${1:?--profile requires a value}" ;;
    --source)            shift; SOURCE="${1:?--source requires a value}" ;;
    --claude-source)     shift; CLAUDE_SOURCE="${1:?--claude-source requires a value}" ;;
    --codex-source)      shift; CODEX_SOURCE="${1:?--codex-source requires a value}" ;;
    --opencode-source)   shift; OPENCODE_SOURCE="${1:?--opencode-source requires a value}" ;;
    --target-root)       shift; TARGET_ROOT="${1:?--target-root requires a value}" ;;
    --os)                shift; OS_OVERRIDE="${1:?--os requires a value}" ;;
    --agent)             shift; PRINT_AGENT="${1:?--agent requires a value}" ;;
    --print-target-dir)  PRINT_TARGET_DIR=1 ;;
    --uninstall)         UNINSTALL=1 ;;
    --smoke-test)        SMOKE_TEST=1 ;;
    --host)              shift; HOST="${1:?--host requires a value}" ;;
    -h|--help)           _usage ;;
    *)                   _warn "Unknown option: $1"; _usage ;;
  esac
  shift
done

# Validate --agents tokens up front so a typo (e.g. "claude" instead of
# "claude-code") fails loudly rather than installing nothing and exiting 0.
_agents_rest="${AGENTS}"
while [ -n "${_agents_rest}" ]; do
  case "${_agents_rest}" in
    *,*) _agent_tok="${_agents_rest%%,*}"; _agents_rest="${_agents_rest#*,}" ;;
    *)   _agent_tok="${_agents_rest}";     _agents_rest="" ;;
  esac
  case "${_agent_tok}" in
    claude-code|codex|opencode) ;;
    "") ;;
    *) _fatal 1 "Unknown agent in --agents: '${_agent_tok}' (valid: claude-code, codex, opencode)." ;;
  esac
done

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
if [ -n "${OS_OVERRIDE}" ]; then
  OS="${OS_OVERRIDE}"
else
  _uname_s="$(uname -s 2>/dev/null || printf 'unknown')"
  case "${_uname_s}" in
    Darwin) OS="macos" ;;
    Linux)  OS="linux" ;;
    *)      _fatal 1 "Unsupported OS '${_uname_s}'. Pass --os macos|linux." ;;
  esac
fi
case "${OS}" in
  macos|linux) ;;
  *) _fatal 1 "Invalid --os value '${OS}'. Must be macos or linux." ;;
esac

# ---------------------------------------------------------------------------
# Install path matrix
# ---------------------------------------------------------------------------
# macos claude:    "/Library/Application Support/ClaudeCode"  (NOTE: space — quote always)
# linux claude:    "/etc/claude-code"
# codex both:      "/etc/codex"
# macos opencode:  "/Library/Application Support/opencode"     (NOTE: space — quote always)
# linux opencode:  "/etc/opencode"
_raw_dir_for() {
  # Usage: _raw_dir_for <os> <agent>
  # Prints the raw install dir (no TARGET_ROOT prefix) without a trailing newline.
  case "$2" in
    claude-code)
      case "$1" in
        macos) printf '/Library/Application Support/ClaudeCode' ;;
        linux) printf '/etc/claude-code' ;;
      esac ;;
    codex)
      printf '/etc/codex' ;;
    opencode)
      case "$1" in
        macos) printf '/Library/Application Support/opencode' ;;
        linux) printf '/etc/opencode' ;;
      esac ;;
    *)
      printf '[install] ERROR: Unknown agent for path matrix: %s\n' "$2" >&2
      exit 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# --print-target-dir (consumed by AC-A1 static assertion)
# Prints the SAME path the installer would write into, honoring --target-root.
# ---------------------------------------------------------------------------
if [ "${PRINT_TARGET_DIR}" = "1" ]; then
  if [ -z "${PRINT_AGENT}" ]; then
    _fatal 1 "--print-target-dir requires --agent <claude-code|codex|opencode>"
  fi
  _ptd_raw="$(_raw_dir_for "${OS}" "${PRINT_AGENT}")"
  printf '%s\n' "${TARGET_ROOT:-}${_ptd_raw}"
  exit 0
fi

# ---------------------------------------------------------------------------
# Root check
# ---------------------------------------------------------------------------
_uid="$(id -u)"
if [ "${_uid}" != "0" ] && [ -z "${TARGET_ROOT}" ]; then
  _fatal 2 "Must run as root, or pass --target-root <prefix> for unprivileged staging."
fi

# Staging mode: non-root with --target-root set — skip all chown, still apply chmod.
_staging=0
if [ -n "${TARGET_ROOT}" ] && [ "${_uid}" != "0" ]; then
  _staging=1
fi

# ---------------------------------------------------------------------------
# OS-specific owner (root:root on linux, root:wheel on macOS)
# ---------------------------------------------------------------------------
case "${OS}" in
  linux) _owner="root:root" ;;
  macos) _owner="root:wheel" ;;
esac

# ---------------------------------------------------------------------------
# Source directory resolution
# ---------------------------------------------------------------------------
if [ -n "${CLAUDE_SOURCE}" ]; then
  _claude_src="${CLAUDE_SOURCE}"
else
  _claude_src="${SOURCE}/claude-code/${OS}"
fi

if [ -n "${CODEX_SOURCE}" ]; then
  _codex_src="${CODEX_SOURCE}"
else
  _codex_src="${SOURCE}/codex"
fi

if [ -n "${OPENCODE_SOURCE}" ]; then
  _opencode_src="${OPENCODE_SOURCE}"
else
  _opencode_src="${SOURCE}/opencode"
fi

# ---------------------------------------------------------------------------
# Resolve install.sh's own path (for installer_sha in the version marker)
# ---------------------------------------------------------------------------
_self_dir="$(cd "$(dirname "$0")" && pwd)"
_self="${_self_dir}/$(basename "$0")"

# ---------------------------------------------------------------------------
# Action wrappers (dry-run aware)
# ---------------------------------------------------------------------------
_action_mkdir() {
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] mkdir -p  \"$1\""
  else
    mkdir -p -- "$1" || _fatal 5 "mkdir failed: $1"
  fi
}

# _backup_path <dest>
# Prints a backup path: <dest>.bak-<UTC>-<pid>-<seq>.
# Uses a run-global _BACKUP_SEQ counter; increments until the path is free.
_backup_path() {
  _bp_ts="$(date -u '+%Y%m%dT%H%M%SZ' 2>/dev/null || date -u | tr ' :' '__')"
  _BACKUP_SEQ=$(( _BACKUP_SEQ + 1 ))
  _bp_base="$1.bak-${_bp_ts}-$$-${_BACKUP_SEQ}"
  while [ -e "${_bp_base}" ]; do
    _BACKUP_SEQ=$(( _BACKUP_SEQ + 1 ))
    _bp_base="$1.bak-${_bp_ts}-$$-${_BACKUP_SEQ}"
  done
  printf '%s' "${_bp_base}"
}

_action_copy() {
  # $1=src  $2=dest
  # Identical content: skip (no backup, no copy).
  if [ -e "$2" ] && cmp -s "$1" "$2"; then
    _info "  unchanged: \"$2\""
    return 0
  fi
  if [ "${DRY_RUN}" = "1" ]; then
    if [ -e "$2" ]; then
      _info "  [plan] backup  \"$2\""
    fi
    _info "  [plan] copy  \"$1\"  ->  \"$2\""
  else
    if [ -e "$2" ]; then
      _ac_bak="$(_backup_path "$2")"
      cp -p -- "$2" "${_ac_bak}" || _fatal 5 "backup failed: $2 -> ${_ac_bak}"
    fi
    cp -- "$1" "$2" || _fatal 5 "copy failed: $1 -> $2"
  fi
}

_action_chmod() {
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] chmod $1  \"$2\""
  else
    chmod -- "$1" "$2" || _fatal 5 "chmod $1 failed: $2"
  fi
}

_action_chown() {
  # $1 = owner:group, $2 = path
  if [ "${DRY_RUN}" = "1" ]; then
    if [ "${_staging}" = "1" ]; then
      _info "  [plan] chown -R $1  \"$2\"  (skipped: non-root staging)"
    else
      _info "  [plan] chown -R $1  \"$2\""
    fi
  else
    if [ "${_staging}" = "1" ]; then
      _log "[staging] non-root: skipping ownership changes for \"$2\""
    else
      chown -R -- "$1" "$2" || _fatal 5 "chown $1 failed: $2"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Uninstall helpers
# ---------------------------------------------------------------------------

# _default_files_for <agent> <install_dir>
# Prints mandatory basenames unconditionally, then probes for optional ones.
_default_files_for() {
  _dff_agent="$1"
  _dff_dir="$2"
  case "${_dff_agent}" in
    claude-code)
      printf 'managed-settings.json'
      [ -e "${_dff_dir}/otel-headers-helper.sh" ] && printf ' otel-headers-helper.sh'
      [ -e "${_dff_dir}/emit_hook_events.sh" ]    && printf ' emit_hook_events.sh'
      ;;
    codex)
      printf 'managed_config.toml requirements.toml'
      [ -e "${_dff_dir}/emit_hook_events.sh" ] && printf ' emit_hook_events.sh'
      ;;
    opencode)
      printf 'opencode.json'
      [ -e "${_dff_dir}/databricks-auth.ts" ]              && printf ' databricks-auth.ts'
      [ -e "${_dff_dir}/ai.opencode.managed.mobileconfig" ] && printf ' ai.opencode.managed.mobileconfig'
      ;;
  esac
  printf '\n'
}

# _uninstall_agent <agent>
# Reads the placed files from the version marker and removes them.
# Removes the marker last. Never uses rm -rf. Preserves .bak-* files.
_uninstall_agent() {
  _ua_agent="$1"
  _ua_raw="$(_raw_dir_for "${OS}" "${_ua_agent}")"
  _ua_dir="${TARGET_ROOT:-}${_ua_raw}"
  _ua_marker="${_ua_dir}/.unity-gateway-version"

  _info ""
  _info "=== Uninstall: ${_ua_agent} ==="
  _info "  target : ${_ua_dir}"

  if [ ! -f "${_ua_marker}" ]; then
    _warn "No version marker at '${_ua_marker}'. Nothing placed by install.sh."
    return 0
  fi

  # Read files= from marker; fall back to probing when the line is absent.
  _ua_files="$(grep '^files=' "${_ua_marker}" 2>/dev/null | cut -d= -f2- || true)"
  if [ -z "${_ua_files}" ]; then
    _warn "Marker has no files= line. Using probing fallback."
    _ua_files="$(_default_files_for "${_ua_agent}" "${_ua_dir}")"
  fi

  _ua_failed=0
  for _ua_f in ${_ua_files}; do
    _ua_path="${_ua_dir}/${_ua_f}"
    if [ ! -e "${_ua_path}" ]; then
      _info "  skip   : \"${_ua_path}\" (not present)"
      continue
    fi
    if [ "${DRY_RUN}" = "1" ]; then
      _info "  [plan] rm  \"${_ua_path}\""
    else
      if rm -f -- "${_ua_path}" 2>/dev/null; then
        _info "  removed: \"${_ua_path}\""
      else
        _warn "Failed to remove '${_ua_path}'"
        _ua_failed=1
      fi
    fi
  done

  if [ "${_ua_failed}" = "1" ]; then
    _fatal 6 "Uninstall incomplete: some files could not be removed. Marker left intact for retry."
  fi

  # Remove the marker last.
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  [plan] rm  \"${_ua_marker}\""
  else
    rm -f -- "${_ua_marker}" || _fatal 6 "Failed to remove marker: ${_ua_marker}"
    _info "  removed: marker"
    # Remove the directory only when it is empty (non-fatal if not empty).
    if rmdir -- "${_ua_dir}" 2>/dev/null; then
      _info "  removed empty dir: \"${_ua_dir}\""
    else
      _warn "Left '${_ua_dir}' in place (not empty — user files remain)."
    fi
  fi
}

# ---------------------------------------------------------------------------
# Version marker
# ---------------------------------------------------------------------------
_write_version_marker() {
  # $1=agent  $2=install_dir  $3=source_dir_for_marker  $4=space-separated placed basenames
  _wvm_agent="$1"
  _wvm_dir="$2"
  _wvm_src="$3"
  _wvm_files="${4:-}"
  _wvm_marker="${_wvm_dir}/.unity-gateway-version"

  # Read packaged VERSION if present (written by make deploy-package)
  _wvm_version="unknown"
  if [ -f "${SOURCE}/VERSION" ]; then
    _wvm_version="$(cat "${SOURCE}/VERSION")"
  fi

  # Compute sha of install.sh itself
  _wvm_sha="unknown"
  if command -v sha256sum >/dev/null 2>&1; then
    _wvm_sha="$(sha256sum "${_self}" 2>/dev/null | awk '{print $1}')" || _wvm_sha="unknown"
  elif command -v shasum >/dev/null 2>&1; then
    _wvm_sha="$(shasum -a 256 "${_self}" 2>/dev/null | awk '{print $1}')" || _wvm_sha="unknown"
  fi

  # Determine log message (installed / upgraded X->Y / unchanged X)
  _wvm_prev=""
  _wvm_prev_files=""
  if [ -f "${_wvm_marker}" ]; then
    _wvm_prev="$(grep '^version=' "${_wvm_marker}" 2>/dev/null | cut -d= -f2 || true)"
    _wvm_prev_files="$(grep '^files=' "${_wvm_marker}" 2>/dev/null | cut -d= -f2- || true)"
  fi

  # Union previously-recorded files with the current set so --uninstall never
  # orphans files dropped from this bundle (e.g. emit_hook_events.sh on a
  # telemetry-off re-install over a telemetry-on one).
  for _wvm_pf in ${_wvm_prev_files}; do
    case " ${_wvm_files} " in
      *" ${_wvm_pf} "*) ;; # already present
      *) _wvm_files="${_wvm_files:+${_wvm_files} }${_wvm_pf}" ;;
    esac
  done

  _wvm_now="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u)"

  if [ "${DRY_RUN}" = "0" ]; then
    cat > "${_wvm_marker}" <<EOF
version=${_wvm_version}
installed_at=${_wvm_now}
agent=${_wvm_agent}
os=${OS}
source=${_wvm_src}
installer_sha=${_wvm_sha}
files=${_wvm_files}
EOF
    chmod 644 "${_wvm_marker}"
  fi

  if [ -z "${_wvm_prev}" ]; then
    _info "  marker : installed (${_wvm_version})"
  elif [ "${_wvm_version}" = "${_wvm_prev}" ]; then
    _info "  marker : unchanged (${_wvm_version})"
  else
    _info "  marker : upgraded (${_wvm_prev} -> ${_wvm_version})"
  fi
}

# ---------------------------------------------------------------------------
# Prerequisite check — report, don't fix; critical absence → exit 3
# ---------------------------------------------------------------------------
_check_prereqs() {
  _info ""
  _info "=== Prerequisite check ==="

  # Determine whether emit_hook_events.sh will be installed (gates jq/curl criticality)
  _prereq_emit=0
  if _contains "${AGENTS}" "claude-code" && [ -f "${_claude_src}/emit_hook_events.sh" ]; then
    _prereq_emit=1
  fi
  if _contains "${AGENTS}" "codex" && [ -f "${_codex_src}/etc/emit_hook_events.sh" ]; then
    _prereq_emit=1
  fi

  _prereq_missing=""

  # _one_check <cmd> <reason> <critical|info>
  # Appends cmd to _prereq_missing if critical and absent.
  _one_check() {
    _oc_cmd="$1" _oc_reason="$2" _oc_level="$3"
    if command -v "${_oc_cmd}" >/dev/null 2>&1; then
      printf '  %-12s  FOUND     %s\n' "${_oc_cmd}" "${_oc_reason}"
    else
      printf '  %-12s  MISSING   %s\n' "${_oc_cmd}" "${_oc_reason}"
      if [ "${_oc_level}" = "critical" ]; then
        _prereq_missing="${_prereq_missing}${_prereq_missing:+ }${_oc_cmd}"
      fi
    fi
  }

  # python3 is critical because both agents' auth helpers call 'python3 -c' on every
  # token mint — EXCEPT in a DATABRICKS_BEARER-only deployment, where the helpers
  # short-circuit to `printf %s "$DATABRICKS_BEARER"` and never invoke python3.
  if [ -n "${DATABRICKS_BEARER:-}" ]; then
    _one_check python3 \
      "auth helpers use \$DATABRICKS_BEARER (set); python3 not required in bearer-only mode." \
      info
  else
    _one_check python3 \
      "auth helpers call 'python3 -c' on every token mint (both agents); \$DATABRICKS_BEARER not set." \
      critical
  fi
  _one_check databricks \
    "Databricks CLI: required for token minting via 'databricks auth token'." \
    critical

  if [ "${_prereq_emit}" = "1" ]; then
    _one_check jq   "emit_hook_events.sh: builds JSON payloads [bundle contains emitter — critical]" critical
    _one_check curl "emit_hook_events.sh: sends Zerobus REST events [bundle contains emitter — critical]" critical
  else
    _one_check jq   "emit_hook_events.sh: builds JSON payloads [emitter not in bundle — informational]" info
    _one_check curl "emit_hook_events.sh: sends Zerobus REST events [emitter not in bundle — informational]" info
  fi

  if [ -n "${_prereq_missing}" ]; then
    if [ "${DRY_RUN}" = "1" ]; then
      _warn "Critical prerequisites MISSING (would exit 3 in real run): ${_prereq_missing}"
    else
      _fatal 3 "Critical prerequisites missing: ${_prereq_missing}"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Claude Code installation
# ---------------------------------------------------------------------------
_install_claude() {
  _cc_raw="$(_raw_dir_for "${OS}" "claude-code")"
  _cc_dir="${TARGET_ROOT:-}${_cc_raw}"
  _cc_src="${_claude_src}"

  _info ""
  _info "=== Claude Code (${OS}) ==="
  _info "  source : ${_cc_src}"
  _info "  target : ${_cc_dir}"

  # managed-settings.json is REQUIRED (exit 4 if missing)
  if [ ! -f "${_cc_src}/managed-settings.json" ]; then
    _fatal 4 "Required file not found: ${_cc_src}/managed-settings.json"
  fi

  _action_mkdir "${_cc_dir}"

  _action_copy  "${_cc_src}/managed-settings.json" "${_cc_dir}/managed-settings.json"
  _action_chmod 644 "${_cc_dir}/managed-settings.json"
  _cc_files="managed-settings.json"

  # Optional: otel-headers-helper.sh
  if [ -f "${_cc_src}/otel-headers-helper.sh" ]; then
    _action_copy  "${_cc_src}/otel-headers-helper.sh" "${_cc_dir}/otel-headers-helper.sh"
    _action_chmod 755 "${_cc_dir}/otel-headers-helper.sh"
    _cc_files="${_cc_files} otel-headers-helper.sh"
  fi

  # Optional: emit_hook_events.sh
  if [ -f "${_cc_src}/emit_hook_events.sh" ]; then
    _action_copy  "${_cc_src}/emit_hook_events.sh" "${_cc_dir}/emit_hook_events.sh"
    _action_chmod 755 "${_cc_dir}/emit_hook_events.sh"
    _cc_files="${_cc_files} emit_hook_events.sh"
  fi

  _action_chown "${_owner}" "${_cc_dir}"

  _write_version_marker "claude-code" "${_cc_dir}" "${_cc_src}" "${_cc_files}"
}

# ---------------------------------------------------------------------------
# Codex installation
# ---------------------------------------------------------------------------
_install_codex() {
  _cx_raw="$(_raw_dir_for "${OS}" "codex")"
  _cx_dir="${TARGET_ROOT:-}${_cx_raw}"
  _cx_src="${_codex_src}"

  _info ""
  _info "=== Codex (${OS}) ==="
  _info "  source : ${_cx_src}"
  _info "  target : ${_cx_dir}"

  if [ -f "${_cx_src}/etc/managed_config.toml" ]; then
    # MANAGED mode: root-owned /etc/codex enforcement bundle
    _info "  mode   : managed (enterprise)"
    _cx_etc="${_cx_src}/etc"

    _action_mkdir "${_cx_dir}"

    _action_copy  "${_cx_etc}/managed_config.toml" "${_cx_dir}/managed_config.toml"
    _action_chmod 644 "${_cx_dir}/managed_config.toml"

    _action_copy  "${_cx_etc}/requirements.toml"   "${_cx_dir}/requirements.toml"
    _action_chmod 644 "${_cx_dir}/requirements.toml"
    _cx_files="managed_config.toml requirements.toml"

    if [ -f "${_cx_etc}/emit_hook_events.sh" ]; then
      _action_copy  "${_cx_etc}/emit_hook_events.sh" "${_cx_dir}/emit_hook_events.sh"
      _action_chmod 755 "${_cx_dir}/emit_hook_events.sh"
      _cx_files="${_cx_files} emit_hook_events.sh"
    fi

    _action_chown "${_owner}" "${_cx_dir}"

    _write_version_marker "codex" "${_cx_dir}" "${_cx_etc}" "${_cx_files}"

  elif [ -f "${_cx_src}/config.toml" ]; then
    # USER mode: per-user $CODEX_HOME — not a root-managed system placement
    _warn "Codex source '${_cx_src}/config.toml' is user-mode (config.toml at bundle root)."
    _warn "User-mode config is NOT a root-managed system placement; skipping."
    _warn "Re-generate with managed mode (--telemetry on --hook-telemetry on) for system deploy."
    _warn "Container/MDM deployments require etc/managed_config.toml."
  else
    _warn "No Codex config found at '${_cx_src}'."
    _warn "  Checked: ${_cx_src}/etc/managed_config.toml  (managed mode)"
    _warn "  Checked: ${_cx_src}/config.toml               (user mode — would be skipped anyway)"
    _warn "Run 'make agent-codex' to generate."
  fi
}

# ---------------------------------------------------------------------------
# opencode installation
# ---------------------------------------------------------------------------
_install_opencode() {
  _oc_raw="$(_raw_dir_for "${OS}" "opencode")"
  _oc_dir="${TARGET_ROOT:-}${_oc_raw}"
  _oc_src="${_opencode_src}"

  _info ""
  _info "=== opencode (${OS}) ==="
  _info "  source : ${_oc_src}"
  _info "  target : ${_oc_dir}"

  # Managed mode is signaled by the macOS Configuration Profile alongside opencode.json.
  # Both modes emit opencode.json, so the .mobileconfig is the managed-vs-user signal.
  if [ -f "${_oc_src}/ai.opencode.managed.mobileconfig" ]; then
    # MANAGED mode: root-owned per-OS managed config dir. opencode reads managed
    # config LAST and it overrides user config.
    _info "  mode   : managed (enterprise)"

    _action_mkdir "${_oc_dir}"

    _action_copy  "${_oc_src}/opencode.json" "${_oc_dir}/opencode.json"
    _action_chmod 644 "${_oc_dir}/opencode.json"
    _oc_files="opencode.json"

    # The auth plugin. opencode.json references it by a relative path, so it must
    # sit beside opencode.json in the managed dir. The .mobileconfig references
    # the same file by its absolute macOS path.
    if [ -f "${_oc_src}/databricks-auth.ts" ]; then
      _action_copy  "${_oc_src}/databricks-auth.ts" "${_oc_dir}/databricks-auth.ts"
      _action_chmod 644 "${_oc_dir}/databricks-auth.ts"
      _oc_files="${_oc_files} databricks-auth.ts"
    else
      _warn "opencode: databricks-auth.ts not found in '${_oc_src}'."
      _warn "  The config references it, so opencode auth will fail without it."
      _warn "  Re-generate with 'make agent-opencode'."
    fi

    # macOS hard-lock profile. An MDM normally DELIVERS the profile to
    # /Library/Managed Preferences/ai.opencode.managed.plist (Jamf, or
    # `profiles install`). Here we stage it into the managed dir so a manual or
    # staged install has it on disk. Non-macOS fleets rely on opencode.json only.
    if [ "${OS}" = "macos" ]; then
      _action_copy  "${_oc_src}/ai.opencode.managed.mobileconfig" "${_oc_dir}/ai.opencode.managed.mobileconfig"
      _action_chmod 644 "${_oc_dir}/ai.opencode.managed.mobileconfig"
      _oc_files="${_oc_files} ai.opencode.managed.mobileconfig"
      _warn "opencode: staged ai.opencode.managed.mobileconfig into \"${_oc_dir}\"."
      _warn "  An MDM must INSTALL this profile to activate the macOS hard-lock at"
      _warn "  /Library/Managed Preferences/ai.opencode.managed.plist (Jamf, or"
      _warn "  'profiles install -path <file>'). Staging it here alone does not activate it."
    fi

    _action_chown "${_owner}" "${_oc_dir}"

    _write_version_marker "opencode" "${_oc_dir}" "${_oc_src}" "${_oc_files}"

  elif [ -f "${_oc_src}/opencode.json" ]; then
    # USER mode: per-user ~/.config/opencode — not a root-managed system placement
    _warn "opencode source '${_oc_src}/opencode.json' is user-mode (no .mobileconfig)."
    _warn "User-mode config is NOT a root-managed system placement; skipping."
    _warn "Re-generate without --user-config for a managed system deploy."
  else
    _warn "No opencode config found at '${_oc_src}'."
    _warn "  Checked: ${_oc_src}/ai.opencode.managed.mobileconfig  (managed mode signal)"
    _warn "  Checked: ${_oc_src}/opencode.json                     (user mode — would be skipped anyway)"
    _warn "Run 'make agent-opencode' to generate."
  fi
}

# ---------------------------------------------------------------------------
# Smoke-test helpers
# ---------------------------------------------------------------------------

# _scheme_authority <url>
# Prints scheme + authority (https://host[:port]) with the path stripped.
_scheme_authority() {
  # shellcheck disable=SC3001
  printf '%s' "$1" | sed -E 's#^(https?://[^/]+).*#\1#'
}

# _derive_host
# Derives the gateway base URL from installed agent configs.
# Precedence: claude-code ANTHROPIC_BASE_URL > codex base_url > opencode baseURL.
# Only URLs containing /ai-gateway/ are accepted. Non-gateway URLs ($schema,
# opencode.ai, anthropic.com docs) are skipped. Returns empty string if no
# gateway URL is found; the caller must skip the smoke test gracefully.
_derive_host() {
  _dh_result=""

  if _contains "${AGENTS}" "claude-code"; then
    _dh_cc_dir="${TARGET_ROOT:-}$(_raw_dir_for "${OS}" "claude-code")"
    _dh_cc_file="${_dh_cc_dir}/managed-settings.json"
    if [ -f "${_dh_cc_file}" ]; then
      _dh_result="$(grep 'ANTHROPIC_BASE_URL' "${_dh_cc_file}" | grep -o 'https://[^"]*' | grep '/ai-gateway/' | head -n1 || true)"
    fi
  fi

  if [ -z "${_dh_result}" ] && _contains "${AGENTS}" "codex"; then
    _dh_cx_dir="${TARGET_ROOT:-}$(_raw_dir_for "${OS}" "codex")"
    _dh_cx_file="${_dh_cx_dir}/managed_config.toml"
    if [ -f "${_dh_cx_file}" ]; then
      _dh_result="$(grep 'base_url' "${_dh_cx_file}" | grep -o 'https://[^"]*' | grep '/ai-gateway/' | head -n1 || true)"
    fi
  fi

  if [ -z "${_dh_result}" ] && _contains "${AGENTS}" "opencode"; then
    _dh_oc_dir="${TARGET_ROOT:-}$(_raw_dir_for "${OS}" "opencode")"
    _dh_oc_file="${_dh_oc_dir}/opencode.json"
    if [ -f "${_dh_oc_file}" ]; then
      _dh_result="$(grep -o 'https://[^"]*' "${_dh_oc_file}" | grep '/ai-gateway/' | head -n1 || true)"
    fi
  fi

  printf '%s' "${_dh_result}"
}

# _derive_model
# Derives a real deployed model ID from installed agent configs.
# Precedence: codex managed_config.toml model= > claude managed-settings.json "model" field.
# Returns empty string if no model can be derived; the caller must skip the probe.
_derive_model() {
  _dm_result=""

  if _contains "${AGENTS}" "codex"; then
    _dm_cx_dir="${TARGET_ROOT:-}$(_raw_dir_for "${OS}" "codex")"
    _dm_cx_file="${_dm_cx_dir}/managed_config.toml"
    if [ -f "${_dm_cx_file}" ]; then
      _dm_result="$(grep '^model' "${_dm_cx_file}" | head -n1 | sed 's/model[[:space:]]*=[[:space:]]*"//;s/".*//' || true)"
    fi
  fi

  if [ -z "${_dm_result}" ] && _contains "${AGENTS}" "claude-code"; then
    _dm_cc_dir="${TARGET_ROOT:-}$(_raw_dir_for "${OS}" "claude-code")"
    _dm_cc_file="${_dm_cc_dir}/managed-settings.json"
    if [ -f "${_dm_cc_file}" ]; then
      _dm_result="$(sed -n 's/.*"model"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${_dm_cc_file}" | head -n1 || true)"
    fi
  fi

  printf '%s' "${_dm_result}"
}

# _smoke_test
# POSTs a minimal ping to the gateway. Sets _smoke_failed=1 on non-200.
# Skips gracefully when host, curl, or token are unavailable.
_smoke_test() {
  _st_host="${HOST:-$(_derive_host)}"
  if [ -z "${_st_host}" ]; then
    _warn "smoke test: no host resolved; skipping (pass --host to set explicitly)."
    return 0
  fi
  _st_host="$(_scheme_authority "${_st_host}")"

  if ! command -v curl >/dev/null 2>&1; then
    _warn "smoke test: curl not found; skipping."
    return 0
  fi

  _st_token="${DATABRICKS_BEARER:-}"
  if [ -z "${_st_token}" ]; then
    _st_token="$(databricks auth token --host "${_st_host}" --profile "${PROFILE}" --output json 2>/dev/null \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])' 2>/dev/null \
      || true)"
  fi
  if [ -z "${_st_token}" ]; then
    _warn "smoke test: no token available; skipping."
    return 0
  fi

  _st_model="${SMOKE_MODEL:-}"
  if [ -z "${_st_model}" ]; then
    _st_model="$(_derive_model)"
  fi
  if [ -z "${_st_model}" ]; then
    _warn "smoke test: no model resolved; skipping (set SMOKE_MODEL=<model-id> to probe)."
    return 0
  fi

  _st_url="${_st_host}/ai-gateway/mlflow/v1/chat/completions"
  # Pass the bearer token via curl --config - (stdin) so it never appears in
  # process argv, which would be visible to other processes on the same machine.
  _st_code="$(printf 'header = "Authorization: Bearer %s"\n' "${_st_token}" \
    | curl -sS -o /dev/null -w '%{http_code}' \
      --config - \
      -X POST "${_st_url}" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"${_st_model}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}" \
    || printf '000')"

  if [ "${_st_code}" = "200" ]; then
    _info "  smoke test: OK (${_st_url} -> HTTP ${_st_code})"
  else
    _warn "smoke test: non-200 response from ${_st_url} (HTTP ${_st_code})"
    _smoke_failed=1
  fi
}

# ---------------------------------------------------------------------------
# Post-install verification (never fatal on its own; smoke test may set
# _smoke_failed=1, which is checked in main after _verify returns)
# ---------------------------------------------------------------------------
_verify() {
  _info ""
  _info "=== Verification ==="

  # Dry-run must touch nothing: skip external tools (codex doctor may write cache/state).
  if [ "${DRY_RUN}" = "1" ]; then
    _info "  (dry-run: skipping verification)"
    return 0
  fi

  if _contains "${AGENTS}" "codex"; then
    if command -v codex >/dev/null 2>&1; then
      _info "  Running codex doctor..."
      codex doctor 2>&1 | sed 's/^/    /' || true
    else
      _info "  codex: not on PATH; skipping codex doctor."
    fi
  fi

  if _contains "${AGENTS}" "claude-code"; then
    _info "  Claude Code: run '/status' in-session to verify managed settings are active."
  fi

  if [ "${SMOKE_TEST}" = "1" ]; then
    _smoke_test
  else
    _info "  smoke test: not run (pass --smoke-test to probe the gateway)."
  fi
}

# ---------------------------------------------------------------------------
# Phase-B handoff (printed last — per plan §4 exact text)
# ---------------------------------------------------------------------------
_phase_b_handoff() {
  printf '\nConfig placement complete (Phase A). Per-user step remaining (Phase B):\n'
  printf '  Each developer must run ONCE, interactively:\n'
  printf '    databricks auth login --host <host> --profile %s\n' "${PROFILE}"
  printf '  Browser OAuth (U2M) -- CANNOT be pushed by MDM.\n'
  # shellcheck disable=SC2016  # backticks are literal slash-command notation, not expansion
  printf '  Verify: `/status` in Claude Code; `codex doctor` for Codex.\n\n'
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
_info "=== unity-gateway install.sh ==="
_info "  os         : ${OS}"
_info "  agents     : ${AGENTS}"
_info "  target-root: '${TARGET_ROOT:-}'"
_info "  dry-run    : ${DRY_RUN}"
if [ "${DRY_RUN}" = "1" ]; then
  _info "  (DRY-RUN MODE -- no files will be written)"
fi

# ---------------------------------------------------------------------------
# Uninstall short-circuit — runs before _check_prereqs; needs only --os and
# --target-root. Does not require --source.
# ---------------------------------------------------------------------------
if [ "${UNINSTALL}" = "1" ]; then
  _info "  mode       : UNINSTALL"
  for _main_agent in claude-code codex opencode; do
    if _contains "${AGENTS}" "${_main_agent}"; then
      _uninstall_agent "${_main_agent}"
    fi
  done
  exit 0
fi

_check_prereqs

if _contains "${AGENTS}" "claude-code"; then
  _install_claude
fi

if _contains "${AGENTS}" "codex"; then
  _install_codex
fi

if _contains "${AGENTS}" "opencode"; then
  _install_opencode
fi

_verify

_phase_b_handoff

if [ "${_smoke_failed}" = "1" ]; then
  _fatal 7 "Gateway smoke test failed (non-200 response). Check gateway connectivity and credentials."
fi

exit 0
