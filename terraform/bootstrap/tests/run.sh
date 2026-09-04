#!/bin/sh
# Offline test runner for the Lakebase state bootstrap.
#
# Style deliberately mirrors agent_setups/deploy/tests/run.sh: an explicit
# hardcoded list rather than a glob, so adding a file to the directory cannot
# silently change what CI runs.
#
# These tests are fully offline: no network, no credentials, no Databricks API
# calls. Each test stubs `databricks` on PATH and fails if it is invoked.
#
# shellcheck shell=sh
set -u

_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
_pass=0
_fail=0
_skipped=''

for _name in \
	test_01_lib_sourcing.sh \
	test_02_arg_parsing.sh \
	test_03_pooler_host_rejection.sh \
	test_04_env_render.sh \
	test_05_idempotency.sh \
	test_06_gitignore.sh \
	test_07_wrapper_fails_closed.sh \
	test_08_wrapper_guards_host.sh \
	test_09_provisioner_env_scrub.sh \
	test_10_conn_str_refusal.sh \
	test_11_env_not_sourced.sh \
	test_12_wrapper_executable.sh \
	test_13_ere_pattern_hygiene.sh; do
	_script="${_dir}/${_name}"
	if [ ! -f "${_script}" ]; then
		printf 'SKIP: %s (not found)\n' "${_name}"
		_skipped="${_skipped} ${_name}"
		continue
	fi
	printf '=== %s ===\n' "${_name}"
	if sh "${_script}"; then
		_pass=$((_pass + 1))
	else
		_fail=$((_fail + 1))
	fi
done

printf '\n--- %d passed, %d failed ---\n' "${_pass}" "${_fail}"
if [ -n "${_skipped}" ]; then
	# Named explicitly: a silently skipped test reports the same green as a
	# passing one, which is how a guard rots without anyone noticing.
	printf 'SKIPPED (absent):%s\n' "${_skipped}"
fi

# A suite that ran nothing must NOT report success. Without this, deleting or
# failing to create the test files would turn `make check` green while asserting
# nothing at all - the same silent-green failure this suite exists to prevent.
if [ "${_pass}" = "0" ] && [ "${_fail}" = "0" ]; then
	printf 'FAIL: no tests ran. Expected test files are missing.\n' >&2
	exit 1
fi

[ "${_fail}" = "0" ]
