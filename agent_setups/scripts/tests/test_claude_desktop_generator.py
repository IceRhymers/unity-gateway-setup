"""Unit tests for the Claude Desktop config generator.

Builds an in-memory GatewayContext and Namespace. Never reads Terraform outputs
and never touches a real user config.

The generator emits the MDM-OWNED half of a Claude Desktop setup: policy and
telemetry. It must NOT emit inference or models — ug owns the workspace, the
identity, and the model pins. So these tests assert both what claude-setup.json
contains (schema version, workspace policy, authentication, otlp) and what it
must not contain.

They also run the generated bootstrap script end to end against a fake
~/.ucode/state.json, which is where the inference + models block actually comes
from.

Run: python3 -m unittest discover -s agent_setups/scripts/tests
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from agents.claude_desktop import (  # noqa: E402
    BOOTSTRAP_FILENAME,
    CONFIG_FILENAME,
    CRED_HELPER_CMD,
    CRED_HELPER_PS1,
    CRED_HELPER_SH,
    MERGED_CONFIG_FILENAME,
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
    """A representative endpoint set.

    The generator deliberately ignores these now — model selection moved to ug.
    They are kept so GatewayContext is realistic, and so
    ModelsAreNotGeneratedTest can prove the endpoints have no effect.
    """
    return [
        Endpoint(key="anthropic/claude-opus", schema="anthropic", name="claude-opus",
                 full_name="cat.anthropic.claude-opus",
                 foundation_model="models/system.ai.claude-opus-4-8", inference_table=None),
        Endpoint(key="anthropic/claude-sonnet", schema="anthropic", name="claude-sonnet",
                 full_name="cat.anthropic.claude-sonnet",
                 foundation_model="models/system.ai.claude-sonnet-4-6", inference_table=None),
    ]


def _context(with_telemetry: bool = False, endpoints: list[Endpoint] | None = None) -> GatewayContext:
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
        endpoints=_endpoints() if endpoints is None else endpoints,
        telemetry=tel,
    )


def _args(**over) -> argparse.Namespace:
    base = dict(
        profile=PROFILE,
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

    def test_websearch_disabled_by_default(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cfg = _macos_json(files)
        self.assertIn("WebSearch", cfg["workspace"]["disabledBuiltinTools"])

    def test_allow_websearch_omits_disable(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(allow_websearch=True))
        cfg = _macos_json(files)
        self.assertNotIn("disabledBuiltinTools", cfg["workspace"])

    def test_egress_hosts_listed(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(egress_hosts="a.example,b.example"))
        cfg = _macos_json(files)
        self.assertEqual(cfg["workspace"]["allowedEgressHosts"], ["a.example", "b.example"])

    def test_disable_claude_ai_signin_by_default(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        cfg = _macos_json(files)
        self.assertTrue(cfg["authentication"]["disableClaudeAiSignIn"])

    def test_allow_claude_ai_signin_omits_lockout(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(allow_claude_ai_signin=True))
        cfg = _macos_json(files)
        self.assertNotIn("disableClaudeAiSignIn", cfg["authentication"])


class ModelsAreNotGeneratedTest(unittest.TestCase):
    """The trim: MDM pushes policy + telemetry, never inference or models.

    ug owns the workspace, the identity, and the model pins. If any of these keys
    come back, the generator has started fighting ug's published config again.
    """

    def test_no_inference_block(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertNotIn("inference", _macos_json(files))
        self.assertNotIn("inference", _windows_json(files))

    def test_no_models_block(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertNotIn("models", _macos_json(files))

    def test_no_chat_surface_or_extensions(self):
        # These are app-surface toggles the bootstrap writes, not MDM policy.
        cfg = _macos_json(ClaudeDesktopGenerator().generate(_context(), _args()))
        self.assertNotIn("chatSurface", cfg)
        self.assertNotIn("extensions", cfg)

    def test_mdm_config_keys_are_policy_and_telemetry_only(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="on"))
        self.assertEqual(
            set(_macos_json(files)),
            {"$schemaVersion", "workspace", "authentication", "otlp"},
        )

    def test_endpoints_do_not_affect_output(self):
        """Terraform endpoints are no longer an input, so changing them changes nothing."""
        gen = ClaudeDesktopGenerator()
        with_eps = gen.generate(_context(), _args(platforms="macos"))
        no_eps = gen.generate(_context(endpoints=[]), _args(platforms="macos"))
        self.assertEqual(with_eps, no_eps)

    def test_non_claude_endpoints_are_not_an_error(self):
        """The old generator raised when no Claude endpoint existed. Not its call now."""
        ctx = _context(endpoints=[
            Endpoint(key="anthropic/gpt-oss", schema="anthropic", name="gpt-oss",
                     full_name="cat.anthropic.gpt-oss",
                     foundation_model="models/system.ai.gpt-oss", inference_table=None),
        ])
        files = ClaudeDesktopGenerator().generate(ctx, _args(platforms="macos"))
        self.assertIn(f"claude-desktop/macos/{CONFIG_FILENAME}", files)


class HelperScriptTest(unittest.TestCase):
    def test_macos_bundle_has_bash_helper_with_baked_fallback_profile(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertIn(f"claude-desktop/macos/{CRED_HELPER_SH}", files)
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        # The profile is baked only as the no-ug-state-yet fallback.
        self.assertIn(PROFILE, sh)

    def test_cred_helper_delegates_to_ucode_auth_token(self):
        """ug ships one cross-platform token helper. We must call it, not reimplement it."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn("auth-token", sh)
        # No reimplemented auth logic: no CLI token call, no JSON parsing, no login.
        self.assertNotIn("access_token", sh)
        self.assertNotIn("databricks auth token", sh)
        self.assertNotIn("auth login", sh)

    def test_cred_helper_strips_ucode_trailing_newline(self):
        """`ucode auth-token` emits token + "\\n"; the credential contract wants bare."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn("printf '%s' \"$token\"", sh)

    def test_cred_helper_resolves_ucode_without_path(self):
        """Claude Desktop starts under launchd with a minimal PATH."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn("UCODE_BIN", sh)
        self.assertIn("/.local/bin/ucode", sh)

    def test_windows_cred_helper_delegates_to_ucode_auth_token(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        ps1 = files[f"claude-desktop/windows/{CRED_HELPER_PS1}"]
        self.assertIn("auth-token", ps1)
        self.assertIn("UCODE_BIN", ps1)
        # The old PowerShell auth path is gone: no JSON parse, no OAuth token call.
        self.assertNotIn("access_token", ps1)
        self.assertNotIn("ConvertFrom-Json", ps1)
        self.assertNotIn("auth login", ps1)

    def test_windows_bundle_has_ps1_and_cmd_shim(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertIn(f"claude-desktop/windows/{CRED_HELPER_PS1}", files)
        self.assertIn(f"claude-desktop/windows/{CRED_HELPER_CMD}", files)
        cmd = files[f"claude-desktop/windows/{CRED_HELPER_CMD}"]
        self.assertIn(CRED_HELPER_PS1, cmd)
        self.assertIn("powershell", cmd.lower())

    def test_windows_bundle_has_no_bash_helper(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertNotIn(f"claude-desktop/windows/{CRED_HELPER_SH}", files)

    def test_macos_bundle_has_no_windows_helpers(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertNotIn(f"claude-desktop/macos/{CRED_HELPER_PS1}", files)
        self.assertNotIn(f"claude-desktop/macos/{CRED_HELPER_CMD}", files)


class BootstrapEmissionTest(unittest.TestCase):
    def test_macos_bundle_has_bootstrap(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="macos"))
        self.assertIn(f"claude-desktop/macos/{BOOTSTRAP_FILENAME}", files)

    def test_windows_bundle_has_no_bootstrap(self):
        """The bootstrap is bash; a Windows port is a follow-up."""
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="windows"))
        self.assertNotIn(f"claude-desktop/windows/{BOOTSTRAP_FILENAME}", files)

    def test_bootstrap_bakes_cred_command_and_ttl(self):
        files = ClaudeDesktopGenerator().generate(
            _context(), _args(platforms="macos", install_dir_macos="/opt/cd",
                              credential_ttl_sec=321, credential_timeout_sec=99)
        )
        sh = files[f"claude-desktop/macos/{BOOTSTRAP_FILENAME}"]
        self.assertIn(f"/opt/cd/{CRED_HELPER_SH}", sh)
        self.assertIn("321", sh)
        self.assertIn("99", sh)

    def test_bootstrap_runs_ug_configure_for_claude(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="macos"))
        sh = files[f"claude-desktop/macos/{BOOTSTRAP_FILENAME}"]
        self.assertIn("configure --agents claude", sh)
        self.assertIn("--skip-validate", sh)

    def test_generated_shell_scripts_are_posix(self):
        """The runbook invokes these with `sh`, and /bin/sh is dash on Debian/Ubuntu.

        macOS /bin/sh is bash in POSIX mode, so a bashism passes locally and fails
        on CI. Assert the absence of the ones that actually bite.
        """
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="macos"))
        bashisms = ("pipefail", "[[", "local ", "<<<", "declare ", "mapfile", "readarray")
        for name in (BOOTSTRAP_FILENAME, CRED_HELPER_SH):
            script = files[f"claude-desktop/macos/{name}"]
            self.assertTrue(script.startswith("#!/usr/bin/env sh"), name)
            for token in bashisms:
                # Skip the comment lines that name the bashisms we are banning.
                offending = [
                    ln for ln in script.split("\n")
                    if token in ln and not ln.lstrip().startswith("#")
                ]
                self.assertEqual(offending, [], f"{name} uses {token!r}: {offending}")

    def test_bootstrap_has_no_placeholders_left(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="macos"))
        sh = files[f"claude-desktop/macos/{BOOTSTRAP_FILENAME}"]
        for placeholder in ("__CRED_COMMAND__", "__TTL_SEC__", "__TIMEOUT_SEC__",
                            "__CONFIG_FILENAME__", "__MERGED_FILENAME__"):
            self.assertNotIn(placeholder, sh)


@unittest.skipUnless(os.name == "posix", "the bootstrap is a POSIX shell script")
class BootstrapExecutionTest(unittest.TestCase):
    """Run the generated bootstrap against a fake ug state.

    This is where inference + models actually come from, so it is worth executing
    rather than only pattern-matching the template.
    """

    WORKSPACE = "https://myws.cloud.databricks.com"

    # /bin/sh is dash on Debian and Ubuntu but bash-in-POSIX-mode on macOS, so a
    # bashism passes on a developer laptop and fails on CI. Prefer a real dash when
    # the machine has one, so both environments exercise the same interpreter.
    SHELL = shutil.which("dash") or "sh"

    def _run(self, ug_state: dict, extra_args: list[str] | None = None,
             args_over: dict | None = None) -> tuple[int, dict | None, str]:
        files = ClaudeDesktopGenerator().generate(
            _context(with_telemetry=True),
            _args(platforms="macos", telemetry="on", **(args_over or {})),
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            script = tdp / BOOTSTRAP_FILENAME
            script.write_text(files[f"claude-desktop/macos/{BOOTSTRAP_FILENAME}"])
            script.chmod(0o755)
            (tdp / CONFIG_FILENAME).write_text(files[f"claude-desktop/macos/{CONFIG_FILENAME}"])
            state = tdp / "state.json"
            state.write_text(json.dumps(ug_state))

            env = dict(os.environ, UCODE_STATE=str(state))
            proc = subprocess.run(
                [self.SHELL, str(script), "--skip-configure", *(extra_args or [])],
                capture_output=True, text=True, env=env, cwd=td,
            )
            merged_path = tdp / MERGED_CONFIG_FILENAME
            merged = json.loads(merged_path.read_text()) if merged_path.exists() else None
            return proc.returncode, merged, proc.stdout + proc.stderr

    def _state(self, **ws_over) -> dict:
        ws = {
            "workspace": self.WORKSPACE,
            "profile": "fevm-west",
            "base_urls": {"claude": f"{self.WORKSPACE}/ai-gateway/anthropic"},
            "claude_models": {
                "opus": "cat.anthropic.claude-opus",
                "sonnet": "cat.anthropic.claude-sonnet",
                "haiku": "cat.anthropic.claude-haiku-4-5",
            },
        }
        ws.update(ws_over)
        return {"current_workspace": self.WORKSPACE, "workspaces": {self.WORKSPACE: ws}}

    def test_merges_inference_from_ug_base_url(self):
        rc, merged, out = self._run(self._state())
        self.assertEqual(rc, 0, out)
        self.assertEqual(merged["inference"]["provider"], "gateway")
        self.assertEqual(merged["inference"]["baseUrl"],
                         f"{self.WORKSPACE}/ai-gateway/anthropic")

    def test_credential_command_points_at_installed_helper(self):
        rc, merged, out = self._run(self._state())
        self.assertEqual(rc, 0, out)
        cred = merged["inference"]["credential"]
        self.assertEqual(cred["kind"], "helper-script")
        self.assertEqual(cred["command"], f"{PLATFORM_INSTALL_DIRS['macos']}/{CRED_HELPER_SH}")
        self.assertEqual(cred["ttlSec"], 500)
        self.assertEqual(cred["timeoutSec"], 120)

    def test_models_come_from_ug_pins(self):
        rc, merged, out = self._run(self._state())
        self.assertEqual(rc, 0, out)
        self.assertFalse(merged["models"]["discoveryEnabled"])
        names = [m["name"] for m in merged["models"]["list"]]
        self.assertEqual(sorted(names), [
            "cat.anthropic.claude-haiku-4-5",
            "cat.anthropic.claude-opus",
            "cat.anthropic.claude-sonnet",
        ])

    def test_default_tier_is_listed_first(self):
        rc, merged, out = self._run(self._state(), ["--default-tier", "opus"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(merged["models"]["list"][0]["name"], "cat.anthropic.claude-opus")

    def test_sonnet_is_default_tier_by_default(self):
        rc, merged, out = self._run(self._state())
        self.assertEqual(rc, 0, out)
        self.assertEqual(merged["models"]["list"][0]["anthropicFamilyTier"], "sonnet")

    def test_opus_and_sonnet_get_1m_haiku_does_not(self):
        rc, merged, out = self._run(self._state())
        self.assertEqual(rc, 0, out)
        by_tier = {m["anthropicFamilyTier"]: m for m in merged["models"]["list"]}
        self.assertTrue(by_tier["opus"]["supports1m"])
        self.assertTrue(by_tier["sonnet"]["prefer1m"])
        self.assertFalse(by_tier["haiku"]["supports1m"])

    def test_small_context_disables_1m(self):
        rc, merged, out = self._run(self._state(), ["--small-context"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(all(not m["supports1m"] for m in merged["models"]["list"]))

    def test_mdm_telemetry_and_policy_survive_the_merge(self):
        rc, merged, out = self._run(self._state())
        self.assertEqual(rc, 0, out)
        self.assertEqual(merged["otlp"]["headers"]["X-Databricks-UC-Table-Name"],
                         "cat.telemetry.otel_traces")
        self.assertTrue(merged["authentication"]["disableClaudeAiSignIn"])
        self.assertIn("WebSearch", merged["workspace"]["disabledBuiltinTools"])
        self.assertEqual(merged["$schemaVersion"], 2)

    def test_non_claude_pin_is_dropped(self):
        """Claude Desktop rejects any model name without 'claude'."""
        rc, merged, out = self._run(
            self._state(claude_models={"sonnet": "cat.anthropic.claude-sonnet",
                                       "opus": "cat.anthropic.gpt-oss"})
        )
        self.assertEqual(rc, 0, out)
        names = [m["name"] for m in merged["models"]["list"]]
        self.assertNotIn("cat.anthropic.gpt-oss", names)
        self.assertIn("cat.anthropic.claude-sonnet", names)

    def test_dry_run_writes_nothing_and_prints_config(self):
        files = ClaudeDesktopGenerator().generate(
            _context(with_telemetry=True), _args(platforms="macos", telemetry="on")
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            script = tdp / BOOTSTRAP_FILENAME
            script.write_text(files[f"claude-desktop/macos/{BOOTSTRAP_FILENAME}"])
            script.chmod(0o755)
            (tdp / CONFIG_FILENAME).write_text(files[f"claude-desktop/macos/{CONFIG_FILENAME}"])
            state = tdp / "state.json"
            state.write_text(json.dumps(self._state()))
            proc = subprocess.run(
                [self.SHELL, str(script), "--skip-configure", "--dry-run"],
                capture_output=True, text=True,
                env=dict(os.environ, UCODE_STATE=str(state)), cwd=td,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((tdp / MERGED_CONFIG_FILENAME).exists())
            self.assertIn("inference", json.loads(proc.stdout))

    def test_warns_when_telemetry_and_inference_workspaces_differ(self):
        """Traces to one workspace and inference to another is almost never intended."""
        other = "https://elsewhere.cloud.databricks.com"
        state = {
            "current_workspace": other,
            "workspaces": {other: {
                "workspace": other,
                "profile": "other-profile",
                "base_urls": {"claude": f"{other}/ai-gateway/anthropic"},
                "claude_models": {"sonnet": "cat.anthropic.claude-sonnet"},
            }},
        }
        rc, merged, out = self._run(state)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARNING", out)
        self.assertIn("different", out)
        # It still emits a config; the operator decides what to do about it.
        self.assertEqual(merged["inference"]["baseUrl"], f"{other}/ai-gateway/anthropic")

    def test_no_warning_when_workspaces_match(self):
        rc, merged, out = self._run(self._state())
        self.assertEqual(rc, 0, out)
        self.assertNotIn("WARNING", out)

    def test_missing_base_url_exits_4(self):
        rc, merged, out = self._run(self._state(base_urls={}))
        self.assertEqual(rc, 4, out)
        self.assertIsNone(merged)

    def test_no_claude_pins_exits_4(self):
        rc, merged, out = self._run(self._state(claude_models={}))
        self.assertEqual(rc, 4, out)

    def test_all_pins_non_claude_exits_4(self):
        rc, merged, out = self._run(self._state(claude_models={"opus": "cat.anthropic.gpt-oss"}))
        self.assertEqual(rc, 4, out)

    def test_use_pat_without_profile_is_a_usage_error(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="macos"))
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / BOOTSTRAP_FILENAME
            script.write_text(files[f"claude-desktop/macos/{BOOTSTRAP_FILENAME}"])
            script.chmod(0o755)
            proc = subprocess.run(
                [self.SHELL, str(script), "--use-pat"],
                capture_output=True, text=True,
                env={k: v for k, v in os.environ.items() if k != "DATABRICKS_PROFILE"},
                cwd=td,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)


class PlatformSelectionTest(unittest.TestCase):
    def test_only_requested_platforms_emitted(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="macos"))
        self.assertIn(f"claude-desktop/macos/{CONFIG_FILENAME}", files)
        self.assertNotIn(f"claude-desktop/windows/{CONFIG_FILENAME}", files)

    def test_unknown_platform_rejected(self):
        with self.assertRaises(SystemExit):
            ClaudeDesktopGenerator().generate(_context(), _args(platforms="beos"))

    def test_linux_bundle_gets_bootstrap_and_bash_helper(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="linux"))
        self.assertIn(f"claude-desktop/linux/{BOOTSTRAP_FILENAME}", files)
        self.assertIn(f"claude-desktop/linux/{CRED_HELPER_SH}", files)


class TelemetryOffTest(unittest.TestCase):
    def test_no_otlp_block_when_off(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="off"))
        self.assertNotIn("otlp", _macos_json(files))

    def test_no_otel_helper_when_off(self):
        files = ClaudeDesktopGenerator().generate(_context(with_telemetry=True), _args(telemetry="off"))
        self.assertNotIn(f"claude-desktop/macos/{OTEL_HELPER_SH}", files)

    def test_auto_without_telemetry_output_omits_otlp(self):
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
        files = ClaudeDesktopGenerator().generate(_context(), _args(profile="fevm-west"))
        self.assertIn(f"claude-desktop/macos/{CONFIG_FILENAME}", files)


class InstallNotesTest(unittest.TestCase):
    def test_notes_describe_the_ug_bootstrap_flow(self):
        gen = ClaudeDesktopGenerator()
        gen.generate(_context(), _args())
        notes = gen.install_notes(_args())
        self.assertIn(BOOTSTRAP_FILENAME, notes)
        self.assertIn("Configure third-party inference", notes)
        self.assertIn("ug", notes)

    def test_notes_say_mdm_does_not_own_models(self):
        gen = ClaudeDesktopGenerator()
        notes = gen.install_notes(_args())
        self.assertIn("POLICY + TELEMETRY", notes)


if __name__ == "__main__":
    unittest.main()
