#!/bin/sh
# test_09_provisioner_env_scrub.sh — every local-exec provisioner under
# terraform/ scrubs PGPASSWORD. This guards against a fifth provisioner
# silently reintroducing the credential leak.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_DIR=$(dirname "${TESTS_DIR}")
REPO_ROOT=$(dirname "$(dirname "${BOOTSTRAP_DIR}")")
TF_DIR="${REPO_ROOT}/terraform"
T="test_09_provisioner_env_scrub"

# Count local-exec provisioners and PGPASSWORD scrubs. grep piped into wc keeps
# the pipeline exit status at wc's 0 even when a count is zero (rule 6).
_lx=$(grep -rF 'provisioner "local-exec"' --include='*.tf' "${TF_DIR}" | wc -l | tr -d ' ')
_scrub=$(grep -rE 'PGPASSWORD[[:space:]]*=[[:space:]]*""' --include='*.tf' "${TF_DIR}" | wc -l | tr -d ' ')

if [ "${_lx}" -eq 0 ]; then
  printf 'FAIL: %s — found no local-exec provisioners; the test would be vacuous\n' "${T}"
  exit 1
fi

if [ "${_lx}" != "${_scrub}" ]; then
  printf 'FAIL: %s — %s local-exec provisioners but %s PGPASSWORD scrubs.\n' "${T}" "${_lx}" "${_scrub}"
  printf '       Every local-exec must set PGPASSWORD = "" (and PGPASSFILE = "").\n'
  grep -rnF 'provisioner "local-exec"' --include='*.tf' "${TF_DIR}"
  exit 1
fi

printf '  ok: all %s local-exec provisioners scrub PGPASSWORD\n' "${_lx}"
printf 'PASS: %s\n' "${T}"
