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
from gateway import Endpoint, GatewayContext  # noqa: E402

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
    config load). Guard against a future accidental fake."""

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


if __name__ == "__main__":
    unittest.main()
