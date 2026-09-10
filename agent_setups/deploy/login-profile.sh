#!/usr/bin/env sh
# login-profile.sh — create or refresh this repo's Databricks profile.
#
# The counterpart to ensure-profile.sh. That script only CHECKS, because it runs as a
# make prerequisite and a gate must never open a browser on its own. This script is the
# mutating half, and an operator runs it on purpose:
#
#   make login-profile HOST=https://<workspace>.cloud.databricks.com
#
# It handles both cases. A missing profile gets created. A profile whose session has
# expired gets refreshed, which is the remedy ensure-profile.sh reports as exit 5.
#
# It uses browser single sign-on (OAuth U2M) and stores no personal access token. The
# `databricks` CLI writes the profile; this script validates the input, refuses a
# surprising repoint, and confirms the result.
#
# Usage:
#   login-profile.sh [--host <url>] [OPTIONS]
#
# Options:
#   --host <url>       Workspace URL. Asked for when omitted and the terminal is
#                      interactive. Required when it is not, so a non-interactive
#                      run fails instead of waiting for input that never arrives.
#   --profile <name>   Profile to write (default: ai_dev_tools)
#   --force            Repoint an existing profile at a different host
#   --dry-run          Resolve and check everything, run no login (exit 0)
#   -h, --help         Show this message
#
# Exit codes:
#   0  the profile authenticates (or --dry-run)
#   1  usage error, or a malformed host
#   3  the databricks CLI or jq is missing
#   6  the profile already points at a different workspace (pass --force to repoint)
#   7  the login ran but the profile still does not authenticate
set -eu

PROFILE_NAME="ai_dev_tools"
HOST=""
FORCE=0
DRY_RUN=0
_DIR="$(dirname "$0")"

_info()  { printf '[login-profile] %s\n' "$*"; }
_err()   { printf '[login-profile] %s\n' "$*" >&2; }
_fatal() { _c="$1"; shift; printf '[login-profile] FATAL: %s\n' "$*" >&2; exit "${_c}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host)     shift; HOST="${1:?--host requires a value}" ;;
    --profile)  shift; PROFILE_NAME="${1:?--profile requires a value}" ;;
    --force)    FORCE=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  sed -n '2,33p' "$0"; exit 0 ;;
    *)          _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

command -v databricks >/dev/null 2>&1 || _fatal 3 "the databricks CLI was not found on PATH."
command -v jq >/dev/null 2>&1 || _fatal 3 "jq was not found on PATH."

# Look the profile up before resolving the host, so an interactive prompt can offer
# the host it already uses. This is also what the repoint guard below compares against.
_existing="$(databricks auth profiles -o json --skip-validate 2>/dev/null \
  | jq -r --arg p "${PROFILE_NAME}" '.profiles[]? | select(.name == $p) | .host // empty' 2>/dev/null || printf '')"

# Ask for the host when it was not passed. Only when stdin is a terminal: a piped or
# scheduled run must fail with the usage error rather than block on a read that no one
# will answer.
if [ -z "${HOST}" ] && [ -t 0 ]; then
  printf '[login-profile] Which workspace should the %s profile authenticate to?\n' "${PROFILE_NAME}"
  if [ -n "${_existing}" ]; then
    printf '[login-profile] Press return to keep the workspace it uses now.\n'
    printf '[login-profile] Workspace URL [%s]: ' "${_existing}"
  else
    printf '[login-profile] Workspace URL (https://<workspace>.cloud.databricks.com): '
  fi
  read -r HOST || HOST=""
  # An empty answer keeps the current host, which makes a bare run the refresh path.
  if [ -z "${HOST}" ] && [ -n "${_existing}" ]; then
    HOST="${_existing}"
    _info "keeping ${HOST}."
  fi
fi

if [ -z "${HOST}" ]; then
  _err "No workspace URL was given. Name the workspace this profile authenticates to:"
  _err ""
  _err "  make login-profile HOST=https://<workspace>.cloud.databricks.com"
  _err ""
  _err "The repo fixes the profile NAME, not the workspace. Point it at the workspace"
  _err "you deploy to."
  exit 1
fi

# Reject a host that is not a URL, and one carrying shell syntax. The value reaches a
# command line, and a clear refusal beats a confusing CLI error.
case "${HOST}" in
  https://*) ;;
  *) _fatal 1 "the workspace URL must start with https:// (got '${HOST}')." ;;
esac
case "${HOST}" in
  *[\'\"\`\$\;\ ]*|*'&'*|*'|'*|*'<'*|*'>'*)
    _fatal 1 "the workspace URL contains characters that are not valid in a URL." ;;
esac

# A repoint is the dangerous case, so it is refused by default. The host is baked into
# every artifact at generation time, so changing it silently changes which workspace
# every later build targets, and the Lakebase state project may not exist there.
if [ -n "${_existing}" ] && [ "${_existing}" != "${HOST}" ] && [ "${FORCE}" = "0" ]; then
  _err "The '${PROFILE_NAME}' profile already points at a different workspace."
  _err ""
  _err "  now:       ${_existing}"
  _err "  requested: ${HOST}"
  _err ""
  _err "Repointing changes the workspace EVERY later build targets, because the host is"
  _err "baked into each artifact when it is generated. The Lakebase state project may"
  _err "not exist in the new workspace either, which stops the tf-* targets."
  _err ""
  _err "To repoint on purpose, re-run with --force:"
  _err "  make login-profile HOST=${HOST} ARGS=--force"
  exit 6
fi

if [ -n "${_existing}" ]; then
  _info "refreshing '${PROFILE_NAME}' (host ${_existing})."
else
  _info "creating '${PROFILE_NAME}' for ${HOST}."
fi
_info "A browser opens for single sign-on. No personal access token is stored."

if [ "${DRY_RUN}" = "1" ]; then
  _info "[dry run] would run: databricks auth login --host ${HOST} --profile ${PROFILE_NAME}"
  exit 0
fi

databricks auth login --host "${HOST}" --profile "${PROFILE_NAME}"

# Confirm, rather than trusting the exit code. Delegate to the checker so the check
# is defined in exactly one place.
sh "${_DIR}/ensure-profile.sh" --profile "${PROFILE_NAME}" --validate \
  || _fatal 7 "the login finished but '${PROFILE_NAME}' still does not authenticate."
