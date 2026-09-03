#!/usr/bin/env sh
# test_16_smoke_gating.sh — smoke-test gating: offline safety, URL derivation,
# no double-path, exit-7 on non-200, dry-run passthrough.
#
# (1) Default install (no --smoke-test): curl stub never called.
# (2) --smoke-test, no token available: SKIPPED, exit 0, curl not called.
# (3) No-double-path: base URL with /ai-gateway/anthropic path ->
#     smoke URL uses scheme+authority only; no repeated path segment.
# (4) curl stub returns 500 -> exit 7; returns 200 -> exit 0.
# (5) --smoke-test --dry-run -> exit 0, curl not called.
set -eu

# shellcheck disable=SC2164
TESTS_DIR="$(cd "$(dirname -- "$0")" && pwd)"
INSTALL_SH="${TESTS_DIR}/../install.sh"
# shellcheck disable=SC1091
. "${TESTS_DIR}/_fixtures.sh"
T="test_16_smoke_gating"

_work=""
_cleanup() { rm -rf "${_work}" 2>/dev/null || true; }
trap '_cleanup' EXIT INT TERM

_work=$(mktemp -d)

# Prereq stubs shared across sub-cases (overridden per sub-case as needed).
_stub_bin="${_work}/bin"
mkdir -p "${_stub_bin}"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/databricks"
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/jq"
chmod +x "${_stub_bin}/databricks" "${_stub_bin}/jq"
_rpy=$(command -v python3 2>/dev/null) || true
if [ -n "${_rpy}" ]; then
  ln -sf "${_rpy}" "${_stub_bin}/python3"
else
  printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/python3" && chmod +x "${_stub_bin}/python3"
fi

# Sentinel file: the curl stub writes this on every invocation.
_curl_sentinel="${_work}/curl_was_called"

# Default curl stub: records call; exits non-zero so smoke test can detect it.
_write_curl_stub() {
  # $1 = HTTP status code to print (or nothing for default passthrough)
  _wcs_code="${1:-200}"
  printf '#!/bin/sh\nprintf "%s" "%s"\ntouch "%s"\nexit 0\n' \
    "${_wcs_code}" "${_wcs_code}" "${_curl_sentinel}" > "${_stub_bin}/curl"
  chmod +x "${_stub_bin}/curl"
}

# Build a claude-code source bundle with a specific ANTHROPIC_BASE_URL.
# $1 = source dir, $2 = base URL (e.g. https://host/ai-gateway/anthropic)
_mk_claude_bundle_with_url() {
  mkdir -p "$1"
  printf '{ "env": { "ANTHROPIC_BASE_URL": "%s" }, "model": "databricks/x" }\n' "$2" \
    > "$1/managed-settings.json"
}

# ---------------------------------------------------------------------------
# (1) Default install with no --smoke-test: curl stub never invoked
# ---------------------------------------------------------------------------
_staging1="${_work}/staging1"
mkdir -p "${_staging1}"
_cc_src1="${_work}/cc_src1"
mk_claude_bundle "${_cc_src1}" off
_write_curl_stub 200
rm -f "${_curl_sentinel}"

_exit=0
PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src1}" \
  --target-root "${_staging1}" \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(1) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if [ -f "${_curl_sentinel}" ]; then
  printf 'FAIL: %s/(1) — curl was called without --smoke-test\n' "${T}"
  exit 1
fi
printf '  ok (1): default install never calls curl\n'

# ---------------------------------------------------------------------------
# (2) --smoke-test with no token: SKIPPED, exit 0, curl not called
# ---------------------------------------------------------------------------
_staging2="${_work}/staging2"
mkdir -p "${_staging2}"
_cc_src2="${_work}/cc_src2"
_mk_claude_bundle_with_url "${_cc_src2}" "https://test-workspace.example.com/ai-gateway/anthropic"
rm -f "${_curl_sentinel}"

# databricks stub prints nothing (no token field).
printf '#!/bin/sh\nprintf "{}"\n' > "${_stub_bin}/databricks"
chmod +x "${_stub_bin}/databricks"

_exit=0
DATABRICKS_BEARER="" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src2}" \
  --target-root "${_staging2}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(2) — expected exit 0 on no-token skip, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if [ -f "${_curl_sentinel}" ]; then
  printf 'FAIL: %s/(2) — curl was called when token was unavailable\n' "${T}"
  exit 1
fi
if ! grep -q 'no token' "${_work}/err.txt"; then
  printf 'FAIL: %s/(2) — expected "no token" skip message on stderr\n' "${T}"
  printf '  stderr:\n'; cat "${_work}/err.txt"
  exit 1
fi
printf '  ok (2): --smoke-test skips gracefully when no token available\n'

# Restore databricks stub.
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/databricks"
chmod +x "${_stub_bin}/databricks"

# ---------------------------------------------------------------------------
# (3) No-double-path: base URL /ai-gateway/anthropic -> smoke URL uses
#     scheme+authority only; path is /ai-gateway/mlflow/v1/chat/completions
# ---------------------------------------------------------------------------
_staging3="${_work}/staging3"
mkdir -p "${_staging3}"
_cc_src3="${_work}/cc_src3"
_test_host="https://test-workspace.example.com"
_mk_claude_bundle_with_url "${_cc_src3}" "${_test_host}/ai-gateway/anthropic"
rm -f "${_curl_sentinel}"

# curl stub that records its full argv so we can inspect the URL.
_curl_argv_file="${_work}/curl_argv"
cat > "${_stub_bin}/curl" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" >> ARGV_FILE
touch SENTINEL
printf '200'
exit 0
STUB
# shellcheck disable=SC2016
sed -i.bak "s|ARGV_FILE|${_curl_argv_file}|g; s|SENTINEL|${_curl_sentinel}|g" "${_stub_bin}/curl"
rm -f "${_stub_bin}/curl.bak"
chmod +x "${_stub_bin}/curl"

_exit=0
DATABRICKS_BEARER="test-token" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src3}" \
  --target-root "${_staging3}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(3) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

# The URL passed to curl must be scheme+authority + /ai-gateway/mlflow/v1/chat/completions.
_expected_url="${_test_host}/ai-gateway/mlflow/v1/chat/completions"
if ! grep -q "${_expected_url}" "${_curl_argv_file}" 2>/dev/null; then
  printf 'FAIL: %s/(3) — expected URL "%s" in curl argv\n' "${T}" "${_expected_url}"
  printf '  curl argv was:\n'; cat "${_curl_argv_file}" 2>/dev/null || printf '  (empty)\n'
  exit 1
fi
# There must be no double /ai-gateway/mlflow/v1 path segment.
if grep -q 'ai-gateway/mlflow/v1/ai-gateway' "${_curl_argv_file}" 2>/dev/null; then
  printf 'FAIL: %s/(3) — double path segment detected in URL\n' "${T}"
  cat "${_curl_argv_file}"
  exit 1
fi
printf '  ok (3): no-double-path: smoke URL is scheme+authority + /ai-gateway/mlflow/v1/chat/completions\n'

# ---------------------------------------------------------------------------
# (4a) curl returns 500 -> exit 7
# ---------------------------------------------------------------------------
_staging4="${_work}/staging4"
mkdir -p "${_staging4}"
_cc_src4="${_work}/cc_src4"
_mk_claude_bundle_with_url "${_cc_src4}" "https://test-workspace.example.com/ai-gateway/anthropic"
_write_curl_stub 500
rm -f "${_curl_sentinel}"

_exit=0
DATABRICKS_BEARER="test-token" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src4}" \
  --target-root "${_staging4}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "7" ]; then
  printf 'FAIL: %s/(4a) — expected exit 7 on 500 response, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
printf '  ok (4a): non-200 response produces exit 7\n'

# ---------------------------------------------------------------------------
# (4b) curl returns 200 -> exit 0
# ---------------------------------------------------------------------------
_staging4b="${_work}/staging4b"
mkdir -p "${_staging4b}"
_cc_src4b="${_work}/cc_src4b"
_mk_claude_bundle_with_url "${_cc_src4b}" "https://test-workspace.example.com/ai-gateway/anthropic"
_write_curl_stub 200
rm -f "${_curl_sentinel}"

_exit=0
DATABRICKS_BEARER="test-token" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src4b}" \
  --target-root "${_staging4b}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(4b) — expected exit 0 on 200 response, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
printf '  ok (4b): 200 response produces exit 0\n'

# ---------------------------------------------------------------------------
# (5) --smoke-test --dry-run -> exit 0, curl never called
# ---------------------------------------------------------------------------
_staging5="${_work}/staging5"
mkdir -p "${_staging5}"
_cc_src5="${_work}/cc_src5"
_mk_claude_bundle_with_url "${_cc_src5}" "https://test-workspace.example.com/ai-gateway/anthropic"
_write_curl_stub 200
rm -f "${_curl_sentinel}"

_exit=0
DATABRICKS_BEARER="test-token" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src5}" \
  --target-root "${_staging5}" \
  --smoke-test --dry-run \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(5) — expected exit 0 from --dry-run, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if [ -f "${_curl_sentinel}" ]; then
  printf 'FAIL: %s/(5) — curl was called during --dry-run\n' "${T}"
  exit 1
fi
printf '  ok (5): --smoke-test --dry-run exits 0 and never calls curl\n'

printf 'PASS: %s\n' "${T}"
