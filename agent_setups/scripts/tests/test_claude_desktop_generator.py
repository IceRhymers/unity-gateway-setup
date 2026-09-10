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
import plistlib
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
    DEFAULT_LAUNCHAGENT_LABEL,
    LAUNCHAGENT_PLIST,
    OTEL_HELPER_CMD,
    OTEL_HELPER_PS1,
    OTEL_HELPER_SH,
    PLATFORM_INSTALL_DIRS,
    SSO_BOOTSTRAP_SH,
    UG_GIT_URL,
    UV_CANDIDATE_PATHS,
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
        launchagent_label=DEFAULT_LAUNCHAGENT_LABEL,
        no_sso_bootstrap=False,
        ug_ref=None,
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
    def test_macos_bundle_has_sh_helper_delegating_to_ug(self):
        """ug ships one cross-platform token helper. We call it, never reimplement it."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        self.assertIn(f"claude-desktop/macos/{CRED_HELPER_SH}", files)
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn("auth-token", sh)
        # No reimplemented auth: no direct CLI token call, no JSON parse, no login.
        self.assertNotIn("access_token", sh)
        self.assertNotIn("databricks auth token", sh)
        self.assertNotIn("auth login", sh)

    def test_cred_helper_gives_ug_a_usable_path(self):
        """Claude Desktop runs the helper under launchd too, so ug needs the same PATH
        to find `databricks`. Without it every token refresh fails."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn("export PATH", sh)
        self.assertIn("/usr/local/bin", sh)

    def test_cred_helper_pins_the_workspace_host(self):
        """The token must come from the same workspace inference.baseUrl points at."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn(f'host="{HOST}"', sh)
        self.assertIn('auth-token --host "$host"', sh)

    def test_cred_helper_strips_ugs_trailing_newline(self):
        """`ug auth-token` emits token + "\n"; the credential contract wants bare."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn("printf '%s' \"$token\"", sh)

    def test_cred_helper_resolves_ug_without_path(self):
        """Claude Desktop starts under launchd with a minimal PATH."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertIn("UG_BIN", sh)
        self.assertIn("/.local/bin/ug", sh)

    def test_generated_sh_helper_is_posix(self):
        """/bin/sh is dash on Debian/Ubuntu; macOS /bin/sh is bash in POSIX mode,
        so a bashism passes locally and fails elsewhere."""
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        sh = files[f"claude-desktop/macos/{CRED_HELPER_SH}"]
        self.assertTrue(sh.startswith("#!/usr/bin/env sh"))
        for token in ("pipefail", "[[", "local ", "<<<", "declare ", "mapfile", "readarray"):
            offending = [ln for ln in sh.split("\n")
                         if token in ln and not ln.lstrip().startswith("#")]
            self.assertEqual(offending, [], f"uses {token!r}: {offending}")

    def test_windows_cred_helper_delegates_to_ug(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args())
        ps1 = files[f"claude-desktop/windows/{CRED_HELPER_PS1}"]
        self.assertIn("auth-token", ps1)
        self.assertIn("UG_BIN", ps1)
        self.assertIn(HOST, ps1)
        # The old hand-written PowerShell auth path is gone.
        self.assertNotIn("access_token", ps1)
        self.assertNotIn("ConvertFrom-Json", ps1)
        self.assertNotIn("auth login", ps1)

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


class FleetPathTest(unittest.TestCase):
    """A shipped bundle must carry no path that belongs to the build machine.

    `make claude-desktop-install-local` passes --install-dir-<os> "$HOME/..." on
    purpose, so a developer can test the helpers without root. That same flag
    makes a FLEET bundle undeployable. The helpers land in a home directory no
    other account has, and Claude Desktop then calls a credential helper that is
    not there. The generator defaults are machine-wide. These tests hold them
    there, so a local-testing invocation cannot ship by mistake.
    """

    def _macos(self, **over) -> dict[str, str]:
        return ClaudeDesktopGenerator().generate(
            _context(with_telemetry=True),
            _args(platforms="macos", telemetry="on", **over),
        )

    def test_no_build_machine_home_in_any_macos_file(self):
        """$HOME is correct in a script (the shell expands it on the device).
        An expanded /Users/<name> path is not: it names one build machine.
        """
        for name, body in self._macos().items():
            offenders = [ln.strip() for ln in body.splitlines() if "/Users/" in ln]
            self.assertEqual(offenders, [], msg=f"{name} carries a build-machine path")

    def test_json_helper_paths_are_machine_wide(self):
        cfg = _macos_json(self._macos())
        for key, path in (
            ("credential command", cfg["inference"]["credential"]["command"]),
            ("otlp headersHelper", cfg["otlp"]["headersHelper"]),
        ):
            self.assertTrue(
                path.startswith("/Library/"), msg=f"{key} is not machine-wide: {path}")

    def test_launchagent_runs_a_machine_wide_script(self):
        raw = self._macos()[f"claude-desktop/macos/{LAUNCHAGENT_PLIST}"]
        script = plistlib.loads(raw.encode())["ProgramArguments"][-1]
        self.assertTrue(
            script.startswith("/Library/"), msg=f"plist runs a non-fleet path: {script}")

    def test_a_home_install_dir_would_be_caught(self):
        """Positive control. Without it the assertions above could pass vacuously."""
        files = self._macos(install_dir_macos="/Users/dev/cd")
        cmd = _macos_json(files)["inference"]["credential"]["command"]
        self.assertIn("/Users/", cmd)
        script = plistlib.loads(
            files[f"claude-desktop/macos/{LAUNCHAGENT_PLIST}"].encode())["ProgramArguments"][-1]
        self.assertIn("/Users/", script)


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


class SsoBootstrapTest(unittest.TestCase):
    """The MDM-triggered SSO bootstrap: a guarded script plus a LaunchAgent.

    The guard is the load-bearing part. `ug configure` sets force_login whenever
    --use-pat is absent, so it opens a browser on every invocation. A login-
    triggered agent without the probe would nag at every login.
    """

    def _macos(self, **over) -> dict[str, str]:
        return ClaudeDesktopGenerator().generate(
            _context(), _args(platforms="macos", **over))

    def test_macos_bundle_has_script_and_launchagent(self):
        files = self._macos()
        self.assertIn(f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}", files)
        self.assertIn(f"claude-desktop/macos/{LAUNCHAGENT_PLIST}", files)

    def test_linux_bundle_has_script_but_no_launchagent(self):
        """A plist is a macOS artifact. Linux would need a systemd user unit."""
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="linux"))
        self.assertIn(f"claude-desktop/linux/{SSO_BOOTSTRAP_SH}", files)
        self.assertNotIn(f"claude-desktop/linux/{LAUNCHAGENT_PLIST}", files)

    def test_windows_bundle_has_neither(self):
        files = ClaudeDesktopGenerator().generate(_context(), _args(platforms="windows"))
        self.assertNotIn(f"claude-desktop/windows/{SSO_BOOTSTRAP_SH}", files)
        self.assertNotIn(f"claude-desktop/windows/{LAUNCHAGENT_PLIST}", files)

    def test_opt_out_omits_both(self):
        files = self._macos(no_sso_bootstrap=True)
        self.assertNotIn(f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}", files)
        self.assertNotIn(f"claude-desktop/macos/{LAUNCHAGENT_PLIST}", files)

    def test_script_guards_with_auth_token_probe(self):
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn('auth-token --host "$host"', sh)
        # It must exit before configuring when the probe passes.
        self.assertIn("exit 0", sh)

    def test_script_uses_workspaces_not_profiles(self):
        """--profiles raises when the profile is absent from ~/.databrickscfg,
        which is the state of a freshly imaged device. --workspaces takes a URL."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn('--workspaces "$host"', sh)
        self.assertNotIn("--profiles", sh)

    def test_script_never_uses_a_pat(self):
        """Only code lines matter. A comment may mention ~/.databrickscfg to explain
        why --workspaces is used instead of --profiles."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        code = [ln for ln in sh.split("\n") if not ln.lstrip().startswith("#")]
        for tok in ("--use-pat", "databrickscfg", "token ="):
            bad = [ln for ln in code if tok in ln]
            self.assertEqual(bad, [], f"code references {tok!r}: {bad}")

    def test_script_bakes_the_host(self):
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn(f'host="{HOST}"', sh)

    def test_script_resolves_ug_without_path(self):
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn("UG_BIN", sh)
        self.assertIn("/.local/bin/ug", sh)

    def test_script_logs_under_user_home_not_tmp(self):
        """A world-writable log path would let another local account pre-create it."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn('$HOME/Library/Logs', sh)
        self.assertNotIn("/tmp/", sh)

    def test_script_is_posix(self):
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertTrue(sh.startswith("#!/usr/bin/env sh"))
        for tok in ("pipefail", "[[", "local ", "<<<", "declare ", "mapfile", "readarray"):
            bad = [ln for ln in sh.split("\n")
                   if tok in ln and not ln.lstrip().startswith("#")]
            self.assertEqual(bad, [], f"uses {tok!r}: {bad}")

    def test_plist_is_valid_xml_with_expected_keys(self):
        raw = self._macos()[f"claude-desktop/macos/{LAUNCHAGENT_PLIST}"]
        d = plistlib.loads(raw.encode())
        self.assertEqual(d["Label"], DEFAULT_LAUNCHAGENT_LABEL)
        self.assertTrue(d["RunAtLoad"])
        # Aqua confines it to a GUI login session, where a browser can open.
        self.assertEqual(d["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(
            d["ProgramArguments"],
            ["/bin/sh", f"{PLATFORM_INSTALL_DIRS['macos']}/{SSO_BOOTSTRAP_SH}"],
        )

    def test_plist_has_no_keepalive(self):
        """KeepAlive would relaunch a dismissed login immediately. The next login retries."""
        d = plistlib.loads(self._macos()[f"claude-desktop/macos/{LAUNCHAGENT_PLIST}"].encode())
        self.assertNotIn("KeepAlive", d)

    def test_plist_script_path_follows_install_dir(self):
        files = self._macos(install_dir_macos="/opt/cd")
        d = plistlib.loads(files[f"claude-desktop/macos/{LAUNCHAGENT_PLIST}"].encode())
        self.assertEqual(d["ProgramArguments"][1], f"/opt/cd/{SSO_BOOTSTRAP_SH}")

    def test_custom_label_is_baked(self):
        files = self._macos(launchagent_label="com.example.my-sso")
        d = plistlib.loads(files[f"claude-desktop/macos/{LAUNCHAGENT_PLIST}"].encode())
        self.assertEqual(d["Label"], "com.example.my-sso")

    def test_script_gives_ug_a_usable_path(self):
        """A launchd job gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, and ug shells out to
        `databricks` BY BARE NAME. Without this export ug reports databricks missing
        and tries to sudo-install it, which cannot prompt from launchd."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn("export PATH", sh)
        for d in ("$HOME/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"):
            self.assertIn(d, sh)

    def test_script_installs_ug_when_absent(self):
        """The install is deferred to login because it is per-user. A package script
        runs as root, so it would install into root's home instead."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn("tool install", sh)
        self.assertIn(UG_GIT_URL, sh)

    def test_script_resolves_uv_by_absolute_path(self):
        """A LaunchAgent inherits a minimal PATH, so uv cannot come from $PATH."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn("UV_BIN", sh)
        for candidate in UV_CANDIDATE_PATHS:
            self.assertIn(candidate, sh)

    def test_script_never_installs_uv_itself(self):
        """uv is a prerequisite IT owns, because it installs per-user and
        self-updates. The script looks for it and reports it, nothing more."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        code = [ln for ln in sh.split("\n") if not ln.lstrip().startswith("#")]
        for tok in ("astral.sh", "curl ", "self update"):
            bad = [ln for ln in code if tok in ln]
            self.assertEqual(bad, [], f"code references {tok!r}: {bad}")

    def test_ug_install_is_unpinned_by_default(self):
        """Unpinned matches `ug upgrade`, which reinstalls from the same URL."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn(f'tool install "{UG_GIT_URL}"', sh)

    def test_ug_ref_pins_the_requirement(self):
        sh = self._macos(ug_ref="v1.2.3")[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn(f'tool install "{UG_GIT_URL}@v1.2.3"', sh)

    def test_unsafe_ug_ref_rejected(self):
        for bad in ('v1"; rm -rf /', "a b", "$(id)", "../../etc", "a;b"):
            with self.assertRaises(SystemExit, msg=f"accepted {bad!r}"):
                self._macos(ug_ref=bad)

    def test_missing_uv_does_not_abort_the_script(self):
        """No uv means log and retry at the next login, never a hard failure."""
        sh = self._macos()[f"claude-desktop/macos/{SSO_BOOTSTRAP_SH}"]
        self.assertIn("no uv to install it with", sh)
        self.assertIn("prerequisite", sh)

    def test_unsafe_label_rejected(self):
        for bad in ("com.example/../evil", "a b", "<script>", "-leading-dash"):
            with self.assertRaises(SystemExit, msg=f"accepted {bad!r}"):
                self._macos(launchagent_label=bad)


if __name__ == "__main__":
    unittest.main()
