"""Unit tests for the `mcp` installer (discovery filter + pagination + merge).

These tests use tmp fixtures only. They never touch a real user config and never
make a live Databricks API call: discovery is exercised with an injected fake API
response or an injected command-runner, and the merge functions take explicit tmp
paths.

Run: python3 -m unittest discover -s agent_setups/scripts/tests
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import generate  # noqa: E402
import mcp_install  # noqa: E402
import mcp_menu  # noqa: E402
from gateway import discover_mcp_services, filter_mcp_services  # noqa: E402

HOST = "https://myws.cloud.databricks.com"
PROFILE = "fevm-west"


def _fake_api_services() -> list[dict]:
    """A fake `mcp_services` list, as the LIST API returns it (with the name prefix)."""
    return [
        {"name": "mcp-services/cat_a.tools.search", "securable_type": "MCP_SERVICE"},
        {"name": "mcp-services/cat_a.tools.web-fetch", "securable_type": "MCP_SERVICE"},
        {"name": "mcp-services/cat_a.data.query", "securable_type": "MCP_SERVICE"},
        {"name": "mcp-services/cat_b.tools.other", "securable_type": "MCP_SERVICE"},
        # Non-MCP securable and a malformed name must be dropped.
        {"name": "mcp-services/cat_a.tools.not_mcp", "securable_type": "FUNCTION"},
        {"name": "mcp-services/bad_name", "securable_type": "MCP_SERVICE"},
    ]


class TmpMixin(unittest.TestCase):
    def _tmpdir(self) -> Path:
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return Path(d)


class DiscoveryFilterTest(unittest.TestCase):
    def test_filters_by_catalog_and_strips_prefix(self):
        names = filter_mcp_services(_fake_api_services(), catalogs=["cat_a"])
        self.assertEqual(
            names,
            ["cat_a.data.query", "cat_a.tools.search", "cat_a.tools.web-fetch"],
        )
        self.assertNotIn("cat_b.tools.other", names)

    def test_filters_by_schema(self):
        names = filter_mcp_services(_fake_api_services(), catalogs=["cat_a"], schemas=["tools"])
        self.assertEqual(names, ["cat_a.tools.search", "cat_a.tools.web-fetch"])

    def test_multiple_catalogs(self):
        names = filter_mcp_services(_fake_api_services(), catalogs=["cat_a", "cat_b"], schemas=["tools"])
        self.assertEqual(
            names,
            ["cat_a.tools.search", "cat_a.tools.web-fetch", "cat_b.tools.other"],
        )


class DiscoveryPaginationTest(unittest.TestCase):
    """discover_mcp_services with an injected command-runner (no subprocess/network)."""

    def test_accumulates_pages_and_terminates_on_missing_token(self):
        responses = [
            '{"mcp_services":[{"name":"mcp-services/c.s.a","securable_type":"MCP_SERVICE"}],'
            '"next_page_token":"tok 1"}',
            '{"mcp_services":[{"name":"mcp-services/c.s.b","securable_type":"MCP_SERVICE"}],'
            '"next_page_token":"t2"}',
            '{"mcp_services":[{"name":"mcp-services/c.s.c","securable_type":"MCP_SERVICE"}]}',
        ]
        calls: list[str] = []
        it = iter(responses)

        def runner(endpoint: str) -> str:
            calls.append(endpoint)
            return next(it)

        names = discover_mcp_services(["c"], PROFILE, runner=runner)
        self.assertEqual(names, ["c.s.a", "c.s.b", "c.s.c"])
        self.assertEqual(len(calls), 3)  # stopped when the last page had no token
        self.assertNotIn("page_token", calls[0])
        # The page token with a space is URL-encoded in the next request.
        self.assertIn("page_token=tok%201", calls[1])
        self.assertIn("page_token=t2", calls[2])

    def test_empty_stdout_yields_no_services(self):
        names = discover_mcp_services(["c"], PROFILE, runner=lambda endpoint: "")
        self.assertEqual(names, [])

    def test_repeated_token_aborts(self):
        def runner(endpoint: str) -> str:
            return '{"mcp_services":[],"next_page_token":"same"}'

        with self.assertRaises(SystemExit):
            discover_mcp_services(["c"], PROFILE, runner=runner)

    def test_bad_json_aborts(self):
        with self.assertRaises(SystemExit):
            discover_mcp_services(["c"], PROFILE, runner=lambda endpoint: "not json")


class ServiceBuildTest(unittest.TestCase):
    def test_key_includes_catalog_and_url(self):
        svcs = mcp_install.build_services(["cat_a.tools.web-fetch"], HOST, PROFILE)
        self.assertEqual(len(svcs), 1)
        svc = svcs[0]
        # Catalog is part of the key; `-` in the leaf becomes `_`.
        self.assertEqual(svc.server_key, "uc_cat_a_tools_web_fetch")
        self.assertEqual(svc.gateway_url, f"{HOST}/ai-gateway/mcp-services/cat_a.tools.web-fetch")

    def test_dotted_catalog_schema_name_sanitized(self):
        # system.ai.slack -> uc_system_ai_slack
        [svc] = mcp_install.build_services(["system.ai.slack"], HOST, PROFILE)
        self.assertEqual(svc.server_key, "uc_system_ai_slack")

    def test_custom_prefix(self):
        svcs = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE, prefix="gw_")
        self.assertEqual(svcs[0].server_key, "gw_cat_a_tools_search")

    def test_cross_catalog_distinct_keys(self):
        svcs = mcp_install.build_services(
            ["cat_a.tools.search", "cat_b.tools.search"], HOST, PROFILE
        )
        keys = sorted(s.server_key for s in svcs)
        self.assertEqual(keys, ["uc_cat_a_tools_search", "uc_cat_b_tools_search"])

    def test_collision_guard_raises_when_forced(self):
        # `cat-a` and `cat_a` both sanitize to `cat_a`, forcing one key.
        with self.assertRaises(SystemExit):
            mcp_install.build_services(["cat-a.tools.x", "cat_a.tools.x"], HOST, PROFILE)

    def test_command_shapes_split_vs_array(self):
        [svc] = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE)
        url = svc.gateway_url
        self.assertEqual(
            mcp_install.claude_entry(svc, PROFILE),
            {"type": "stdio", "command": "uvx",
             "args": ["uc-mcp-proxy", "--url", url, "--profile", PROFILE]},
        )
        self.assertEqual(
            mcp_install.codex_entry(svc, PROFILE),
            {"command": "uvx",
             "args": ["uc-mcp-proxy", "--url", url, "--profile", PROFILE]},
        )
        self.assertEqual(
            mcp_install.opencode_entry(svc, PROFILE),
            {"type": "local",
             "command": ["uvx", "uc-mcp-proxy", "--url", url, "--profile", PROFILE],
             "enabled": True},
        )


class ClaudeMergeTest(TmpMixin):
    def setUp(self):
        self._tmp = self._tmpdir()
        self.path = self._tmp / ".claude.json"

    def _services(self, names):
        return mcp_install.build_services(names, HOST, PROFILE)

    def test_preserves_other_keys_and_non_prefixed_servers(self):
        self.path.write_text(json.dumps({
            "numStartups": 7,
            "mcpServers": {
                "my_custom": {"type": "stdio", "command": "foo", "args": []},
            },
        }, indent=2) + "\n")
        svcs = self._services(["cat_a.tools.search"])
        mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["numStartups"], 7)
        self.assertIn("my_custom", data["mcpServers"])
        self.assertIn("uc_cat_a_tools_search", data["mcpServers"])

    def test_stale_removed_current_upserted(self):
        self.path.write_text(json.dumps({
            "mcpServers": {
                "uc_cat_a_tools_gone": {"type": "stdio", "command": "uvx", "args": ["old"]},
                "keepme": {"type": "stdio", "command": "x", "args": []},
            },
        }, indent=2) + "\n")
        svcs = self._services(["cat_a.tools.search"])
        mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        servers = json.loads(self.path.read_text())["mcpServers"]
        self.assertNotIn("uc_cat_a_tools_gone", servers)
        self.assertIn("uc_cat_a_tools_search", servers)
        self.assertIn("keepme", servers)

    def test_backup_created_before_write_and_byte_equals_original(self):
        original = json.dumps({"mcpServers": {}}, indent=2) + "\n"
        self.path.write_text(original)
        original_bytes = self.path.read_bytes()
        svcs = self._services(["cat_a.tools.search"])
        result = mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        self.assertTrue(result.written)
        self.assertIsNotNone(result.backup)
        backups = list(self._tmp.glob(".claude.json.bak-*"))
        self.assertEqual(len(backups), 1)
        # The backup captures the pre-write bytes exactly.
        self.assertEqual(backups[0].read_bytes(), original_bytes)

    def test_idempotent_second_run_is_noop(self):
        self.path.write_text(json.dumps({"mcpServers": {}}, indent=2) + "\n")
        svcs = self._services(["cat_a.tools.search", "cat_a.data.query"])
        mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        first = self.path.read_bytes()
        result2 = mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        self.assertFalse(result2.changed)
        self.assertFalse(result2.written)
        self.assertIsNone(result2.backup)
        self.assertEqual(self.path.read_bytes(), first)
        self.assertEqual(len(list(self._tmp.glob(".claude.json.bak-*"))), 1)

    def test_dry_run_writes_nothing(self):
        svcs = self._services(["cat_a.tools.search"])
        result = mcp_install.install_harness("claude-code", svcs, PROFILE, self.path, dry_run=True)
        self.assertTrue(result.changed)
        self.assertFalse(result.written)
        self.assertFalse(self.path.exists())

    def test_empty_discovery_keeps_entries_unless_allowed(self):
        self.path.write_text(json.dumps({
            "mcpServers": {
                "uc_cat_a_tools_search": {"type": "stdio", "command": "uvx", "args": ["x"]},
                "keepme": {"type": "stdio", "command": "y", "args": []},
            },
        }, indent=2) + "\n")
        # Empty discovery, allow_empty False: no removal at all.
        r1 = mcp_install.install_harness("claude-code", [], PROFILE, self.path, allow_empty=False)
        self.assertFalse(r1.changed)
        servers = json.loads(self.path.read_text())["mcpServers"]
        self.assertIn("uc_cat_a_tools_search", servers)
        self.assertIn("keepme", servers)
        # Empty discovery, allow_empty True: prefixed entry removed, others kept.
        r2 = mcp_install.install_harness("claude-code", [], PROFILE, self.path, allow_empty=True)
        self.assertTrue(r2.changed)
        servers = json.loads(self.path.read_text())["mcpServers"]
        self.assertNotIn("uc_cat_a_tools_search", servers)
        self.assertIn("keepme", servers)

    def test_malformed_json_raises_no_write(self):
        self.path.write_text("{ this is not json ")
        before = self.path.read_bytes()
        svcs = self._services(["cat_a.tools.search"])
        with self.assertRaises(SystemExit):
            mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        self.assertEqual(self.path.read_bytes(), before)  # untouched

    def test_wrong_typed_key_raises_no_write(self):
        self.path.write_text(json.dumps({"mcpServers": [1, 2, 3]}) + "\n")
        before = self.path.read_bytes()
        svcs = self._services(["cat_a.tools.search"])
        with self.assertRaises(SystemExit):
            mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_content_file_is_seeded(self):
        self.path.write_text("")  # exists but empty
        svcs = self._services(["cat_a.tools.search"])
        mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        data = json.loads(self.path.read_text())
        self.assertIn("uc_cat_a_tools_search", data["mcpServers"])

    def test_realistic_populated_config_stable_and_preserved(self):
        # Resembles a real ~/.claude.json: many top-level keys, unicode path,
        # compact/non-canonical formatting.
        raw = (
            '{"numStartups":42,"autoUpdates":true,'
            '"projects":{"/Users/josé/dev/café":{"lastCwd":"/Users/josé/dev/café"}},'
            '"mcpServers":{"legacy":{"type":"stdio","command":"node","args":["s.js"]}},'
            '"tipsHistory":{"a":1}}'
        )
        self.path.write_text(raw)
        svcs = self._services(["cat_a.tools.search", "cat_b.tools.search"])
        mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        first = self.path.read_bytes()
        data = json.loads(first)
        # Non-uc_ entries and unrelated keys survive; unicode path preserved unescaped.
        self.assertIn("legacy", data["mcpServers"])
        self.assertIn("/Users/josé/dev/café", data["projects"])
        self.assertIn("/Users/josé/dev/café", first.decode("utf-8"))  # not \u-escaped
        self.assertEqual(data["numStartups"], 42)
        self.assertIn("uc_cat_a_tools_search", data["mcpServers"])
        self.assertIn("uc_cat_b_tools_search", data["mcpServers"])
        # Second run is a byte-identical no-op.
        r2 = mcp_install.install_harness("claude-code", svcs, PROFILE, self.path)
        self.assertFalse(r2.changed)
        self.assertEqual(self.path.read_bytes(), first)


class OpencodeMergeTest(TmpMixin):
    def setUp(self):
        self.path = self._tmpdir() / "opencode.json"

    def test_single_array_command_and_preserves_config(self):
        self.path.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "model": "x/y",
            "mcp": {"userthing": {"type": "local", "command": ["a"], "enabled": True}},
        }, indent=2) + "\n")
        svcs = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE)
        mcp_install.install_harness("opencode", svcs, PROFILE, self.path)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["$schema"], "https://opencode.ai/config.json")
        self.assertIn("userthing", data["mcp"])
        entry = data["mcp"]["uc_cat_a_tools_search"]
        self.assertEqual(entry["type"], "local")
        self.assertEqual(entry["command"][0], "uvx")
        self.assertEqual(entry["command"][1], "uc-mcp-proxy")
        self.assertTrue(entry["enabled"])


class CodexMergeTest(TmpMixin):
    def setUp(self):
        self._tmp = self._tmpdir()
        self.path = self._tmp / "config.toml"

    SEED = (
        "# my codex config\n"
        'model = "gpt-5"  # inline comment\n'
        "\n"
        "[model_providers.databricks]\n"
        'name = "Databricks"\n'
        "\n"
        "[mcp_servers.uc_cat_a_tools_gone]\n"
        'command = "uvx"\n'
        'args = ["uc-mcp-proxy", "--url", "old"]\n'
        "\n"
        "[mcp_servers.hand_written]\n"
        'command = "python"\n'
        'args = ["-m", "myserver"]\n'
    )

    def test_preserves_comments_other_tables_and_merges(self):
        self.path.write_text(self.SEED)
        svcs = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE)
        mcp_install.install_harness("codex", svcs, PROFILE, self.path)
        text = self.path.read_text()
        self.assertIn("# my codex config", text)
        self.assertIn("# inline comment", text)
        self.assertIn("[model_providers.databricks]", text)
        self.assertIn("[mcp_servers.hand_written]", text)
        self.assertNotIn("uc_cat_a_tools_gone", text)
        self.assertIn("[mcp_servers.uc_cat_a_tools_search]", text)

        import tomllib
        parsed = tomllib.loads(text)
        entry = parsed["mcp_servers"]["uc_cat_a_tools_search"]
        self.assertEqual(entry["command"], "uvx")
        self.assertEqual(entry["args"][0], "uc-mcp-proxy")
        self.assertNotIn("uc_cat_a_tools_gone", parsed["mcp_servers"])
        self.assertIn("hand_written", parsed["mcp_servers"])

    def test_backup_and_idempotency(self):
        self.path.write_text(self.SEED)
        original_bytes = self.path.read_bytes()
        svcs = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE)
        r1 = mcp_install.install_harness("codex", svcs, PROFILE, self.path)
        self.assertTrue(r1.written)
        self.assertIsNotNone(r1.backup)
        self.assertEqual(r1.backup.read_bytes(), original_bytes)  # backup == pre-write
        after_first = self.path.read_bytes()
        r2 = mcp_install.install_harness("codex", svcs, PROFILE, self.path)
        self.assertFalse(r2.changed)
        self.assertFalse(r2.written)
        self.assertEqual(self.path.read_bytes(), after_first)
        self.assertEqual(len(list(self._tmp.glob("config.toml.bak-*"))), 1)

    def test_dry_run_writes_nothing(self):
        self.path.write_text(self.SEED)
        original = self.path.read_bytes()
        svcs = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE)
        result = mcp_install.install_harness("codex", svcs, PROFILE, self.path, dry_run=True)
        self.assertTrue(result.changed)
        self.assertFalse(result.written)
        self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_toml_raises_no_write(self):
        self.path.write_text("this = = broken\n[unterminated\n")
        before = self.path.read_bytes()
        svcs = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE)
        with self.assertRaises(SystemExit):
            mcp_install.install_harness("codex", svcs, PROFILE, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_typed_mcp_servers_raises(self):
        self.path.write_text('mcp_servers = "oops"\n')
        before = self.path.read_bytes()
        svcs = mcp_install.build_services(["cat_a.tools.search"], HOST, PROFILE)
        with self.assertRaises(SystemExit):
            mcp_install.install_harness("codex", svcs, PROFILE, self.path)
        self.assertEqual(self.path.read_bytes(), before)


class RunMcpPrefixGuardTest(unittest.TestCase):
    def _args(self, prefix):
        return argparse.Namespace(server_prefix=prefix)

    def test_empty_prefix_rejected(self):
        with self.assertRaises(SystemExit):
            generate.run_mcp(self._args(""))

    def test_whitespace_prefix_rejected(self):
        with self.assertRaises(SystemExit):
            generate.run_mcp(self._args("   "))


class ParseSelectionTest(unittest.TestCase):
    DISCOVERED = ["cat_a.data.query", "cat_a.tools.search", "system.ai.slack"]

    def test_empty_confirms_preselected(self):
        pre = {"system.ai.slack"}
        self.assertEqual(mcp_install.parse_selection(self.DISCOVERED, pre, ""), pre)
        self.assertEqual(mcp_install.parse_selection(self.DISCOVERED, pre, "   "), pre)

    def test_all_selects_everything(self):
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), "all"),
            set(self.DISCOVERED),
        )

    def test_all_or_none_mixed_with_other_tokens_raises(self):
        for text in ("all,none", "all,slack", "1,none", "none,2"):
            with self.assertRaises(SystemExit):
                mcp_install.parse_selection(self.DISCOVERED, set(), text)

    def test_none_selects_nothing_even_with_preselected(self):
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, {"system.ai.slack"}, "none"),
            set(),
        )

    def test_numbers(self):
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), "1,3"),
            {"cat_a.data.query", "system.ai.slack"},
        )

    def test_names_leaf_schema_full_and_key(self):
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), "slack"),
            {"system.ai.slack"},
        )
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), "tools.search"),
            {"cat_a.tools.search"},
        )
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), "cat_a.data.query"),
            {"cat_a.data.query"},
        )
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), "uc_system_ai_slack"),
            {"system.ai.slack"},
        )

    def test_case_insensitive(self):
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), "SLACK"),
            {"system.ai.slack"},
        )

    def test_mixed_numbers_and_names_with_whitespace(self):
        self.assertEqual(
            mcp_install.parse_selection(self.DISCOVERED, set(), " 2 , slack "),
            {"cat_a.tools.search", "system.ai.slack"},
        )

    def test_out_of_range_number_raises(self):
        with self.assertRaises(SystemExit):
            mcp_install.parse_selection(self.DISCOVERED, set(), "9")

    def test_invalid_name_raises(self):
        with self.assertRaises(SystemExit):
            mcp_install.parse_selection(self.DISCOVERED, set(), "nope")

    def test_leaf_matches_across_catalogs(self):
        discovered = ["cat_a.tools.search", "cat_b.tools.search"]
        self.assertEqual(
            mcp_install.parse_selection(discovered, set(), "search"),
            set(discovered),
        )


class InstalledPrefixedKeysTest(TmpMixin):
    def test_reads_json_and_toml_and_tolerates_missing(self):
        tmp = self._tmpdir()
        claude = tmp / ".claude.json"
        claude.write_text(json.dumps({
            "mcpServers": {"uc_system_ai_slack": {}, "keepme": {}},
        }) + "\n")
        self.assertEqual(
            mcp_install.installed_prefixed_keys("claude-code", claude),
            {"uc_system_ai_slack"},
        )
        codex = tmp / "config.toml"
        codex.write_text(
            "[mcp_servers.uc_system_ai_slack]\ncommand = \"uvx\"\n"
            "[mcp_servers.hand]\ncommand = \"x\"\n"
        )
        self.assertEqual(
            mcp_install.installed_prefixed_keys("codex", codex),
            {"uc_system_ai_slack"},
        )
        # Missing file -> empty, never raises.
        self.assertEqual(
            mcp_install.installed_prefixed_keys("opencode", tmp / "nope.json"),
            set(),
        )


class RunMcpSelectionTest(TmpMixin):
    """End-to-end run_mcp selection with injected discovery and tmp config paths."""

    DISCOVERED = ["system.ai.search", "system.ai.slack"]

    def setUp(self):
        self._tmp = self._tmpdir()
        self.claude = self._tmp / ".claude.json"
        self.codex = self._tmp / "config.toml"
        self.opencode = self._tmp / "opencode.json"

    def _args(self, **over):
        base = dict(
            server_prefix=mcp_install.DEFAULT_SERVER_PREFIX,
            profile=PROFILE,
            host=HOST,
            catalog=["system"],
            schema=["ai"],
            harness=None,
            databricks_bin="databricks",
            list=False,
            all=False,
            select=None,
            allow_empty=False,
            dry_run=False,
            claude_config=self.claude,
            codex_config=self.codex,
            opencode_config=self.opencode,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _run(self, discovered=None, **over):
        from unittest import mock
        names = self.DISCOVERED if discovered is None else discovered
        with mock.patch.object(generate, "discover_mcp_services", return_value=names), \
             mock.patch.object(generate, "resolve_host", return_value=HOST):
            return generate.run_mcp(self._args(**over))

    def _keys(self, harness, path):
        return mcp_install.installed_prefixed_keys(harness, path)

    def test_enable_writes_only_selected_to_all_three(self):
        self._run(select=["slack"])
        for harness, path in (
            ("claude-code", self.claude),
            ("codex", self.codex),
            ("opencode", self.opencode),
        ):
            self.assertEqual(self._keys(harness, path), {"uc_system_ai_slack"})

    def test_all_writes_everything(self):
        self._run(all=True)
        self.assertEqual(
            self._keys("claude-code", self.claude),
            {"uc_system_ai_search", "uc_system_ai_slack"},
        )

    def test_enable_removes_stale_uc_entries(self):
        self.claude.write_text(json.dumps({
            "mcpServers": {
                "uc_system_ai_gone": {"type": "stdio", "command": "uvx", "args": ["old"]},
                "keepme": {"type": "stdio", "command": "x", "args": []},
            },
        }) + "\n")
        self._run(select=["slack"])
        servers = json.loads(self.claude.read_text())["mcpServers"]
        self.assertNotIn("uc_system_ai_gone", servers)
        self.assertIn("uc_system_ai_slack", servers)
        self.assertIn("keepme", servers)

    def test_select_is_declarative_removes_discovered_but_unselected(self):
        # Both discovered services are installed; selecting only `slack` must remove
        # the discovered-and-installed `search` (selection defines the complete set).
        self.claude.write_text(json.dumps({
            "mcpServers": {
                "uc_system_ai_search": {"type": "stdio", "command": "uvx", "args": ["old"]},
                "uc_system_ai_slack": {"type": "stdio", "command": "uvx", "args": ["old"]},
            },
        }) + "\n")
        self._run(select=["slack"])
        self.assertEqual(self._keys("claude-code", self.claude), {"uc_system_ai_slack"})

    def test_enable_unknown_token_raises(self):
        with self.assertRaises(SystemExit):
            self._run(select=["does-not-exist"])

    def test_list_prints_and_writes_nothing(self):
        import contextlib
        import io
        self.claude.write_text(json.dumps({
            "mcpServers": {"uc_system_ai_slack": {"type": "stdio", "command": "uvx", "args": []}},
        }) + "\n")
        before = self.claude.read_bytes()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._run(list=True)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        # Both discovered services listed; the installed one is pre-marked with `*`.
        self.assertIn("uc_system_ai_search", out)
        self.assertIn("uc_system_ai_slack", out)
        self.assertIn("[*] uc_system_ai_slack", out)
        self.assertIn("[ ] uc_system_ai_search", out)
        # Nothing written.
        self.assertEqual(self.claude.read_bytes(), before)
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.opencode.exists())

    def test_no_tty_without_flags_raises(self):
        from unittest import mock
        with mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("sys.stdout.isatty", return_value=False):
            with self.assertRaises(SystemExit):
                self._run()

    def test_interactive_menu_selection_writes_chosen(self):
        from unittest import mock
        # services sort to [search(0), slack(1)]; the menu returns index 1.
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch.object(mcp_menu, "choose", return_value=[1]):
            self._run()
        self.assertEqual(self._keys("opencode", self.opencode), {"uc_system_ai_slack"})

    def test_interactive_menu_cancel_makes_no_change(self):
        from unittest import mock
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch.object(mcp_menu, "choose", return_value=None):
            with self.assertRaises(SystemExit):
                self._run()
        self.assertFalse(self.opencode.exists())

    def test_interactive_fallback_to_numbered_prompt(self):
        from unittest import mock
        # No raw-mode terminal -> fall back to the numbered prompt, answered "slack".
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("sys.stdout.isatty", return_value=True), \
             mock.patch.object(mcp_menu, "choose", side_effect=mcp_menu.MenuUnavailable("x")), \
             mock.patch("builtins.input", return_value="slack"):
            self._run()
        self.assertEqual(self._keys("opencode", self.opencode), {"uc_system_ai_slack"})

    def test_idempotent_rerun_is_byte_identical(self):
        self._run(select=["slack"])
        first = {
            "claude": self.claude.read_bytes(),
            "codex": self.codex.read_bytes(),
            "opencode": self.opencode.read_bytes(),
        }
        self._run(select=["slack"])
        self.assertEqual(self.claude.read_bytes(), first["claude"])
        self.assertEqual(self.codex.read_bytes(), first["codex"])
        self.assertEqual(self.opencode.read_bytes(), first["opencode"])
        # The idempotent second run backs up nothing (it makes no change).
        self.assertEqual(list(self._tmp.glob("*.bak-*")), [])

    def test_dry_run_writes_nothing(self):
        rc = self._run(all=True, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.opencode.exists())


class MenuReducerTest(unittest.TestCase):
    """Pure key -> state transitions for the arrow-key menu (no terminal I/O)."""

    def test_down_and_up_wrap(self):
        cursor, _, _, _ = mcp_menu.apply_key(mcp_menu.DOWN, 0, set(), 3)
        self.assertEqual(cursor, 1)
        cursor, _, _, _ = mcp_menu.apply_key(mcp_menu.DOWN, 2, set(), 3)
        self.assertEqual(cursor, 0)  # wraps to top
        cursor, _, _, _ = mcp_menu.apply_key(mcp_menu.UP, 0, set(), 3)
        self.assertEqual(cursor, 2)  # wraps to bottom

    def test_toggle_adds_and_removes(self):
        _, sel, _, _ = mcp_menu.apply_key(mcp_menu.TOGGLE, 1, set(), 3)
        self.assertEqual(sel, {1})
        _, sel, _, _ = mcp_menu.apply_key(mcp_menu.TOGGLE, 1, {1}, 3)
        self.assertEqual(sel, set())

    def test_all_selects_then_clears(self):
        _, sel, _, _ = mcp_menu.apply_key(mcp_menu.ALL, 0, set(), 3)
        self.assertEqual(sel, {0, 1, 2})
        _, sel, _, _ = mcp_menu.apply_key(mcp_menu.ALL, 0, {0, 1, 2}, 3)
        self.assertEqual(sel, set())

    def test_confirm_and_cancel(self):
        _, _, done, cancelled = mcp_menu.apply_key(mcp_menu.CONFIRM, 0, {1}, 3)
        self.assertTrue(done)
        self.assertFalse(cancelled)
        _, _, done, cancelled = mcp_menu.apply_key(mcp_menu.CANCEL, 0, {1}, 3)
        self.assertTrue(done)
        self.assertTrue(cancelled)

    def test_unknown_key_is_noop(self):
        state = mcp_menu.apply_key(None, 1, {0}, 3)
        self.assertEqual(state, (1, {0}, False, False))

    def test_empty_list_only_confirm_or_cancel_end(self):
        _, _, done, cancelled = mcp_menu.apply_key(mcp_menu.CONFIRM, 0, set(), 0)
        self.assertTrue(done)
        self.assertFalse(cancelled)
        _, _, done, cancelled = mcp_menu.apply_key(mcp_menu.CANCEL, 0, set(), 0)
        self.assertTrue(done)
        self.assertTrue(cancelled)
        cursor, _, done, _ = mcp_menu.apply_key(mcp_menu.DOWN, 0, set(), 0)
        self.assertEqual(cursor, 0)
        self.assertFalse(done)


if __name__ == "__main__":
    unittest.main()
