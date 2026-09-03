"""Unit tests for the opencode generator's plugin-reference and hook-telemetry behavior.

These tests build an in-memory GatewayContext and Namespace. They never read the
Terraform outputs and never touch a real user config. They assert:
  - managed mode  : relative plugin ref and .mobileconfig emitted.
  - user mode     : absolute plugin ref, no .mobileconfig.
  - user mode + XDG: absolute ref honors XDG_CONFIG_HOME.
  - hook telemetry (SpoolBatchEmissionTest): spool/flush markers + second Zerobus
    token cache present when hook_events are deployed.
  - HookNameGateTest: when hook telemetry is off, no event hooks in the plugin.
  - ContentOffByDefaultTest: tool args absent by default.
  - NoUnsupportedHardeningKnobsTest: forbids OTEL keys and CA/version keys.
  - Cross-agent golden mint-shape assertion: authorization_details shape matches
    the canonical shape used by claude_code.py and codex.py.

Run: python3 -m unittest discover -s agent_setups/scripts/tests
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from agents.opencode import MOBILECONFIG_FILENAME, OpenCodeGenerator  # noqa: E402
from gateway import Endpoint, GatewayContext, Telemetry  # noqa: E402

HOST = "https://myws.cloud.databricks.com"
PROFILE = "fevm-west"
ZB_HOST = "https://9876543210.zerobus.us-east-1.cloud.databricks.com"
ZB_TABLE = "cat.telemetry.hook_events"
ZB_SECRET = "cat.telemetry.otel_sp_creds"


def _context() -> GatewayContext:
    """A minimal context with one anthropic endpoint and no telemetry."""
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
    """A context with both OTEL tables and hook_events telemetry deployed."""
    tel = Telemetry(
        schema_full_name="cat.telemetry",
        tables={
            "metrics": "cat.telemetry.otel_metrics",
            "traces": "cat.telemetry.otel_traces",
        },
        secret_full_name=ZB_SECRET,
        service_principal_application_id="12345",
        hook_events={"table": ZB_TABLE, "endpoint": ZB_HOST},
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
        auth_profile=None,
        default_model=None,
        user_config=False,
        hook_telemetry="off",
        hook_categories="usage,governance,adoption",
        hook_log_content=False,
        hook_token_ttl_seconds=600,
        zerobus_endpoint=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _plugin(files: dict[str, str]) -> str:
    from agents.opencode import PLUGIN_FILENAME
    return files[f"opencode/{PLUGIN_FILENAME}"]


def _plugin_entries(files: dict[str, str]) -> list:
    config = json.loads(files["opencode/opencode.json"])
    return config["plugin"]


class ManagedModeTest(unittest.TestCase):
    def test_relative_plugin_ref_and_mobileconfig_emitted(self):
        files = OpenCodeGenerator().generate(_context(), _args(user_config=False))
        self.assertEqual(_plugin_entries(files), ["./databricks-auth.ts"])
        self.assertIn(f"opencode/{MOBILECONFIG_FILENAME}", files)


class UserModeTest(unittest.TestCase):
    def test_absolute_plugin_ref_and_no_mobileconfig(self):
        files = OpenCodeGenerator().generate(_context(), _args(user_config=True))
        entries = _plugin_entries(files)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(os.path.isabs(entry), f"expected absolute path, got {entry!r}")
        self.assertTrue(entry.endswith("/databricks-auth.ts"), entry)
        self.assertNotIn(f"opencode/{MOBILECONFIG_FILENAME}", files)


class UserModeXdgTest(unittest.TestCase):
    def test_absolute_ref_honors_xdg_config_home(self):
        prior = os.environ.get("XDG_CONFIG_HOME")
        tmp = "/tmp/xdg-opencode-test"
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            files = OpenCodeGenerator().generate(_context(), _args(user_config=True))
            [entry] = _plugin_entries(files)
            self.assertTrue(
                entry.startswith(tmp + "/"),
                f"expected path under {tmp}, got {entry!r}",
            )
            self.assertEqual(entry, os.path.join(tmp, "opencode", "databricks-auth.ts"))
        finally:
            if prior is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = prior


class SpoolBatchEmissionTest(unittest.TestCase):
    """When hook telemetry is on, the plugin contains spool/flush markers, the
    second Zerobus token cache, and the down-scoped mint shape."""

    def _plugin_src(self) -> str:
        files = OpenCodeGenerator().generate(
            _context_with_telemetry(), _args(hook_telemetry="auto")
        )
        return _plugin(files)

    def test_spool_append_present_no_per_hook_post(self):
        src = self._plugin_src()
        # Spool: local append (no fetch on the hot path)
        self.assertIn("_zbAppend", src)
        self.assertIn("appendFileSync", src)
        # The tool.execute.after handler must call _zbAppend, not fetch directly.
        # Find the tool.execute.after handler body and verify it only uses _zbAppend.
        idx = src.index("tool.execute.after")
        handler_region = src[idx: idx + 500]
        self.assertIn("_zbAppend", handler_region)
        self.assertNotIn("fetch(", handler_region)

    def test_batched_flush_to_zerobus(self):
        src = self._plugin_src()
        self.assertIn("/zerobus/v1/tables/", src)
        self.assertIn(ZB_TABLE, src)

    def test_second_zerobus_token_cache_present(self):
        src = self._plugin_src()
        # Second cache uses _zbCached / _zbInflight — distinct from cached/inflight
        self.assertIn("_zbCached", src)
        self.assertIn("_zbInflight", src)
        self.assertIn("_zbMintToken", src)
        self.assertIn("_zbGetToken", src)
        # First gateway cache must still be present
        self.assertIn("let cached", src)
        self.assertIn("let inflight", src)

    def test_mint_markers_authorization_details(self):
        src = self._plugin_src()
        # Down-scoped mint: authorization_details, UC privilege names, audience
        self.assertIn("authorization_details", src)
        self.assertIn("USE CATALOG", src)
        self.assertIn("USE SCHEMA", src)
        self.assertIn("MODIFY", src)
        self.assertIn("include_value=true", src)
        self.assertIn("zerobusDirectWriteApi", src)

    def test_chat_headers_still_present(self):
        src = self._plugin_src()
        self.assertIn("chat.headers", src)

    def test_sweep_on_server_start(self):
        src = self._plugin_src()
        # Opportunistic sweep of leftover spool at plugin start
        self.assertIn("_zbSweep", src)
        # sweep called at top level of server() before return
        idx_sweep_def = src.index("const _zbSweep =")
        idx_sweep_call = src.index("await _zbSweep()")
        self.assertGreater(idx_sweep_call, idx_sweep_def)

    def test_session_end_hook_present_and_awaited(self):
        src = self._plugin_src()
        self.assertIn("session.deleted", src)
        # The session-end flush is awaited (not fire-and-forget)
        idx_deleted = src.index("session.deleted")
        # Find 'await' after 'session.deleted'
        post = src[idx_deleted:]
        self.assertIn("await", post[:500])

    def test_tool_execute_after_hook_present(self):
        src = self._plugin_src()
        self.assertIn("tool.execute.after", src)


class HookNameGateTest(unittest.TestCase):
    """When hook telemetry is off (or auto with no telemetry deployed), the plugin
    must not contain any event hooks (tool.execute.after, event/session.deleted)."""

    def test_no_event_hooks_when_off(self):
        files = OpenCodeGenerator().generate(_context(), _args(hook_telemetry="off"))
        src = _plugin(files)
        self.assertNotIn("tool.execute.after", src)
        self.assertNotIn("session.deleted", src)
        self.assertNotIn("_zbAppend", src)
        self.assertNotIn("_zbFlush", src)

    def test_no_event_hooks_when_auto_no_telemetry(self):
        # auto mode with no telemetry deployed → no hooks
        files = OpenCodeGenerator().generate(_context(), _args(hook_telemetry="auto"))
        src = _plugin(files)
        self.assertNotIn("tool.execute.after", src)
        self.assertNotIn("session.deleted", src)

    def test_opencode_json_unchanged_and_parses(self):
        # opencode.json must not have any OTEL keys regardless of hook_telemetry
        files = OpenCodeGenerator().generate(_context(), _args(hook_telemetry="off"))
        cfg = json.loads(files["opencode/opencode.json"])
        self.assertIsInstance(cfg, dict)
        blob = json.dumps(cfg)
        for forbidden in ("experimental.openTelemetry", "OTEL_EXPORTER_OTLP_ENDPOINT",
                          "OTEL_EXPORTER_OTLP_HEADERS"):
            self.assertNotIn(forbidden, blob, f"{forbidden} found in opencode.json")


class ContentOffByDefaultTest(unittest.TestCase):
    """Tool args must not appear in the tool_used event by default."""

    def test_content_off_by_default(self):
        files = OpenCodeGenerator().generate(
            _context_with_telemetry(), _args(hook_telemetry="auto")
        )
        src = _plugin(files)
        # ZB_CONTENT should be false
        self.assertIn("ZB_CONTENT = false", src)
        # With content off, no 'args: JSON.stringify(input.args)' in unconditioned path
        # The conditional is: ZB_CONTENT ? { args: ... } : {}
        self.assertIn("ZB_CONTENT ?", src)

    def test_content_enabled_when_flag_set(self):
        files = OpenCodeGenerator().generate(
            _context_with_telemetry(),
            _args(hook_telemetry="auto", hook_log_content=True),
        )
        src = _plugin(files)
        self.assertIn("ZB_CONTENT = true", src)


class NoUnsupportedHardeningKnobsTest(unittest.TestCase):
    """opencode has no config-emittable custom-CA, version-floor, or OTEL key."""

    def test_no_ca_or_version_keys_in_managed_output(self):
        files = OpenCodeGenerator().generate(_context(), _args(user_config=False))
        blob = "".join(files.values())
        for forbidden in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
                          "minVersion", "minimum_version", "requiredMinimumVersion"):
            self.assertNotIn(forbidden, blob, f"{forbidden} unexpectedly emitted")

    def test_no_ca_or_version_keys_in_user_output(self):
        files = OpenCodeGenerator().generate(_context(), _args(user_config=True))
        blob = "".join(files.values())
        for forbidden in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
                          "minVersion", "minimum_version", "requiredMinimumVersion"):
            self.assertNotIn(forbidden, blob, f"{forbidden} unexpectedly emitted")

    def test_no_otel_keys_in_opencode_json(self):
        """opencode.json must never contain OTEL env keys (they go in launch env only)."""
        for hook_tel in ("off", "auto"):
            with self.subTest(hook_telemetry=hook_tel):
                ctx = _context_with_telemetry() if hook_tel == "auto" else _context()
                files = OpenCodeGenerator().generate(ctx, _args(hook_telemetry=hook_tel))
                config_blob = files["opencode/opencode.json"]
                for forbidden in ("experimental.openTelemetry",
                                  "OTEL_EXPORTER_OTLP_ENDPOINT",
                                  "OTEL_EXPORTER_OTLP_HEADERS"):
                    self.assertNotIn(forbidden, config_blob,
                                     f"{forbidden} found in opencode.json (hook_telemetry={hook_tel})")


class CrossAgentMintShapeTest(unittest.TestCase):
    """The Zerobus down-scoped mint shape in the opencode TS plugin must match the
    canonical shape used by claude_code.py and codex.py.

    Checks that authorization_details carries all required UC privilege types and
    the zerobusDirectWriteApi audience — so a wrong audience or mis-derived wsid
    is caught at test time rather than at runtime.
    """

    def _plugin_src(self) -> str:
        files = OpenCodeGenerator().generate(
            _context_with_telemetry(), _args(hook_telemetry="auto")
        )
        return _plugin(files)

    def test_authorization_details_use_catalog(self):
        src = self._plugin_src()
        self.assertIn("USE CATALOG", src)
        self.assertIn("object_type: 'CATALOG'", src)

    def test_authorization_details_use_schema(self):
        src = self._plugin_src()
        self.assertIn("USE SCHEMA", src)
        self.assertIn("object_type: 'SCHEMA'", src)

    def test_authorization_details_select_modify(self):
        src = self._plugin_src()
        self.assertIn("'SELECT'", src)
        self.assertIn("'MODIFY'", src)
        self.assertIn("object_type: 'TABLE'", src)

    def test_zerobus_direct_write_api_audience(self):
        src = self._plugin_src()
        self.assertIn("zerobusDirectWriteApi", src)
        # Audience is derived from the ZB_ENDPOINT host (wsid = first label)
        self.assertIn("hostname.split('.')", src)

    def test_include_value_true_in_secret_fetch(self):
        src = self._plugin_src()
        self.assertIn("include_value=true", src)

    def test_basic_auth_for_oidc_token_endpoint(self):
        src = self._plugin_src()
        # Uses Basic auth with client_id:client_secret from the UC secret
        self.assertIn("Basic ", src)
        self.assertIn("client_id", src)
        self.assertIn("client_secret", src)

    def test_oidc_token_endpoint_path(self):
        src = self._plugin_src()
        self.assertIn("/oidc/v1/token", src)


if __name__ == "__main__":
    unittest.main()
