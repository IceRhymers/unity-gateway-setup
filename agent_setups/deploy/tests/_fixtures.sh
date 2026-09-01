#!/usr/bin/env sh
# _fixtures.sh — shared, self-contained fixtures for the install.sh test suite.
#
# Source this AFTER defining TESTS_DIR. It is never executed directly and is not
# in run.sh's test list (the leading underscore + no test_ prefix keep it out).
# These builders let every test run with NO pre-generated bundles, NO network,
# and NO Terraform. `make test` is zero-setup.
#
# Two kinds of fixture:
#   - mk_claude_bundle: a hand-crafted Claude Code bundle for install.sh PLACEMENT
#     tests. install.sh only copies files, so file PRESENCE (not content) drives
#     its behavior. This keeps placement/prereq tests independent of the generator.
#   - mk_tf_fixture: a telemetry-ON `terraform output -json` fixture for tests that
#     must run the REAL generator offline (e.g. generator<->installer path
#     agreement), where hand-crafted content would be a vacuous pass.

# mk_claude_bundle <dir> [on|off]
#   Writes a minimal Claude Code bundle into <dir>:
#     managed-settings.json                        (always)
#     otel-headers-helper.sh + emit_hook_events.sh (only when the 2nd arg is "on")
mk_claude_bundle() {
  _mcb_dir="$1"; _mcb_tel="${2:-off}"
  mkdir -p "${_mcb_dir}"
  printf '{ "env": {}, "model": "databricks/x" }\n' > "${_mcb_dir}/managed-settings.json"
  if [ "${_mcb_tel}" = "on" ]; then
    printf '#!/bin/sh\nexit 0\n' > "${_mcb_dir}/otel-headers-helper.sh"
    printf '#!/bin/sh\nexit 0\n' > "${_mcb_dir}/emit_hook_events.sh"
    chmod +x "${_mcb_dir}/otel-headers-helper.sh" "${_mcb_dir}/emit_hook_events.sh"
  fi
}

# mk_tf_fixture <path>
#   Writes a telemetry-ON `terraform output -json` fixture. Combined with
#   --skip-api-discovery, the generator emits real telemetry + hook configs with
#   no network and no Terraform: managed-settings.json with an otelHeadersHelper
#   path and emit_hook_events hook commands, plus the helper scripts. The
#   endpoint is supplied here so the generator does not derive it (which would
#   need workspace metadata).
mk_tf_fixture() {
  cat > "$1" <<'JSON'
{
  "endpoints": {"value": {
    "openai/gpt": {"full_name": "cat.openai.gpt", "foundation_model": "", "inference_table": null},
    "anthropic/claude-sonnet": {"full_name": "cat.anthropic.claude-sonnet", "foundation_model": "", "inference_table": null}
  }},
  "catalog_name": {"value": "cat"},
  "provider_schemas": {"value": {"openai": "cat.openai", "anthropic": "cat.anthropic"}},
  "telemetry": {"value": {
    "schema_full_name": "cat.telemetry",
    "tables": {"metrics": "cat.telemetry.m", "logs": "cat.telemetry.l", "traces": "cat.telemetry.t"},
    "secret_full_name": "cat.telemetry.otel_oauth",
    "service_principal_application_id": "00000000-0000-0000-0000-000000000000",
    "hook_events": {"table": "cat.telemetry.hook_events", "endpoint": "https://ex.zerobus.ex.cloud.databricks.com"}
  }}
}
JSON
}
