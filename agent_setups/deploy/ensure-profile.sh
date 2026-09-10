#!/usr/bin/env sh
# ensure-profile.sh — confirm this machine has the AI dev tools Databricks profile.
#
# Every generator and Terraform target in this repo authenticates through a named
# profile in ~/.databrickscfg. This repo standardizes the NAME, not the workspace:
# `ai_dev_tools` on every machine, pointing at whichever workspace that operator
# deploys to. A fixed name gives the generated artifacts one predictable auth path,
# which is what makes a local test comparable to an MDM rollout. It cannot fix the
# host: the workspace URL is baked into every artifact at generation time.
#
# It NEVER reads ~/.databrickscfg. That file holds tokens and client secrets for
# every workspace an operator has. This script asks the Databricks CLI instead:
# `databricks auth profiles -o json` reports name, host, cloud, auth_type, and
# valid, and no secret material. So a failure message can name the profile and its
# host without risking a credential in a terminal, a CI log, or a screen share.
#
# Usage:
#   ensure-profile.sh [OPTIONS]
#
# Options:
#   --profile <name>   Profile to require (default: ai_dev_tools)
#   --host <url>       Workspace URL to name in the setup hint. Optional: without
#                      it the hint carries a <workspace-url> placeholder.
#   --validate         Also confirm the profile authenticates (one network call)
#   --quiet            Print nothing on success
#   -h, --help         Show this message
#
# Exit codes:
#   0  the profile exists (and authenticates, with --validate)
#   1  usage error
#   3  the databricks CLI or jq is missing
#   4  the profile is absent — the message names the command that creates it
#   5  the profile exists but does not authenticate (--validate only)
set -eu

PROFILE_NAME="ai_dev_tools"
HOST=""
VALIDATE=0
QUIET=0

_info()  { [ "${QUIET}" = "1" ] || printf '[ensure-profile] %s\n' "$*"; }
_err()   { printf '[ensure-profile] %s\n' "$*" >&2; }
_fatal() { _c="$1"; shift; printf '[ensure-profile] FATAL: %s\n' "$*" >&2; exit "${_c}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)  shift; PROFILE_NAME="${1:?--profile requires a value}" ;;
    --host)     shift; HOST="${1?--host requires a value}" ;;
    --validate) VALIDATE=1 ;;
    --quiet)    QUIET=1 ;;
    -h|--help)  sed -n '2,32p' "$0"; exit 0 ;;
    *)          _fatal 1 "Unknown option: $1" ;;
  esac
  shift
done

command -v databricks >/dev/null 2>&1 || _fatal 3 "the databricks CLI was not found on PATH."
command -v jq >/dev/null 2>&1 || _fatal 3 "jq was not found on PATH."

# --skip-validate keeps this offline and instant, because a prerequisite runs on
# every make target. It parses the config only. Authentication is checked by
# --validate, which asks about ONE profile instead of every profile the CLI lists.
_profiles_json="$(databricks auth profiles -o json --skip-validate 2>/dev/null || printf '')"
if [ -z "${_profiles_json}" ]; then
  _fatal 4 "the databricks CLI listed no profiles. Run: databricks auth login --host <workspace-url> --profile ${PROFILE_NAME}"
fi

_found="$(printf '%s' "${_profiles_json}" \
  | jq -r --arg p "${PROFILE_NAME}" '.profiles[]? | select(.name == $p) | .host' 2>/dev/null || printf '')"

if [ -z "${_found}" ]; then
  _hint_host="${HOST:-<workspace-url>}"
  _err "The '${PROFILE_NAME}' profile is not configured on this machine."
  _err ""
  _err "This repo targets one profile NAME on every machine, so a local test matches"
  _err "an MDM rollout. Point it at the workspace you deploy to:"
  _err ""
  _err "  make login-profile HOST=${_hint_host}"
  _err ""
  _err "That opens a browser for single sign-on and writes the profile. It stores no"
  _err "personal access token. The equivalent command, to run by hand, is:"
  _err ""
  _err "  databricks auth login --host ${_hint_host} --profile ${PROFILE_NAME}"
  # Name the other profiles, so an operator can see which workspace to reuse. Names
  # and hosts only: this never prints a token, and the CLI never returned one.
  _others="$(printf '%s' "${_profiles_json}" \
    | jq -r '.profiles[]? | "  \(.name)  ->  \(.host // "no host")"' 2>/dev/null || printf '')"
  if [ -n "${_others}" ]; then
    _err "Profiles already on this machine (names and hosts only):"
    printf '%s\n' "${_others}" >&2
  fi
  exit 4
fi

if [ "${VALIDATE}" = "1" ]; then
  # One targeted call. `databricks auth profiles` without --skip-validate would
  # validate every profile, which costs a round trip per workspace.
  if ! databricks current-user me --profile "${PROFILE_NAME}" >/dev/null 2>&1; then
    _err "The '${PROFILE_NAME}' profile exists (host ${_found}) but does not authenticate."
    _err "Its session probably expired. Refresh it:"
    _err ""
    _err "  databricks auth login --host ${_found} --profile ${PROFILE_NAME}"
    exit 5
  fi
  _info "profile '${PROFILE_NAME}' authenticates (host ${_found})."
  exit 0
fi

_info "profile '${PROFILE_NAME}' is configured (host ${_found})."
