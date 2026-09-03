"""Unit tests for the DeepSeek Harness (dsh) cordis.patch.yml generator.

Builds an in-memory GatewayContext and Namespace. Never reads Terraform outputs
and never touches a real user config. Asserts the STRUCTURE of the emitted
cordis.patch.yml: gateway baseURL, credential reference, token-refresh plugin
insert, gateway-safety plugin disables, and the thinking flag's effect.

Run: python3 -m unittest discover -s agent_setups/scripts/tests
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from agents.dsh import (  # noqa: E402
    CREDENTIAL_REF,
    DEEPSEEK_PROVIDER,
    GATEWAY_OSS_ROUTE,
    PATCH_FILENAME,
    PLUGIN_FILENAME,
    PLUGIN_REF_RELATIVE,
    DshGenerator,
)
from gateway import Endpoint, GatewayContext  # noqa: E402

HOST = "https://myws.cloud.databricks.com"
PROFILE = "fevm-west"


def _context() -> GatewayContext:
    """A minimal context with one DeepSeek endpoint."""
    ep = Endpoint(
        key="deepseek/deepseek-v4-flash",
        schema="deepseek",
        name="deepseek-v4-flash",
        full_name="cat.deepseek.deepseek-v4-flash",
        foundation_model="models/system.ai.deepseek-v4-flash",
        inference_table=None,
    )
    return GatewayContext(
        host=HOST,
        catalog_name="cat",
        provider_schemas={"deepseek": "cat.deepseek"},
        endpoints=[ep],
    )


def _args(**over) -> argparse.Namespace:
    base = dict(
        profile=PROFILE,
        auth_profile=None,
        default_model=None,
        thinking=False,
        refresh_skew_ms=5 * 60 * 1000,
        fallback_ttl_ms=50 * 60 * 1000,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _patch(files: dict[str, str]) -> list:
    return yaml.safe_load(files[f"dsh/{PATCH_FILENAME}"])


def _entry_by_id(patch: list, entry_id: str) -> dict:
    """Return the first patch entry with the given id, or raise AssertionError."""
    for item in patch:
        if isinstance(item, dict) and item.get("id") == entry_id:
            return item
    raise AssertionError(f"No entry with id={entry_id!r} in patch")


class PatchStructureTest(unittest.TestCase):
    def test_parses_as_yaml_list(self):
        files = DshGenerator().generate(_context(), _args())
        patch = _patch(files)
        self.assertIsInstance(patch, list)
        self.assertGreater(len(patch), 0)

    def test_gateway_base_url_in_llm_entry(self):
        files = DshGenerator().generate(_context(), _args())
        entry = _entry_by_id(_patch(files), "llm-deepseek")
        self.assertEqual(entry["config"]["baseURL"], f"{HOST}{GATEWAY_OSS_ROUTE}")

    def test_credential_ref_in_llm_entry(self):
        files = DshGenerator().generate(_context(), _args())
        entry = _entry_by_id(_patch(files), "llm-deepseek")
        self.assertEqual(entry["config"]["apiKeyEnv"], CREDENTIAL_REF)

    def test_default_model_set_in_agent_model_entry(self):
        files = DshGenerator().generate(_context(), _args())
        entry = _entry_by_id(_patch(files), "agent-default-model")
        self.assertEqual(entry["config"]["provider"], DEEPSEEK_PROVIDER)
        self.assertEqual(entry["config"]["model"], "cat.deepseek.deepseek-v4-flash")

    def test_token_refresh_plugin_insert_present(self):
        files = DshGenerator().generate(_context(), _args())
        patch = _patch(files)
        insert_entry = next(
            (i for i in patch if isinstance(i, dict) and "insert" in i),
            None,
        )
        self.assertIsNotNone(insert_entry, "expected an 'insert' entry in the patch")
        names = [row.get("name") for row in insert_entry["insert"]]
        self.assertIn(PLUGIN_REF_RELATIVE, names)

    def test_gateway_safety_plugins_disabled(self):
        files = DshGenerator().generate(_context(), _args())
        patch = _patch(files)
        disabled_ids = {
            i["id"]
            for i in patch
            if isinstance(i, dict) and i.get("disabled") is True
        }
        self.assertIn("plugin-package-inventory-deepseek", disabled_ids)
        self.assertIn("session-log-deepseek", disabled_ids)


class ThinkingFlagTest(unittest.TestCase):
    def test_thinking_disabled_by_default(self):
        files = DshGenerator().generate(_context(), _args(thinking=False))
        entry = _entry_by_id(_patch(files), "llm-deepseek")
        self.assertEqual(entry["config"]["thinking"], "disabled")

    def test_thinking_enabled_with_flag(self):
        files = DshGenerator().generate(_context(), _args(thinking=True))
        entry = _entry_by_id(_patch(files), "llm-deepseek")
        self.assertEqual(entry["config"]["thinking"], "enabled")


class PluginFileTest(unittest.TestCase):
    def test_plugin_file_emitted(self):
        files = DshGenerator().generate(_context(), _args())
        self.assertIn(f"dsh/{PLUGIN_FILENAME}", files)

    def test_plugin_contains_host_and_profile(self):
        files = DshGenerator().generate(_context(), _args())
        plugin_src = files[f"dsh/{PLUGIN_FILENAME}"]
        self.assertIn(HOST, plugin_src)
        self.assertIn(PROFILE, plugin_src)


class NoDeepSeekEndpointTest(unittest.TestCase):
    def test_raises_when_no_deepseek_endpoints(self):
        ep = Endpoint(
            key="openai/gpt",
            schema="openai",
            name="gpt",
            full_name="cat.openai.gpt",
            foundation_model="models/system.ai.gpt",
            inference_table=None,
        )
        ctx = GatewayContext(
            host=HOST,
            catalog_name="cat",
            provider_schemas={"openai": "cat.openai"},
            endpoints=[ep],
        )
        with self.assertRaises(SystemExit):
            DshGenerator().generate(ctx, _args())


class NoUnsupportedHardeningKnobsTest(unittest.TestCase):
    """DSH refuses NODE_EXTRA_CA_CERTS / SSL_CERT_FILE in any .env/config layer
    (bootstrap-only, exported before launch) and has no config-enforceable version
    floor, so the generator must not leak one into the patch or plugin."""

    def test_no_ca_or_version_keys_emitted(self):
        files = DshGenerator().generate(_context(), _args())
        blob = "".join(files.values())
        for forbidden in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "SSL_CERT_DIR",
                          "minVersion", "minimum_version", "requiredMinimumVersion"):
            self.assertNotIn(forbidden, blob, f"{forbidden} unexpectedly emitted")


if __name__ == "__main__":
    unittest.main()
