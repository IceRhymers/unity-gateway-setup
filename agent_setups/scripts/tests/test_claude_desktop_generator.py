"""Unit tests for the Claude Desktop config generator.

Builds an in-memory GatewayContext and Namespace. Never reads Terraform outputs
and never touches a real user config. Asserts the STRUCTURE of the emitted
importable claude-setup.json (schema v2 nested form): the gateway inference block,
the helper-script credential with the correct per-OS absolute command path, the
model list, the OTEL block, and the emitted credential/OTEL helper scripts.

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

from agents.claude_desktop import (  # noqa: E402
    CONFIG_FILENAME,
    CRED_HELPER_CMD,
    CRED_HELPER_PS1,
    CRED_HELPER_SH,
    OTEL_HELPER_CMD,
    OTEL_HELPER_PS1,
    OTEL_HELPER_SH,
    PLATFORM_INSTALL_DIRS,
    ClaudeDesktopGenerator,
)
from gateway import Endpoint, GatewayContext, Telemetry  # noqa: E402

HOST = "https://myws.cloud.databricks.com"
PROFILE = "fevm-west"


def _endpoints() -> list[Endpoint]:
    """An alias + a version pin for opus, plus a sonnet alias and a haiku pin."""
    return [
        Endpoint(key="anthropic/claude-opus", schema="anthropic", name="claude-opus",
                 full_name="cat.anthropic.claude-opus",
                 foundation_model="models/system.ai.claude-opus-4-8", inference_table=None),
        Endpoint(key="anthropic/claude-opus-4-8", schema="anthropic", name="claude-opus-4-8",
                 full_name="cat.anthropic.claude-opus-4-8",
                 foundation_model="models/system.ai.claude-opus-4-8", inference_table=None),
        Endpoint(key="anthropic/claude-sonnet", schema="anthropic", name="claude-sonnet",
                 full_name="cat.anthropic.claude-sonnet",
                 foundation_model="models/system.ai.claude-sonnet-4-6", inference_table=None),
        Endpoint(key="anthropic/claude-haiku-4-5", schema="anthropic", name="claude-haiku-4-5",
                 full_name="cat.anthropic.claude-haiku-4-5",
                 foundation_model="models/system.ai.claude-haiku-4-5", inference_table=None),
    ]


def _context(with_telemetry: bool = False) -> GatewayContext:
    tel = None
    if with_telemetry:
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
    return GatewayContext(
        host=HOST,
        catalog_name="cat",
        provider_schemas={"anthropic": "cat.anthropic"},
        endpoints=_endpoints(),
        telemetry=tel,
    )


def _args(**over) -> argparse.Namespace:
    base = dict(
        profile=PROFILE,
        skip_api_discovery=True,
        fallback_schema="anthropic",
        default_tier="sonnet",
        small_context=False,
        platforms="macos,windows",
        install_dir_macos=PLATFORM_INSTALL_DIRS["macos"],
        install_dir_windows=PLATFORM_INSTALL_DIRS["windows"],
        install_dir_linux=PLATFORM_INSTALL_DIRS["linux"],
        credential_ttl_sec=500,
        credential_timeout_sec=120,
        allow_websearch=False,
        egress_hosts="*",
        allow_claude_ai_signin=False,
        databricks_bin="databricks",
        telemetry="off",
        otel_log_content=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _macos_json(files: dict[str, str]) -> dict:
    return json.loads(files[f"claude-desktop/macos/{CONFIG_FILENAME}"])


def _windows_json(files: dict[str, str]) -> dict:
    return json.loads(files[f"claude-desktop/windows/{CONFIG_FILENAME}"])


class ConfigStructureTest(unittest.TestCase):
    def test_macos_json_parses_and_schema_version(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cfg = _macos_json(files)
        self.assertEqual(cfg["$schemaVersion"], 2)

    def test_inference_gateway_and_base_url(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cfg = _macos_json(files)
        self.assertEqual(cfg["inference"]["provider"], "gateway")
        self.assertEqual(cfg["inference"]["baseUrl"], f"{HOST}/ai-gateway/anthropic")

    def test_credential_is_helper_script_with_ttl_and_timeout(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cred = _macos_json(files)["inference"]["credential"]
        self.assertEqual(cred["kind"], "helper-script")
        self.assertEqual(cred["ttlSec"], 500)
        self.assertEqual(cred["timeoutSec"], 120)

    def test_macos_command_points_to_sh_absolute_path(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cmd = _macos_json(files)["inference"]["credential"]["command"]
        self.assertEqual(cmd, f"{PLATFORM_INSTALL_DIRS['macos']}/{CRED_HELPER_SH}")

    def test_windows_command_points_to_cmd_absolute_path(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cmd = _windows_json(files)["inference"]["credential"]["command"]
        self.assertEqual(cmd, f"{PLATFORM_INSTALL_DIRS['windows']}\\{CRED_HELPER_CMD}")

    def test_models_discovery_disabled_with_explicit_list(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        models = _macos_json(files)["models"]
        self.assertFalse(models["discoveryEnabled"])
        self.assertGreater(len(models["list"]), 0)
        # Names are the three-level UC full names.
        self.assertTrue(all(m["name"].count(".") == 2 for m in models["list"]))

    def test_default_tier_endpoint_is_first(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(default_tier="sonnet"))
        models = _macos_json(files)["models"]["list"]
        self.assertEqual(models[0]["name"], "cat.anthropic.claude-sonnet")
        self.assertEqual(models[0].get("anthropicFamilyTier"), "sonnet")

    def test_opus_family_gets_1m_context(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        models = _macos_json(files)["models"]["list"]
        opus_alias = next(m for m in models if m["name"] == "cat.anthropic.claude-opus")
        self.assertTrue(opus_alias["supports1m"])
        self.assertTrue(opus_alias["prefer1m"])

    def test_small_context_disables_1m(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(small_context=True))
        models = _macos_json(files)["models"]["list"]
        self.assertTrue(all(not m["supports1m"] for m in models))

    def test_alias_is_family_default_pin_is_not(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        models = {m["name"]: m for m in _macos_json(files)["models"]["list"]}
        self.assertTrue(models["cat.anthropic.claude-opus"]["isFamilyDefault"])
        self.assertFalse(models["cat.anthropic.claude-opus-4-8"]["isFamilyDefault"])

    def test_haiku_family_tier_set_no_1m(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        models = {m["name"]: m for m in _macos_json(files)["models"]["list"]}
        haiku = models["cat.anthropic.claude-haiku-4-5"]
        self.assertEqual(haiku.get("anthropicFamilyTier"), "haiku")
        self.assertFalse(haiku["supports1m"])

    def test_websearch_disabled_by_default(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cfg = _macos_json(files)
        self.assertIn("WebSearch", cfg["workspace"]["disabledBuiltinTools"])

    def test_allow_websearch_omits_disable(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(allow_websearch=True))
        cfg = _macos_json(files)
        self.assertNotIn("disabledBuiltinTools", cfg["workspace"])

    def test_disable_claude_ai_signin_by_default(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cfg = _macos_json(files)
        self.assertTrue(cfg["authentication"]["disableClaudeAiSignIn"])


class HelperScriptTest(unittest.TestCase):
    def test_macos_bundle_has_bash_helper_with_baked_profile(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertIn(f"claude-desktop/macos/{CRED_HELPER_SH}", files)
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn(PROFILE, sh)
        self.assertIn("access_token", sh)

    def test_windows_bundle_has_ps1_and_cmd_shim(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertIn(f"claude-desktop/windows/{CRED_HELPER_PS1}", files)
        self.assertIn(f"claude-desktop/windows/{CRED_HELPER_CMD}", files)
        cmd = files[f"claude-desktop/windows/{CRED_HELPER_CMD}"]
        # The shim runs the sibling .ps1.
        self.assertIn(CRED_HELPER_PS1, cmd)
        self.assertIn("powershell", cmd.lower())

    def test_windows_bundle_has_no_bash_helper(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertNotIn(f"claude-desktop/windows/{CRED_HELPER_SH}", files)

    def test_macos_bundle_has_no_windows_helpers(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertNotIn(f"claude-desktop/macos/{CRED_HELPER_PS1}", files)
        self.assertNotIn(f"claude-desktop/macos/{CRED_HELPER_CMD}", files)


class PlatformSelectionTest(unittest.TestCase):
    def test_only_requested_platforms_emitted(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="macos"))
        self.assertIn(f"claude-desktop/macos/{CONFIG_FILENAME}", files)
        self.assertNotIn(f"claude-desktop/windows/{CONFIG_FILENAME}", files)

    def test_unknown_platform_rejected(self):
        with self.assertRaises(SystemExit):
            ClaudeDesktopGenerator().generate(_context(), _args(platforms="beos"))

    def test_install_dir_override_reflected_in_command(self):
        files = ClaudeDesktopGenerator().generate(
            _context(), _args(platforms="macos", install_dir_macos="/opt/cd")
        )
        cmd = _macos_json(files)["inference"]["credential"]["command"]
        self.assertEqual(cmd, f"/opt/cd/{CRED_HELPER_SH}")


class TelemetryOffTest(unittest.TestCase):
    def test_no_otlp_block_when_off(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="off"))
        self.assertNotIn("otlp", _macos_json(files))

    def test_no_otel_helper_when_off(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="off"))
        self.assertNotIn(f"claude-desktop/macos/{OTEL_HELPER_SH}", files)

    def test_auto_without_telemetry_output_omits_otlp(self):
        # telemetry=auto but the context has no telemetry -> no otlp block, no error.
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=False), _args(telemetry="auto"))
        self.assertNotIn("otlp", _macos_json(files))

    def test_on_without_telemetry_output_raises(self):
        with self.assertRaises(SystemExit):
            ClaudeDesktopGenerator().generate(_context(with_telemetry=False), _args(telemetry="on"))


class TelemetryOnTest(unittest.TestCase):
    def test_otlp_block_present_and_routes_traces(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="on"))
        otlp = _macos_json(files)["otlp"]
        self.assertEqual(otlp["endpoint"], f"{HOST}/api/2.0/otel")
        self.assertTrue(otlp["tracesEnabled"])
        self.assertEqual(otlp["authMode"], "none")
        self.assertEqual(otlp["headers"]["X-Databricks-UC-Table-Name"], "cat.telemetry.otel_traces")

    def test_macos_headers_helper_points_to_sh(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="on"))
        otlp = _macos_json(files)["otlp"]
        self.assertEqual(otlp["headersHelper"], f"{PLATFORM_INSTALL_DIRS['macos']}/{OTEL_HELPER_SH}")
        self.assertIn(f"claude-desktop/macos/{OTEL_HELPER_SH}", files)

    def test_windows_headers_helper_points_to_cmd_with_ps1(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="on"))
        otlp = _windows_json(files)["otlp"]
        self.assertEqual(otlp["headersHelper"], f"{PLATFORM_INSTALL_DIRS['windows']}\\{OTEL_HELPER_CMD}")
        self.assertIn(f"claude-desktop/windows/{OTEL_HELPER_PS1}", files)
        self.assertIn(f"claude-desktop/windows/{OTEL_HELPER_CMD}", files)

    def test_otel_helper_down_scopes_to_traces_table(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="on"))
        sh = files[f"claude-desktop/macos/{OTEL_HELPER_SH}"]
        self.assertIn("cat.telemetry.otel_traces", sh)
        # Only the traces table is wired (not metrics/logs).
        self.assertNotIn("cat.telemetry.otel_metrics", sh)
        self.assertNotIn("cat.telemetry.otel_logs", sh)

    def test_content_capture_off_by_default(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="on"))
        self.assertNotIn("contentCapture", _macos_json(files)["otlp"])

    def test_content_capture_on_with_flag(self):
        files = ClaudeDesktopGenerator().generate(
            _context(with_telemetry=True), _args(telemetry="on", otel_log_content=True)
        )
        self.assertIn("contentCapture", _macos_json(files)["otlp"])


class ClaudeOnlyFilterTest(unittest.TestCase):
    """Claude Desktop rejects any model name without 'claude'; the generator must
    drop non-Claude anthropic-capable endpoints and error if none remain."""

    def _mixed_context(self) -> GatewayContext:
        eps = _endpoints() + [
            Endpoint(key="anthropic/gpt-oss", schema="anthropic", name="gpt-oss",
                     full_name="cat.anthropic.gpt-oss",
                     foundation_model="models/system.ai.gpt-oss", inference_table=None),
        ]
        return GatewayContext(host=HOST, catalog_name="cat",
                              provider_schemas={"anthropic": "cat.anthropic"}, endpoints=eps)

    def test_non_claude_endpoint_excluded(self):
        files = ClaudeDesktopGenerator().generate(self._mixed_context(), _args())
        names = [m["name"] for m in _macos_json(files)["models"]["list"]]
        self.assertNotIn("cat.anthropic.gpt-oss", names)
        self.assertIn("cat.anthropic.claude-opus", names)

    def test_all_non_claude_raises(self):
        ctx = GatewayContext(
            host=HOST, catalog_name="cat", provider_schemas={"anthropic": "cat.anthropic"},
            endpoints=[Endpoint(key="anthropic/gpt-oss", schema="anthropic", name="gpt-oss",
                                full_name="cat.anthropic.gpt-oss",
                                foundation_model="models/system.ai.gpt-oss", inference_table=None)],
        )
        with self.assertRaises(SystemExit):
            ClaudeDesktopGenerator().generate(ctx, _args())


class BakeableValidationTest(unittest.TestCase):
    def test_unsafe_profile_rejected(self):
        with self.assertRaises(SystemExit):
            ClaudeDesktopGenerator().generate(_context(), _args(profile='p";rm -rf /'))

    def test_unsafe_host_rejected(self):
        ctx = _context()
        bad = GatewayContext(
            host="https://ws.databricks.com'evil", catalog_name=ctx.catalog_name,
            provider_schemas=ctx.provider_schemas, endpoints=ctx.endpoints, telemetry=ctx.telemetry,
        )
        with self.assertRaises(SystemExit):
            ClaudeDesktopGenerator().generate(bad, _args())

    def test_valid_inputs_accepted(self):
        # A normal profile + host must not raise.
        files = ClaudeDesktopGenerator().generate(_context(), _args(profile="fevm-west"))
        self.assertIn(f"claude-desktop/macos/{CONFIG_FILENAME}", files)


class InstallNotesTest(unittest.TestCase):
    def test_notes_describe_import_then_export_flow(self):
        gen = ClaudeDesktopGenerator()
        gen.generate(_context(), _args())
        notes = gen.install_notes(_args())
        self.assertIn("Configure third-party inference", notes)
        self.assertIn("export", notes.lower())


if __name__ == "__main__":
    unittest.main()
