"""Unit tests for the opencode generator's plugin-reference behavior.

These tests build an in-memory GatewayContext and Namespace. They never read the
Terraform outputs and never touch a real user config. They assert how the
generated opencode.json references the auth plugin per deployment mode:

  - managed mode  : a relative reference (`./databricks-auth.ts`), and a macOS
                    .mobileconfig is emitted.
  - user mode     : an absolute reference ending in `/databricks-auth.ts`, and
                    NO .mobileconfig is emitted.
  - user mode + XDG: the absolute reference honors XDG_CONFIG_HOME at generation.

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
from gateway import Endpoint, GatewayContext  # noqa: E402

HOST = "https://myws.cloud.databricks.com"
PROFILE = "fevm-west"


def _context() -> GatewayContext:
    """A minimal context with one anthropic endpoint."""
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


def _args(**over) -> argparse.Namespace:
    base = dict(
        profile=PROFILE,
        auth_profile=None,
        default_model=None,
        user_config=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


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


if __name__ == "__main__":
    unittest.main()
