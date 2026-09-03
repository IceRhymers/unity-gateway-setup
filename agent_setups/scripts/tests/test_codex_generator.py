"""Unit tests for the Codex CLI config generator.

Builds an in-memory GatewayContext and Namespace. Never reads Terraform outputs
and never touches a real user config. Asserts the STRUCTURE of the emitted
managed_config.toml: the model_providers.databricks block, base_url, wire_api,
and the auth command. Also verifies requirements.toml parses as valid TOML.

Run: python3 -m unittest discover -s agent_setups/scripts/tests
"""

from __future__ import annotations

import argparse
import sys
import tomllib
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from agents.codex import (  # noqa: E402
    CodexGenerator,
    DEFAULT_API_TYPE,
    DEFAULT_GATEWAY_PATH,
    MANAGED_CONFIG_FILENAME,
    REQUIREMENTS_FILENAME,
)
from gateway import Endpoint, GatewayContext, Telemetry  # noqa: E402

HOST = "https://myws.cloud.databricks.com"
PROFILE = "fevm-west"


def _context() -> GatewayContext:
    """A minimal context with one openai endpoint."""
    ep = Endpoint(
        key="openai/gpt",
        schema="openai",
        name="gpt",
        full_name="cat.openai.gpt",
        foundation_model="models/system.ai.gpt-5",
        inference_table=None,
    )
    return GatewayContext(
        host=HOST,
        catalog_name="cat",
        provider_schemas={"openai": "cat.openai"},
        endpoints=[ep],
    )


def _args(**over) -> argparse.Namespace:
    base = dict(
        profile=PROFILE,
        api_type=DEFAULT_API_TYPE,
        skip_api_discovery=True,
        fallback_schema="openai",
        default_model=None,
        reasoning_effort="high",
        provider_name="databricks",
        gateway_path=DEFAULT_GATEWAY_PATH,
        refresh_interval_ms=900000,
        auth_timeout_ms=5000,
        databricks_bin="databricks",
        user_config=False,
        hook_telemetry="off",
        hook_categories="usage,governance,adoption",
        hook_token_ttl_seconds=600,
        hook_script_path=None,
        zerobus_endpoint=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _managed_toml(files: dict[str, str]) -> dict:
    return tomllib.loads(files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"])


class ManagedConfigStructureTest(unittest.TestCase):
    def test_parses_as_toml(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = _managed_toml(files)
        self.assertIsInstance(parsed, dict)

    def test_model_providers_databricks_block_present(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = _managed_toml(files)
        self.assertIn("model_providers", parsed)
        self.assertIn("databricks", parsed["model_providers"])

    def test_base_url_is_host_plus_gateway_route(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = _managed_toml(files)
        self.assertEqual(
            parsed["model_providers"]["databricks"]["base_url"],
            f"{HOST}{DEFAULT_GATEWAY_PATH}",
        )

    def test_wire_api_is_responses(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = _managed_toml(files)
        self.assertEqual(
            parsed["model_providers"]["databricks"]["wire_api"], "responses"
        )

    def test_auth_command_is_bash_with_args(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = _managed_toml(files)
        auth = parsed["model_providers"]["databricks"]["auth"]
        self.assertEqual(auth["command"], "bash")
        self.assertIsInstance(auth["args"], list)
        self.assertGreater(len(auth["args"]), 0)

    def test_top_level_model_and_provider_set(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = _managed_toml(files)
        self.assertIn("model", parsed)
        self.assertIn("model_provider", parsed)
        self.assertEqual(parsed["model_provider"], "databricks")


class RequirementsTomlTest(unittest.TestCase):
    def test_requirements_toml_present_and_parses(self):
        files = CodexGenerator().generate(_context(), _args())
        self.assertIn(f"codex/etc/{REQUIREMENTS_FILENAME}", files)
        # File is all comments when hooks are off; parsed result is an empty dict.
        parsed = tomllib.loads(files[f"codex/etc/{REQUIREMENTS_FILENAME}"])
        self.assertIsInstance(parsed, dict)


class UserConfigModeTest(unittest.TestCase):
    def test_user_config_emits_config_toml(self):
        files = CodexGenerator().generate(_context(), _args(user_config=True))
        self.assertIn("codex/config.toml", files)

    def test_user_config_omits_managed_files(self):
        files = CodexGenerator().generate(_context(), _args(user_config=True))
        self.assertNotIn(f"codex/etc/{MANAGED_CONFIG_FILENAME}", files)
        self.assertNotIn(f"codex/etc/{REQUIREMENTS_FILENAME}", files)

    def test_user_config_toml_parses_and_has_routing(self):
        files = CodexGenerator().generate(_context(), _args(user_config=True))
        parsed = tomllib.loads(files["codex/config.toml"])
        self.assertIn("databricks", parsed.get("model_providers", {}))
        self.assertEqual(
            parsed["model_providers"]["databricks"]["wire_api"], "responses"
        )


class GatewayPathTest(unittest.TestCase):
    def test_custom_gateway_path_reflected_in_base_url(self):
        files = CodexGenerator().generate(
            _context(), _args(gateway_path="/ai-gateway/codex/v1")
        )
        parsed = _managed_toml(files)
        self.assertIn(
            "/ai-gateway/codex/v1",
            parsed["model_providers"]["databricks"]["base_url"],
        )


class NoUnsupportedHardeningKnobsTest(unittest.TestCase):
    """Codex has no config-emittable custom-CA or version-floor knob, so the
    generator must not leak one. NODE_EXTRA_CA_CERTS is Node-only (Codex is Rust),
    and no min-version key is confirmed for requirements.toml (a wrong shape breaks
    config load). Guard against a future accidental fake.
    Also ensures OTEL_* env names do not appear as active keys in the output."""

    def test_no_ca_or_version_keys_in_managed_output(self):
        files = CodexGenerator().generate(_context(), _args())
        blob = "".join(files.values())
        for forbidden in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
                          "minimum_version", "required_version", "requiredMinimumVersion"):
            self.assertNotIn(forbidden, blob, f"{forbidden} unexpectedly emitted")

    def test_no_ca_or_version_keys_in_user_output(self):
        files = CodexGenerator().generate(_context(), _args(user_config=True))
        blob = "".join(files.values())
        for forbidden in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
                          "minimum_version", "required_version", "requiredMinimumVersion"):
            self.assertNotIn(forbidden, blob, f"{forbidden} unexpectedly emitted")

    def test_no_active_otel_env_names_in_managed_output(self):
        """OTEL_* env names must not appear as active (uncommented) TOML keys."""
        files = CodexGenerator().generate(_context(), _args())
        toml_blob = files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"]
        # All OTEL_* references must be commented lines (starting with '#')
        for line in toml_blob.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # OK — in a comment
            for otel_key in ("OTEL_EXPORTER_OTLP", "OTEL_LOGS_EXPORTER",
                              "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER"):
                self.assertNotIn(otel_key, stripped,
                                 f"Active OTEL key {otel_key!r} in uncommented TOML line: {line!r}")


class NoActiveOtelBlockTest(unittest.TestCase):
    """The generated TOML files must not contain an active (parsed) [otel] table.
    The stub ships as COMMENTS only; tomllib must not see an 'otel' key."""

    def test_no_otel_table_in_managed_config(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = tomllib.loads(files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"])
        self.assertNotIn("otel", parsed, "'otel' table found in parsed managed_config.toml")

    def test_no_otel_table_in_requirements(self):
        files = CodexGenerator().generate(_context(), _args())
        parsed = tomllib.loads(files[f"codex/etc/{REQUIREMENTS_FILENAME}"])
        self.assertNotIn("otel", parsed, "'otel' table found in parsed requirements.toml")

    def test_otel_stub_comment_present_in_managed_config(self):
        """The COMMENTED stub must be present so operators can activate it."""
        files = CodexGenerator().generate(_context(), _args())
        toml_blob = files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"]
        self.assertIn("# [otel]", toml_blob)
        self.assertIn("log_user_prompt", toml_blob)


def _context_with_telemetry() -> GatewayContext:
    """A context with OTEL tables deployed (no hook_events needed for OTEL stub tests)."""
    tel = Telemetry(
        schema_full_name="cat.telemetry",
        tables={
            "metrics": "cat.telemetry.otel_metrics",
            "logs": "cat.telemetry.otel_logs",
            "traces": "cat.telemetry.otel_traces",
        },
        secret_full_name="cat.telemetry.otel_sp_creds",
        service_principal_application_id="12345",
    )
    ep = Endpoint(
        key="openai/gpt",
        schema="openai",
        name="gpt",
        full_name="cat.openai.gpt",
        foundation_model="models/system.ai.gpt-5",
        inference_table=None,
    )
    return GatewayContext(
        host=HOST,
        catalog_name="cat",
        provider_schemas={"openai": "cat.openai"},
        endpoints=[ep],
        telemetry=tel,
    )


class OtelPerSignalHeaderTest(unittest.TestCase):
    """Finding 1: the OTEL stub and install_notes must show per-signal
    X-Databricks-UC-Table-Name headers, not just a single Authorization header."""

    def test_per_signal_headers_in_toml_stub(self):
        files = CodexGenerator().generate(_context(), _args())
        toml_blob = files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"]
        # Per-signal headers must appear in the commented stub
        self.assertIn("X-Databricks-UC-Table-Name", toml_blob)
        self.assertIn("OTEL_EXPORTER_OTLP_METRICS_HEADERS", toml_blob)
        self.assertIn("OTEL_EXPORTER_OTLP_LOGS_HEADERS", toml_blob)
        self.assertIn("OTEL_EXPORTER_OTLP_TRACES_HEADERS", toml_blob)

    def test_all_per_signal_lines_in_stub_are_comments(self):
        files = CodexGenerator().generate(_context(), _args())
        toml_blob = files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"]
        for line in toml_blob.splitlines():
            if "X-Databricks-UC-Table-Name" in line:
                stripped = line.strip()
                self.assertTrue(stripped.startswith("#"),
                                f"X-Databricks-UC-Table-Name in uncommented line: {line!r}")

    def test_actual_table_names_in_stub_when_telemetry_present(self):
        # generate() stores _tel_tables; _otel_toml_stub uses them
        gen = CodexGenerator()
        files = gen.generate(_context_with_telemetry(), _args())
        toml_blob = files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"]
        self.assertIn("cat.telemetry.otel_metrics", toml_blob)
        self.assertIn("cat.telemetry.otel_logs", toml_blob)
        self.assertIn("cat.telemetry.otel_traces", toml_blob)

    def test_placeholder_in_stub_when_no_telemetry(self):
        files = CodexGenerator().generate(_context(), _args())
        toml_blob = files[f"codex/etc/{MANAGED_CONFIG_FILENAME}"]
        self.assertIn("<otel-metrics-table>", toml_blob)
        self.assertIn("<otel-logs-table>", toml_blob)
        self.assertIn("<otel-traces-table>", toml_blob)

    def test_per_signal_headers_in_install_notes(self):
        gen = CodexGenerator()
        gen.generate(_context_with_telemetry(), _args())
        notes = gen.install_notes(_args())
        self.assertIn("OTEL_EXPORTER_OTLP_METRICS_HEADERS", notes)
        self.assertIn("OTEL_EXPORTER_OTLP_LOGS_HEADERS", notes)
        self.assertIn("OTEL_EXPORTER_OTLP_TRACES_HEADERS", notes)
        self.assertIn("X-Databricks-UC-Table-Name", notes)
        self.assertIn("cat.telemetry.otel_metrics", notes)


if __name__ == "__main__":
    unittest.main()
