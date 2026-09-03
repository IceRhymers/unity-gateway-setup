#!/usr/bin/env sh
# test_16_smoke_gating.sh — smoke-test gating: offline safety, URL derivation,
# no double-path, exit-7 on non-200, dry-run passthrough, per-route probe selection.
#
# (1) Default install (no --smoke-test): curl stub never called.
# (2) --smoke-test, no token available: SKIPPED, exit 0, curl not called.
# (3) claude-only bundle -> probe URL ends /ai-gateway/anthropic/v1/messages,
#     anthropic-version header present, Anthropic payload; no double path segment.
# (4) curl stub returns 500 -> exit 7; returns 200 -> exit 0.
# (5) --smoke-test --dry-run -> exit 0, curl not called.
# (6) opencode-only bundle: $schema URL (opencode.ai) first in opencode.json ->
#     smoke uses gateway host, never opencode.ai.
# (7) No derivable model (claude bundle without "model" field, no codex) ->
#     smoke test SKIPS with "no model" message; curl not called.
# (8) codex-only bundle -> probe URL ends /ai-gateway/mlflow/v1/chat/completions,
#     OpenAI-compatible payload, no anthropic-version header.
# (9) opencode-only bundle with "model" field -> model derived from opencode.json
#     (provider prefix stripped), gateway route used (not opencode.ai).
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
# (3) claude-only bundle: ANTHROPIC_BASE_URL -> probe URL is
#     scheme+authority + /ai-gateway/anthropic/v1/messages,
#     anthropic-version header present, Anthropic payload, no double path.
# ---------------------------------------------------------------------------
_staging3="${_work}/staging3"
mkdir -p "${_staging3}"
_cc_src3="${_work}/cc_src3"
_test_host="https://test-workspace.example.com"
_mk_claude_bundle_with_url "${_cc_src3}" "${_test_host}/ai-gateway/anthropic"
rm -f "${_curl_sentinel}"

# curl stub that records its full argv so we can inspect URL, headers, and body.
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

# The URL must be scheme+authority + /ai-gateway/anthropic/v1/messages.
_expected_url3="${_test_host}/ai-gateway/anthropic/v1/messages"
if ! grep -q "${_expected_url3}" "${_curl_argv_file}" 2>/dev/null; then
  printf 'FAIL: %s/(3) — expected URL "%s" in curl argv\n' "${T}" "${_expected_url3}"
  printf '  curl argv was:\n'; cat "${_curl_argv_file}" 2>/dev/null || printf '  (empty)\n'
  exit 1
fi
# anthropic-version header must be present.
if ! grep -q 'anthropic-version: 2023-06-01' "${_curl_argv_file}" 2>/dev/null; then
  printf 'FAIL: %s/(3) — expected "anthropic-version: 2023-06-01" header in curl argv\n' "${T}"
  printf '  curl argv was:\n'; cat "${_curl_argv_file}" 2>/dev/null || printf '  (empty)\n'
  exit 1
fi
# Body must use Anthropic Messages shape (max_tokens before messages).
if ! grep -q '"max_tokens":1' "${_curl_argv_file}" 2>/dev/null; then
  printf 'FAIL: %s/(3) — expected "max_tokens":1 in curl body\n' "${T}"
  cat "${_curl_argv_file}" 2>/dev/null || true
  exit 1
fi
# There must be no double path segment.
if grep -q 'ai-gateway/anthropic/.*ai-gateway' "${_curl_argv_file}" 2>/dev/null; then
  printf 'FAIL: %s/(3) — double path segment detected in URL\n' "${T}"
  cat "${_curl_argv_file}"
  exit 1
fi
printf '  ok (3): claude-only -> probe URL /ai-gateway/anthropic/v1/messages, anthropic-version header, Anthropic payload\n'

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

# ---------------------------------------------------------------------------
# (6) opencode-only: $schema URL (opencode.ai) first in opencode.json ->
#     smoke must use the /ai-gateway/ URL, not opencode.ai
# ---------------------------------------------------------------------------
_staging6="${_work}/staging6"
mkdir -p "${_staging6}"
_oc_src6="${_work}/oc_src6"
mkdir -p "${_oc_src6}"
_test_gw6="https://test-gw.example.com"

# opencode.json with opencode.ai $schema first, then a provider with
# the real gateway baseURL containing /ai-gateway/.
cat > "${_oc_src6}/opencode.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "providers": {
    "databricks-oss": {
      "options": { "baseURL": "${_test_gw6}/ai-gateway/mlflow/v1" }
    }
  }
}
EOF
printf '<?xml version="1.0"?><plist></plist>\n' > "${_oc_src6}/ai.opencode.managed.mobileconfig"
printf '// auth stub\n' > "${_oc_src6}/databricks-auth.ts"

# curl stub that records all argv so we can inspect the URL.
_curl_argv_6="${_work}/curl_argv_6"
cat > "${_stub_bin}/curl" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" >> ARGV_FILE_6
touch SENTINEL_6
printf '200'
exit 0
STUB
# shellcheck disable=SC2016
sed -i.bak "s|ARGV_FILE_6|${_curl_argv_6}|g; s|SENTINEL_6|${_curl_sentinel}|g" "${_stub_bin}/curl"
rm -f "${_stub_bin}/curl.bak"
chmod +x "${_stub_bin}/curl"
rm -f "${_curl_sentinel}" "${_curl_argv_6}"

# Restore databricks stub (ensures token resolution works via DATABRICKS_BEARER).
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/databricks"
chmod +x "${_stub_bin}/databricks"

_exit=0
DATABRICKS_BEARER="test-token" SMOKE_MODEL="test-model" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents opencode \
  --opencode-source "${_oc_src6}" \
  --target-root "${_staging6}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(6) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

# curl must have been called (smoke was not skipped).
if [ ! -f "${_curl_sentinel}" ]; then
  printf 'FAIL: %s/(6) — curl was not called (smoke was unexpectedly skipped)\n' "${T}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi

# The URL passed to curl must use the gateway host, not opencode.ai.
_expected_url6="${_test_gw6}/ai-gateway/mlflow/v1/chat/completions"
if ! grep -q "${_expected_url6}" "${_curl_argv_6}" 2>/dev/null; then
  printf 'FAIL: %s/(6) — expected gateway URL "%s" in curl argv\n' "${T}" "${_expected_url6}"
  printf '  curl argv was:\n'; cat "${_curl_argv_6}" 2>/dev/null || printf '  (empty)\n'
  exit 1
fi
if grep -q 'opencode\.ai' "${_curl_argv_6}" 2>/dev/null; then
  printf 'FAIL: %s/(6) — opencode.ai appeared in curl argv (must be excluded)\n' "${T}"
  cat "${_curl_argv_6}"
  exit 1
fi
# shellcheck disable=SC2016  # $schema is literal text, not a variable
printf '  ok (6): opencode-only with $schema first -> smoke uses gateway host, not opencode.ai\n'

# ---------------------------------------------------------------------------
# (7) No derivable model: claude bundle without "model" field, no codex ->
#     smoke test SKIPS with "no model" message; curl not called
# ---------------------------------------------------------------------------
_staging7="${_work}/staging7"
mkdir -p "${_staging7}"
_cc_src7="${_work}/cc_src7"
mkdir -p "${_cc_src7}"
# managed-settings.json with a gateway URL but no "model" field.
printf '{ "env": { "ANTHROPIC_BASE_URL": "https://test-ws.example.com/ai-gateway/anthropic" } }\n' \
  > "${_cc_src7}/managed-settings.json"

_write_curl_stub 200
rm -f "${_curl_sentinel}"

_exit=0
DATABRICKS_BEARER="test-token" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents claude-code \
  --claude-source "${_cc_src7}" \
  --target-root "${_staging7}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(7) — expected exit 0 on no-model skip, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if [ -f "${_curl_sentinel}" ]; then
  printf 'FAIL: %s/(7) — curl was called when model was not derivable\n' "${T}"
  exit 1
fi
if ! grep -q 'no model' "${_work}/err.txt"; then
  printf 'FAIL: %s/(7) — expected "no model" skip message on stderr\n' "${T}"
  printf '  stderr:\n'; cat "${_work}/err.txt"
  exit 1
fi
printf '  ok (7): no derivable model -> smoke test skips with "no model" message\n'

# ---------------------------------------------------------------------------
# (8) codex-only bundle -> probe URL ends /ai-gateway/mlflow/v1/chat/completions,
#     OpenAI-compatible payload, no anthropic-version header.
# ---------------------------------------------------------------------------
_staging8="${_work}/staging8"
mkdir -p "${_staging8}"
_cx_src8="${_work}/cx_src8"
mkdir -p "${_cx_src8}/etc"
_test_cx_host="https://cx-gw.example.com"
# Minimal managed_config.toml: base_url on the mlflow gateway path + model.
printf 'base_url = "%s/ai-gateway/mlflow/v1"\nmodel = "gpt-4o"\n' \
  "${_test_cx_host}" > "${_cx_src8}/etc/managed_config.toml"
printf 'allow_managed_hooks_only = true\n' > "${_cx_src8}/etc/requirements.toml"

_curl_argv_8="${_work}/curl_argv_8"
cat > "${_stub_bin}/curl" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" >> ARGV_FILE_8
touch SENTINEL_8
printf '200'
exit 0
STUB
# shellcheck disable=SC2016
sed -i.bak "s|ARGV_FILE_8|${_curl_argv_8}|g; s|SENTINEL_8|${_curl_sentinel}|g" "${_stub_bin}/curl"
rm -f "${_stub_bin}/curl.bak"
chmod +x "${_stub_bin}/curl"
rm -f "${_curl_sentinel}" "${_curl_argv_8}"

_exit=0
DATABRICKS_BEARER="test-token" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents codex \
  --codex-source "${_cx_src8}" \
  --target-root "${_staging8}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(8) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if [ ! -f "${_curl_sentinel}" ]; then
  printf 'FAIL: %s/(8) — curl was not called (smoke was unexpectedly skipped)\n' "${T}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
# URL must be the mlflow chat/completions path.
_expected_url8="${_test_cx_host}/ai-gateway/mlflow/v1/chat/completions"
if ! grep -q "${_expected_url8}" "${_curl_argv_8}" 2>/dev/null; then
  printf 'FAIL: %s/(8) — expected URL "%s" in curl argv\n' "${T}" "${_expected_url8}"
  printf '  curl argv was:\n'; cat "${_curl_argv_8}" 2>/dev/null || printf '  (empty)\n'
  exit 1
fi
# No anthropic-version header.
if grep -q 'anthropic-version' "${_curl_argv_8}" 2>/dev/null; then
  printf 'FAIL: %s/(8) — anthropic-version header must not be present for codex/mlflow route\n' "${T}"
  cat "${_curl_argv_8}"
  exit 1
fi
# Body must use OpenAI-compatible shape (messages before max_tokens).
if ! grep -q '"messages"' "${_curl_argv_8}" 2>/dev/null; then
  printf 'FAIL: %s/(8) — expected "messages" key in OpenAI payload\n' "${T}"
  cat "${_curl_argv_8}" 2>/dev/null || true
  exit 1
fi
printf '  ok (8): codex-only -> probe URL /ai-gateway/mlflow/v1/chat/completions, OpenAI payload, no anthropic-version header\n'

# ---------------------------------------------------------------------------
# (9) opencode-only bundle with "model" field -> model derived from opencode.json
#     (provider prefix stripped), mlflow gateway route used (not opencode.ai).
# ---------------------------------------------------------------------------
_staging9="${_work}/staging9"
mkdir -p "${_staging9}"
_oc_src9="${_work}/oc_src9"
mkdir -p "${_oc_src9}"
_test_oc_host="https://oc-gw.example.com"
# opencode.json with a gateway baseURL and a "model" field with a provider prefix.
cat > "${_oc_src9}/opencode.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "databricks/my-gateway-model",
  "providers": {
    "databricks-oss": {
      "options": { "baseURL": "${_test_oc_host}/ai-gateway/mlflow/v1" }
    }
  }
}
EOF
printf '<?xml version="1.0"?><plist></plist>\n' > "${_oc_src9}/ai.opencode.managed.mobileconfig"
printf '// auth stub\n' > "${_oc_src9}/databricks-auth.ts"

_curl_argv_9="${_work}/curl_argv_9"
cat > "${_stub_bin}/curl" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" >> ARGV_FILE_9
touch SENTINEL_9
printf '200'
exit 0
STUB
# shellcheck disable=SC2016
sed -i.bak "s|ARGV_FILE_9|${_curl_argv_9}|g; s|SENTINEL_9|${_curl_sentinel}|g" "${_stub_bin}/curl"
rm -f "${_stub_bin}/curl.bak"
chmod +x "${_stub_bin}/curl"
rm -f "${_curl_sentinel}" "${_curl_argv_9}"

# Restore databricks stub (no SMOKE_MODEL — model must come from opencode.json).
printf '#!/bin/sh\nexit 0\n' > "${_stub_bin}/databricks"
chmod +x "${_stub_bin}/databricks"

_exit=0
DATABRICKS_BEARER="test-token" PATH="${_stub_bin}:${PATH}" sh "${INSTALL_SH}" \
  --os linux --agents opencode \
  --opencode-source "${_oc_src9}" \
  --target-root "${_staging9}" \
  --smoke-test \
  > "${_work}/out.txt" 2> "${_work}/err.txt" || _exit=$?

if [ "${_exit}" != "0" ]; then
  printf 'FAIL: %s/(9) — expected exit 0, got %d\n' "${T}" "${_exit}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
if [ ! -f "${_curl_sentinel}" ]; then
  printf 'FAIL: %s/(9) — curl was not called (smoke was unexpectedly skipped)\n' "${T}"
  cat "${_work}/out.txt" "${_work}/err.txt"
  exit 1
fi
# URL must use the gateway host, not opencode.ai.
_expected_url9="${_test_oc_host}/ai-gateway/mlflow/v1/chat/completions"
if ! grep -q "${_expected_url9}" "${_curl_argv_9}" 2>/dev/null; then
  printf 'FAIL: %s/(9) — expected gateway URL "%s" in curl argv\n' "${T}" "${_expected_url9}"
  printf '  curl argv was:\n'; cat "${_curl_argv_9}" 2>/dev/null || printf '  (empty)\n'
  exit 1
fi
if grep -q 'opencode\.ai' "${_curl_argv_9}" 2>/dev/null; then
  printf 'FAIL: %s/(9) — opencode.ai appeared in curl argv (must be excluded)\n' "${T}"
  cat "${_curl_argv_9}"
  exit 1
fi
# Model in the payload must be the stripped suffix "my-gateway-model", not "databricks/my-gateway-model".
if ! grep -q 'my-gateway-model' "${_curl_argv_9}" 2>/dev/null; then
  printf 'FAIL: %s/(9) — expected stripped model "my-gateway-model" in curl argv\n' "${T}"
  cat "${_curl_argv_9}" 2>/dev/null || true
  exit 1
fi
if grep -q 'databricks/my-gateway-model' "${_curl_argv_9}" 2>/dev/null; then
  printf 'FAIL: %s/(9) — provider prefix "databricks/" must be stripped from model in curl argv\n' "${T}"
  cat "${_curl_argv_9}"
  exit 1
fi
# shellcheck disable=SC2016  # $schema is literal text, not a variable
printf '  ok (9): opencode-only with "model" field -> provider prefix stripped, mlflow gateway route used\n'

printf 'PASS: %s\n' "${T}"
