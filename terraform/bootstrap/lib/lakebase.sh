#!/bin/sh
# lib/lakebase.sh — pure sourceable helpers for bootstrap-state.sh and with-state.sh.
# NO side effects on source. All functions are prefixed lb_.
#
# Source this file with:
#   . "$(dirname "$0")/lib/lakebase.sh"
# or with a computed path (shellcheck disable=SC1090 at the source site).

# shellcheck disable=SC2034  # LB_LIB_LOADED: used by callers to guard re-sourcing
LB_LIB_LOADED=1

# ---------------------------------------------------------------------------
# lb_require_cmd <cmd>
# Hard-fail if <cmd> is not on PATH.
# ---------------------------------------------------------------------------
lb_require_cmd() {
  _lrc_cmd="$1"
  if ! command -v "${_lrc_cmd}" >/dev/null 2>&1; then
    printf '[lakebase] FATAL: required command not found: %s\n' "${_lrc_cmd}" >&2
    printf '[lakebase] Install %s and re-run.\n' "${_lrc_cmd}" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# lb_pick_host
# Read endpoint JSON on stdin. Print .status.hosts.host. Exit 1 on error.
# Refuses any host matching *pooler*: PgBouncer transaction mode does not
# support advisory locks — state locking would fail SILENTLY.
# ---------------------------------------------------------------------------
lb_pick_host() {
  _host=$(jq -r '.status.hosts.host // empty')
  [ -n "${_host}" ] || {
    printf '[lakebase] FATAL: endpoint JSON has no .status.hosts.host\n' >&2
    return 1
  }
  case "${_host}" in
    *pooler*)
      printf '[lakebase] FATAL: refusing pooled host %s\n' "${_host}" >&2
      printf '[lakebase] PgBouncer runs in transaction mode and does not support advisory locks.\n' >&2
      printf '[lakebase] Terraform state locking would fail SILENTLY. Use status.hosts.host.\n' >&2
      return 1 ;;
  esac
  printf '%s\n' "${_host}"
}

# ---------------------------------------------------------------------------
# lb_assert_direct_host <hostname>
# Same pooler rejection as lb_pick_host, but takes a bare hostname string.
# Used by with-state.sh after it has already resolved PGHOST from .lakebase.env.
# ---------------------------------------------------------------------------
lb_assert_direct_host() {
  _adh_host="$1"
  if [ -z "${_adh_host}" ]; then
    printf '[lakebase] FATAL: lb_assert_direct_host called with empty hostname\n' >&2
    return 1
  fi
  case "${_adh_host}" in
    *pooler*)
      printf '[lakebase] FATAL: refusing pooled host %s\n' "${_adh_host}" >&2
      printf '[lakebase] PgBouncer runs in transaction mode and does not support advisory locks.\n' >&2
      printf '[lakebase] Terraform state locking would fail SILENTLY. Use status.hosts.host.\n' >&2
      return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# lb_parse_env <file>
# Parse KEY=VALUE lines from <file> against a whitelist.
# Exports whitelisted vars into the current shell environment.
# Rejects any key not in the whitelist. Never uses . or source.
# ---------------------------------------------------------------------------
lb_parse_env() {
  _lpe_file="$1"
  if [ ! -f "${_lpe_file}" ]; then
    printf '[lakebase] FATAL: env file not found: %s\n' "${_lpe_file}" >&2
    return 1
  fi

  # Read line by line without sourcing.
  while IFS= read -r _lpe_line || [ -n "${_lpe_line}" ]; do
    # Strip leading whitespace.
    _lpe_line="${_lpe_line#"${_lpe_line%%[! ]*}"}"
    # Skip blank lines and comments.
    case "${_lpe_line}" in
      ''|\#*) continue ;;
    esac
    # Must be KEY=VALUE.
    case "${_lpe_line}" in
      *=*)
        _lpe_key="${_lpe_line%%=*}"
        _lpe_val="${_lpe_line#*=}"
        ;;
      *)
        printf '[lakebase] FATAL: malformed line in %s (no =): %s\n' "${_lpe_file}" "${_lpe_line}" >&2
        return 1
        ;;
    esac
    # Whitelist check.
    case "${_lpe_key}" in
      PGHOST|PGPORT|PGDATABASE|PGSSLMODE| \
      LAKEBASE_PROJECT|LAKEBASE_BRANCH|LAKEBASE_ENDPOINT|LAKEBASE_SCHEMA| \
      DATABRICKS_PROFILE)
        # Strip optional surrounding single or double quotes from value.
        case "${_lpe_val}" in
          \"*\") _lpe_val="${_lpe_val#\"}"; _lpe_val="${_lpe_val%\"}" ;;
          \'*\') _lpe_val="${_lpe_val#\'}"; _lpe_val="${_lpe_val%\'}" ;;
        esac
        export "${_lpe_key}=${_lpe_val}"
        ;;
      *)
        printf '[lakebase] FATAL: key not in whitelist: %s (file: %s)\n' "${_lpe_key}" "${_lpe_file}" >&2
        printf '[lakebase] Allowed keys: PGHOST PGPORT PGDATABASE PGSSLMODE\n' >&2
        printf '[lakebase]   LAKEBASE_PROJECT LAKEBASE_BRANCH LAKEBASE_ENDPOINT\n' >&2
        printf '[lakebase]   LAKEBASE_SCHEMA DATABRICKS_PROFILE\n' >&2
        return 1
        ;;
    esac
  done < "${_lpe_file}"
}

# ---------------------------------------------------------------------------
# lb_render_env
# Emit .lakebase.env content to stdout.
# Caller must set: LB_HOST LB_PORT LB_DATABASE LB_PROJECT LB_BRANCH
#                  LB_ENDPOINT LB_SCHEMA LB_PROFILE LB_GEN_SHA
# No PGUSER line (resolved at run time). No wall-clock timestamp.
# ---------------------------------------------------------------------------
lb_render_env() {
  cat <<EOF
# GENERATED by terraform/bootstrap/bootstrap-state.sh. Do not edit. Do not commit.
# Contains no secret. The credential is minted per invocation.
# generator: ${LB_GEN_SHA}
PGHOST=${LB_HOST}
PGPORT=${LB_PORT}
PGDATABASE=${LB_DATABASE}
# No PGUSER: the wrapper resolves it at run time from \`databricks current-user me\`,
# so this file stays byte-identical across operators (see bootstrap README §3.d).
PGSSLMODE=require
LAKEBASE_PROJECT=${LB_PROJECT}
LAKEBASE_BRANCH=${LB_BRANCH}
LAKEBASE_ENDPOINT=${LB_ENDPOINT}
LAKEBASE_SCHEMA=${LB_SCHEMA}
DATABRICKS_PROFILE=${LB_PROFILE}
EOF
}

# ---------------------------------------------------------------------------
# lb_mint_token <endpoint_path> <profile>
# Call databricks postgres generate-database-credential and print the token.
# -o json is required: default output is text and jq cannot parse it.
# ---------------------------------------------------------------------------
lb_mint_token() {
  _lmt_endpoint="$1"
  _lmt_profile="$2"
  databricks postgres generate-database-credential "${_lmt_endpoint}" \
    --ttl 3600 -o json --profile "${_lmt_profile}" \
    | jq -r '.token'
}
