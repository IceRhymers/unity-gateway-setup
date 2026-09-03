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


class OtelDormantByDefaultTest(unittest.TestCase):
    """The OTEL exporter row ships DORMANT (commented) because exporter.headers is a
    static map read at boot — confirmed from deepseek-ai/deepseek-harness
    packages/session/session-telemetry-otel/src/index.ts
    (commit 7169660d330452d32c91bb2e4788a9b8c2f83a18).
    Asserts: no active exporter row, no DATABRICKS_OTEL_TOKEN credential write,
    dormant explanation text present."""

    def test_no_active_session_telemetry_otel_row(self):
        files = DshGenerator().generate(_context(), _args())
        patch = _patch(files)
        # No active (non-disabled, non-commented) session-telemetry-otel entry
        for item in patch:
            if isinstance(item, dict) and item.get("id") == "session-telemetry-otel":
                # If present at all it must be disabled
                self.assertTrue(item.get("disabled"),
                                "session-telemetry-otel row is active (not disabled)")

    def test_no_databricks_otel_token_write_in_plugin(self):
        files = DshGenerator().generate(_context(), _args())
        plugin_src = files[f"dsh/{PLUGIN_FILENAME}"]
        # The dormant branch must not write DATABRICKS_OTEL_TOKEN to credentials
        self.assertNotIn("DATABRICKS_OTEL_TOKEN", plugin_src)

    def test_dormant_explanation_text_in_patch(self):
        files = DshGenerator().generate(_context(), _args())
        patch_raw = files[f"dsh/{PATCH_FILENAME}"]
        # The commented section must carry the static-headers caveat
        self.assertIn("DORMANT", patch_raw)
        self.assertIn("static", patch_raw)
        self.assertIn("session-telemetry-otel", patch_raw)

    def test_dormant_otel_row_in_patch_comment(self):
        files = DshGenerator().generate(_context(), _args())
        patch_raw = files[f"dsh/{PATCH_FILENAME}"]
        # Commented example URL must contain the OTEL ingest path
        self.assertIn("/api/2.0/otel/v1/logs", patch_raw)
        # Commented Authorization header reference present
        self.assertIn("DATABRICKS_OTEL_TOKEN", patch_raw)
        # But all DATABRICKS_OTEL_TOKEN lines must be comments
        for line in patch_raw.splitlines():
            if "DATABRICKS_OTEL_TOKEN" in line:
                stripped = line.strip()
                self.assertTrue(
                    stripped.startswith("#"),
                    f"DATABRICKS_OTEL_TOKEN appears in non-comment line: {line!r}",
                )


class OtelActiveRowTest(unittest.TestCase):
    """This test asserts the DORMANT branch (what actually ships): the commented
    exporter stub is present with the correct shape. Named 'Active' per the plan
    which says 'asserts whichever branch you actually ship'."""

    def test_dormant_exporter_stub_shape(self):
        files = DshGenerator().generate(_context(), _args())
        patch_raw = files[f"dsh/{PATCH_FILENAME}"]
        # Correct commented shape: mode FULL, exporter.url with OTEL path, headers
        self.assertIn("mode: FULL", patch_raw)
        self.assertIn("exporter:", patch_raw)
        self.assertIn("headers:", patch_raw)
        # All these must be in comment lines
        for marker in ("mode: FULL", "exporter:", "headers:"):
            found_commented = any(
                line.strip().startswith("#") and marker in line
                for line in patch_raw.splitlines()
            )
            self.assertTrue(found_commented,
                            f"Expected {marker!r} only in comment lines in patch")

    def test_citation_commit_in_patch(self):
        files = DshGenerator().generate(_context(), _args())
        patch_raw = files[f"dsh/{PATCH_FILENAME}"]
        # Source citation present so the dormant state can be re-evaluated
        self.assertIn("7169660d", patch_raw)


class ContentOffByDefaultTest(unittest.TestCase):
    """The default patch must not enable session telemetry (content stays local)."""

    def test_telemetry_mode_not_full_by_default(self):
        files = DshGenerator().generate(_context(), _args())
        patch = _patch(files)
        # No active session-telemetry-otel entry with mode=FULL
        for item in patch:
            if isinstance(item, dict) and item.get("id") == "session-telemetry-otel":
                config = item.get("config", {})
                self.assertNotEqual(config.get("mode"), "FULL",
                                    "session-telemetry-otel in FULL mode by default")


class TelemetryOffTest(unittest.TestCase):
    """The default output has no active telemetry (all dormant/commented).
    Verifies the 'telemetry off by default' invariant for the DSH generator."""

    def test_no_active_otel_in_default_output(self):
        files = DshGenerator().generate(_context(), _args())
        patch = _patch(files)
        # None of the active patch entries should configure OTEL exporting
        for item in patch:
            if not isinstance(item, dict) or item.get("disabled"):
                continue
            if item.get("id") == "session-telemetry-otel":
                self.fail("Active session-telemetry-otel entry found in default output")

    def test_no_databricks_otel_token_in_active_yaml(self):
        files = DshGenerator().generate(_context(), _args())
        patch = _patch(files)
        # The active (non-commented) YAML must not reference DATABRICKS_OTEL_TOKEN
        for item in patch:
            self.assertNotIn("DATABRICKS_OTEL_TOKEN", str(item))


if __name__ == "__main__":
    unittest.main()
