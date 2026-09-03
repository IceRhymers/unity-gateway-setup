"""Unit tests for the Claude Code managed-settings generator.

Builds an in-memory GatewayContext and Namespace. Never reads Terraform outputs
and never touches a real user config. Asserts the STRUCTURE of the emitted
managed-settings.json: required env vars, model governance, WebSearch deny,
modelPicker, requiredMinimumVersion, and the OTEL env block.

Run: python3 -m unittest discover -s agent_setups/scripts/tests
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from agents.claude_code import ClaudeCodeGenerator, OTEL_HELPER_FILENAME  # noqa: E402
from gateway import Endpoint, GatewayContext, Telemetry  # noqa: E402

HOST = "https://myws.cloud.databricks.com"
PROFILE = "fevm-west"


def _context() -> GatewayContext:
    """A minimal context with one Anthropic endpoint and no telemetry."""
    ep = Endpoint(
        key="anthropic/claude-sonnet",
        schema="anthropic",
        name="claude-sonnet",
        full_name="cat.anthropic.claude-sonnet",
        foundation_model="models/system.ai.databricks-claude-sonnet",
        inference_table=None,
    )
    return GatewayContext(
        host=HOST,
        catalog_name="cat",
        provider_schemas={"anthropic": "cat.anthropic"},
        endpoints=[ep],
    )


def _context_with_telemetry() -> GatewayContext:
    """A context that carries OTEL telemetry (metrics + traces)."""
    tel = Telemetry(
        schema_full_name="cat.telemetry",
        tables={
            "metrics": "cat.telemetry.otel_metrics",
            "traces": "cat.telemetry.otel_traces",
        },
        secret_full_name="cat.telemetry.otel_sp_creds",
        service_principal_application_id="12345",
    )
    ep = Endpoint(
        key="anthropic/claude-sonnet",
        schema="anthropic",
        name="claude-sonnet",
        full_name="cat.anthropic.claude-sonnet",
        foundation_model="models/system.ai.databricks-claude-sonnet",
        inference_table=None,
    )
    return GatewayContext(
        host=HOST,
        catalog_name="cat",
        provider_schemas={"anthropic": "cat.anthropic"},
        endpoints=[ep],
        telemetry=tel,
    )


def _args(**over) -> argparse.Namespace:
    base = dict(
        profile=PROFILE,
        skip_api_discovery=True,
        fallback_schema="anthropic",
        default_tier="sonnet",
        lock_models="catalog",
        small_context=False,
        allow_websearch=False,
        declare_capabilities=False,
        api_key_ttl_ms=900000,
        databricks_bin="databricks",
        required_min_version=None,
        ssl_cert_file=None,
        model_picker=False,
        model_picker_append=False,
        telemetry="off",
        otel_log_content=False,
        otel_metric_interval_ms=60000,
        otel_logs_interval_ms=5000,
        otel_headers_helper_debounce_ms=900000,
        user_config=False,
        platforms="macos",
        hook_telemetry="off",
        hook_categories="usage,reliability,governance,adoption",
        hook_doc_patterns=r"TESTING\.md",
        hook_log_paths=False,
        hook_token_ttl_seconds=600,
        zerobus_endpoint=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _settings(files: dict[str, str], platform: str = "macos") -> dict:
    return json.loads(files[f"claude-code/{platform}/managed-settings.json"])


class BaseStructureTest(unittest.TestCase):
    def test_parses_as_json_and_contains_anthropic_base_url(self):
        files = ClaudeCodeGenerator().generate(_context(), _args())
        s = _settings(files)
        self.assertIn("ANTHROPIC_BASE_URL", s["env"])
        self.assertEqual(
            s["env"]["ANTHROPIC_BASE_URL"],
            f"{HOST}/ai-gateway/anthropic",
        )

    def test_apikey_helper_is_nonempty_string(self):
        files = ClaudeCodeGenerator().generate(_context(), _args())
        s = _settings(files)
        self.assertIn("apiKeyHelper", s)
        self.assertIsInstance(s["apiKeyHelper"], str)
        self.assertGreater(len(s["apiKeyHelper"]), 0)


class ModelGovernanceTest(unittest.TestCase):
    def test_catalog_mode_sets_enforce_and_nonempty_available(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(lock_models="catalog"))
        s = _settings(files)
        self.assertTrue(s.get("enforceAvailableModels"))
        self.assertIsInstance(s.get("availableModels"), list)
        self.assertGreater(len(s["availableModels"]), 0)

    def test_none_mode_omits_enforcement(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(lock_models="none"))
        s = _settings(files)
        self.assertNotIn("enforceAvailableModels", s)
        self.assertNotIn("availableModels", s)

    def test_aliases_mode_includes_alias_endpoints(self):
        # claude-sonnet has no version-digit suffix, so it is an alias and appears.
        files = ClaudeCodeGenerator().generate(_context(), _args(lock_models="aliases"))
        s = _settings(files)
        self.assertTrue(s.get("enforceAvailableModels"))
        self.assertGreater(len(s["availableModels"]), 0)


class WebSearchDenyTest(unittest.TestCase):
    def test_websearch_denied_by_default(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(allow_websearch=False))
        s = _settings(files)
        self.assertIn("WebSearch", s["permissions"]["deny"])

    def test_no_deny_when_allow_websearch(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(allow_websearch=True))
        s = _settings(files)
        self.assertNotIn("permissions", s)


class ModelPickerTest(unittest.TestCase):
    def test_no_picker_by_default(self):
        files = ClaudeCodeGenerator().generate(_context(), _args())
        s = _settings(files)
        self.assertNotIn("modelPicker", s)

    def test_picker_block_present_when_flag_set(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(model_picker=True))
        s = _settings(files)
        self.assertIn("modelPicker", s)
        picker = s["modelPicker"]
        self.assertIn("options", picker)
        self.assertIsInstance(picker["options"], list)
        self.assertGreater(len(picker["options"]), 0)
        self.assertIn("replaceBuiltInOptions", picker)

    def test_picker_replaces_builtin_by_default(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(model_picker=True))
        s = _settings(files)
        self.assertTrue(s["modelPicker"]["replaceBuiltInOptions"])

    def test_picker_appends_when_flag_set(self):
        files = ClaudeCodeGenerator().generate(
            _context(), _args(model_picker=True, model_picker_append=True)
        )
        s = _settings(files)
        self.assertFalse(s["modelPicker"]["replaceBuiltInOptions"])


class RequiredMinVersionTest(unittest.TestCase):
    def test_absent_by_default(self):
        files = ClaudeCodeGenerator().generate(_context(), _args())
        s = _settings(files)
        self.assertNotIn("requiredMinimumVersion", s)

    def test_present_when_set(self):
        files = ClaudeCodeGenerator().generate(
            _context(), _args(required_min_version="1.2.3")
        )
        s = _settings(files)
        self.assertEqual(s["requiredMinimumVersion"], "1.2.3")

    def test_dropped_in_user_config_mode(self):
        files = ClaudeCodeGenerator().generate(
            _context(), _args(required_min_version="1.2.3", user_config=True)
        )
        self.assertIn("claude-code/user/settings.json", files)
        s = json.loads(files["claude-code/user/settings.json"])
        self.assertNotIn("requiredMinimumVersion", s)


class OtelEnvTest(unittest.TestCase):
    def test_otel_vars_present_with_telemetry(self):
        files = ClaudeCodeGenerator().generate(
            _context_with_telemetry(), _args(telemetry="auto")
        )
        s = _settings(files)
        env = s["env"]
        self.assertIn("CLAUDE_CODE_ENABLE_TELEMETRY", env)
        self.assertIn("OTEL_EXPORTER_OTLP_PROTOCOL", env)
        # At least one per-signal endpoint var must be present.
        signal_endpoint_vars = [
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        ]
        self.assertTrue(
            any(k in env for k in signal_endpoint_vars),
            f"expected at least one OTLP endpoint var; env keys: {sorted(env)}",
        )

    def test_otel_helper_path_set_with_telemetry(self):
        files = ClaudeCodeGenerator().generate(
            _context_with_telemetry(), _args(telemetry="auto")
        )
        s = _settings(files)
        self.assertIn("otelHeadersHelper", s)

    def test_otel_absent_when_off(self):
        files = ClaudeCodeGenerator().generate(
            _context_with_telemetry(), _args(telemetry="off")
        )
        s = _settings(files)
        self.assertNotIn("CLAUDE_CODE_ENABLE_TELEMETRY", s["env"])
        self.assertNotIn("otelHeadersHelper", s)

    def test_otel_absent_when_no_telemetry_in_context(self):
        # auto + no telemetry deployed -> no OTEL vars.
        files = ClaudeCodeGenerator().generate(_context(), _args(telemetry="auto"))
        s = _settings(files)
        self.assertNotIn("CLAUDE_CODE_ENABLE_TELEMETRY", s["env"])
        self.assertNotIn("otelHeadersHelper", s)


class OtelTokenScopeTest(unittest.TestCase):
    """The otelHeadersHelper must mint a DOWN-SCOPED token (authorization_details
    to the OTEL UC tables), not a bare all-apis token."""

    def _helper(self, platform: str = "macos") -> str:
        files = ClaudeCodeGenerator().generate(
            _context_with_telemetry(), _args(telemetry="auto")
        )
        return files[f"claude-code/{platform}/{OTEL_HELPER_FILENAME}"]

    def test_helper_uses_authorization_details_downscoping(self):
        helper = self._helper()
        self.assertIn("authorization_details", helper)
        # The UC privilege grants that scope the token to the OTEL objects.
        self.assertIn("USE CATALOG", helper)
        self.assertIn("USE SCHEMA", helper)
        self.assertIn("MODIFY", helper)

    def test_helper_scopes_to_the_otel_tables(self):
        helper = self._helper()
        # OTEL_UC_TABLES carries exactly the tables the export writes to.
        self.assertIn("OTEL_UC_TABLES", helper)
        self.assertIn("cat.telemetry.otel_metrics", helper)
        self.assertIn("cat.telemetry.otel_traces", helper)

    def test_helper_not_a_bare_all_apis_token(self):
        # Regression guard: an all-apis scope WITHOUT authorization_details is the
        # old over-broad mint. authorization_details must be present alongside it.
        helper = self._helper()
        self.assertIn("all-apis", helper)  # base scope grammar is still all-apis
        self.assertIn("authorization_details", helper)


class UserConfigModeTest(unittest.TestCase):
    def test_user_config_emits_user_settings_json(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(user_config=True))
        self.assertIn("claude-code/user/settings.json", files)

    def test_user_settings_contains_required_keys(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(user_config=True))
        s = json.loads(files["claude-code/user/settings.json"])
        self.assertIn("ANTHROPIC_BASE_URL", s["env"])
        self.assertIn("apiKeyHelper", s)

    def test_user_config_emits_no_platform_bundles(self):
        files = ClaudeCodeGenerator().generate(_context(), _args(user_config=True))
        for key in files:
            self.assertFalse(
                any(f"/{p}/" in key for p in ("macos", "linux", "windows")),
                f"unexpected platform path in user-config output: {key}",
            )


if __name__ == "__main__":
    unittest.main()
