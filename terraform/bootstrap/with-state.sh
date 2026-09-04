#!/bin/sh
# with-state.sh — per-invocation Lakebase credential wrapper.
#
# Every state-touching Makefile target routes through this script.
# It sanitizes inherited PG* variables, parses .lakebase.env (never
# sources it), resolves PGUSER at run time, mints a per-invocation
# OAuth token into PGPASSWORD, re-runs the pooler guard, then execs
# the wrapped command.
#
# Four-quadrant behaviour (plan §3.b):
#
#   backend.tf present  + .lakebase.env present  → mint + exec
#   backend.tf present  + .lakebase.env missing  → HARD FAIL
#   backend.tf absent   + no pg record in .terraform/ → exec passthrough
#   backend.tf absent   + pg record present       → HARD FAIL
#
# PG_CONN_STR set → HARD FAIL unconditionally (checked before all else).
#
# lib/lakebase.sh is created by the bootstrap Track A scripts.
# This file must be present before any wrapped target can run.

set -eu

# ---- 0. load shared library ----
# SC1090/SC1091: sourcing by computed path; path is repo-internal, not user input.
# shellcheck disable=SC1090,SC1091
. "$(dirname "$0")/lib/lakebase.sh"

# ---- 1. refuse PG_CONN_STR before any unset ----
# The Terraform pg backend's DefaultFunc reads PG_CONN_STR and writes it into
# .terraform/ and plan files, which violates the credential-isolation guarantee
# (plan principle 2). Check it first, before the unset loop, so the two rules
# do not name the same variable with opposite intents.
if [ -n "${PG_CONN_STR:-}" ]; then
    printf 'FATAL: PG_CONN_STR is set.\n' >&2
    printf 'The Terraform pg backend writes this value into .terraform/ and plan files.\n' >&2
    printf 'Unset it before running any Terraform target:\n' >&2
    printf '  unset PG_CONN_STR\n' >&2
    exit 1
fi

# ---- 2. sanitize inherited PG* variables ----
# PGHOSTADDR overrides PGHOST as the TCP target.
# PGSERVICE can redirect host/port/user wholesale.
# The TLS/connection vars can silently alter the semantics the design depends on.
# None of these are leaks in themselves, but each can break the safety properties.
unset PGHOSTADDR PGSERVICE PGSERVICEFILE PGPASSFILE PGOPTIONS \
      PGSSLROOTCERT PGSSLCERT PGSSLKEY PGGSSENCMODE \
      PGTARGETSESSIONATTRS PGCHANNELBINDING

# ---- 3. resolve the Terraform working directory ----
# Precedence:
#   1. TF_STATE_DIR, when the caller sets it. This is REQUIRED whenever the
#      wrapped command is not terraform itself. The config generator, for
#      example, runs `terraform -chdir=<dir> output -json` from inside Python,
#      so no -chdir= appears in this script's argv and the scan below cannot
#      see it. Without this override the generator fell through to the clean-
#      passthrough quadrant, execed with no PG* set, and the pg backend then
#      used libpq's default target (127.0.0.1:5432) instead of Lakebase.
#   2. a -chdir= argument in the wrapped command (the plain terraform targets).
#   3. "." — the repo root.
_tf_dir="${TF_STATE_DIR:-}"
if [ -z "${_tf_dir}" ]; then
    _tf_dir="."
    for _arg in "$@"; do
        case "${_arg}" in
            -chdir=*) _tf_dir="${_arg#-chdir=}"; break ;;
        esac
    done
fi

# ---- 4. four-quadrant dispatch ----
_backend="${_tf_dir}/backend.tf"
_env_file="${_tf_dir}/.lakebase.env"
_tf_state="${_tf_dir}/.terraform/terraform.tfstate"

_has_pg_record=0
if [ -f "${_tf_state}" ] && grep -q '"type"[[:space:]]*:[[:space:]]*"pg"' "${_tf_state}" 2>/dev/null; then
    _has_pg_record=1
fi

if [ ! -f "${_backend}" ]; then
    # backend.tf absent.
    if [ "${_has_pg_record}" -eq 1 ]; then
        printf 'FATAL: backend.tf is absent but .terraform/ still records a pg backend.\n' >&2
        printf 'This is mid-rollback state. Complete tf-state-unwire step 2:\n' >&2
        printf '  rm -rf %s/.terraform\n' "${_tf_dir}" >&2
        printf 'See terraform/bootstrap/RUNBOOK.md for the full rollback procedure.\n' >&2
        exit 1
    fi
    # Clean passthrough: no backend, no pg record.
    exec "$@"
fi

# backend.tf is present from here on.

if [ ! -f "${_env_file}" ]; then
    printf 'FATAL: backend.tf is present but %s is missing.\n' "${_env_file}" >&2
    printf '\n' >&2
    printf 'Run the bootstrap to create the Lakebase project and your .lakebase.env:\n' >&2
    printf '  make tf-bootstrap-state\n' >&2
    printf '\n' >&2
    printf 'If you do not have Lakebase access, supply Terraform outputs directly\n' >&2
    printf 'using the --tf-output-json escape hatch:\n' >&2
    printf '  python3 agent_setups/scripts/generate.py <agent> --tf-output-json <file>\n' >&2
    exit 1
fi

# Parse .lakebase.env against the key whitelist. Never source it.
# lb_parse_env exports: PGHOST PGPORT PGDATABASE PGSSLMODE
#   LAKEBASE_PROJECT LAKEBASE_BRANCH LAKEBASE_ENDPOINT LAKEBASE_SCHEMA
#   DATABRICKS_PROFILE
lb_parse_env "${_env_file}"

# Re-run the bare-hostname pooler guard on the final PGHOST.
# lb_assert_direct_host hard-fails if the host contains "pooler".
lb_assert_direct_host "${PGHOST:-}"

# Profile mismatch check: only when PROFILE was set explicitly.
# PROFILE_EXPLICIT=1 is set by the Makefile using $(origin PROFILE) so the
# Makefile default (fevm-west) does not trip the check for other operators.
if [ "${PROFILE_EXPLICIT:-}" = "1" ] && \
   [ -n "${DATABRICKS_PROFILE:-}" ] && \
   [ -n "${PROFILE:-}" ] && \
   [ "${PROFILE}" != "${DATABRICKS_PROFILE}" ]; then
    printf 'FATAL: PROFILE=%s but .lakebase.env records DATABRICKS_PROFILE=%s.\n' \
        "${PROFILE}" "${DATABRICKS_PROFILE}" >&2
    printf 'Pass the recorded profile explicitly: make <target> PROFILE=%s\n' \
        "${DATABRICKS_PROFILE}" >&2
    exit 1
fi

_profile="${PROFILE:-${DATABRICKS_PROFILE:-fevm-west}}"

# Connect AS the group role. In Lakebase you do not inherit a group role; a
# member of the backing workspace group authenticates as it. Every operator uses
# the same login role, so the state objects the role owns are shared. The token
# below authorizes the human; PGUSER selects the role. .lakebase.env stays
# byte-identical across operators because LAKEBASE_ROLE is the same for all.
PGUSER="${LAKEBASE_ROLE:-}"
if [ -z "${PGUSER}" ]; then
    printf 'FATAL: LAKEBASE_ROLE is missing from %s.\n' "${_env_file}" >&2
    printf 'Re-run the bootstrap to regenerate it: make tf-bootstrap-state\n' >&2
    exit 1
fi
export PGUSER

# Mint a per-invocation OAuth token into PGPASSWORD.
#
# One CLI call, not two: minting twice would issue two different tokens and
# waste a round trip, so the token and its expiry come from the same response.
# -o json is required - the CLI defaults to text output, and piping text into
# jq yields an empty result plus a misleading error further downstream.
_cred=$(databricks postgres generate-database-credential "${LAKEBASE_ENDPOINT:-}" \
    --ttl 3600s -o json --profile "${_profile}")
PGPASSWORD=$(printf '%s' "${_cred}" | jq -r '.token // empty')
_expires=$(printf '%s' "${_cred}" | jq -r '.expire_time // empty')
unset _cred
if [ -z "${PGPASSWORD}" ]; then
    printf 'FATAL: failed to mint a Lakebase credential for %s\n' "${LAKEBASE_ENDPOINT:-}" >&2
    exit 1
fi
export PGPASSWORD

# Print one observability line per invocation. Never print the token itself.
printf 'lakebase: %s db=%s schema=%s user=%s token_expires=%s\n' \
    "${PGHOST:-}" "${PGDATABASE:-}" "${LAKEBASE_SCHEMA:-}" \
    "${PGUSER:-}" "${_expires:-}" >&2

exec "$@"
