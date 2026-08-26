"""Shared Unity AI Gateway context for agent-config generation.

Loads the Terraform outputs of terraform/infra (the deployed model services) and
resolves the workspace host, then exposes them to the per-agent generators.
Standard library only.
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Repo layout: this file is agent_setups/scripts/gateway.py
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INFRA_DIR = REPO_ROOT / "terraform" / "infra"


@dataclass(frozen=True)
class Endpoint:
    """One deployed model service (a gateway endpoint)."""

    key: str  # "<schema>/<endpoint>", e.g. "anthropic/claude-opus"
    schema: str  # provider schema, e.g. "anthropic"
    name: str  # endpoint leaf, e.g. "claude-opus" or "claude-opus-4-8"
    full_name: str  # three-level UC name catalog.schema.endpoint
    foundation_model: str  # models/system.ai.databricks-...
    inference_table: str | None

    @property
    def is_alias(self) -> bool:
        """Versionless alias (no -<digit> version suffix), e.g. claude-opus."""
        import re

        return re.search(r"-\d", self.name) is None


@dataclass(frozen=True)
class GatewayContext:
    host: str  # https://<workspace-host> (no trailing slash)
    catalog_name: str
    provider_schemas: dict[str, str]  # provider -> catalog.schema
    endpoints: list[Endpoint]

    def endpoints_for(self, schema: str) -> list[Endpoint]:
        return [e for e in self.endpoints if e.schema == schema]


def load_tf_outputs(infra_dir: Path, tf_output_json: Path | None = None) -> dict:
    """Return the parsed `terraform output -json` map ({name: {value, ...}})."""
    if tf_output_json is not None:
        return json.loads(tf_output_json.read_text())

    try:
        proc = subprocess.run(
            ["terraform", f"-chdir={infra_dir}", "output", "-json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("terraform not found on PATH; install it or pass --tf-output-json") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"`terraform output` failed in {infra_dir}. Has it been applied?\n{exc.stderr.strip()}"
        ) from exc
    return json.loads(proc.stdout)


def resolve_host(profile: str, explicit_host: str | None = None) -> str:
    """Resolve the workspace URL: --host > $DATABRICKS_HOST > ~/.databrickscfg profile."""
    host = explicit_host or os.environ.get("DATABRICKS_HOST")
    if not host:
        cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE", Path.home() / ".databrickscfg"))
        if cfg_path.exists():
            parser = configparser.ConfigParser()
            parser.read(cfg_path)
            if parser.has_option(profile, "host"):
                host = parser.get(profile, "host")
    if not host:
        raise SystemExit(
            f"Could not resolve workspace host for profile '{profile}'. "
            "Pass --host, set DATABRICKS_HOST, or add the profile to ~/.databrickscfg."
        )
    return host.rstrip("/")


def build_context(
    profile: str,
    infra_dir: Path = DEFAULT_INFRA_DIR,
    tf_output_json: Path | None = None,
    explicit_host: str | None = None,
) -> GatewayContext:
    outputs = load_tf_outputs(infra_dir, tf_output_json)

    def value(name: str, default=None):
        return outputs.get(name, {}).get("value", default)

    endpoints_raw = value("endpoints", {}) or {}
    endpoints: list[Endpoint] = []
    for key, meta in endpoints_raw.items():
        schema, _, name = key.partition("/")
        endpoints.append(
            Endpoint(
                key=key,
                schema=schema,
                name=name,
                full_name=meta.get("full_name", ""),
                foundation_model=meta.get("foundation_model", ""),
                inference_table=meta.get("inference_table"),
            )
        )
    endpoints.sort(key=lambda e: e.key)

    return GatewayContext(
        host=resolve_host(profile, explicit_host),
        catalog_name=value("catalog_name", ""),
        provider_schemas=value("provider_schemas", {}) or {},
        endpoints=endpoints,
    )
