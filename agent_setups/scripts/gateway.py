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
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
class Telemetry:
    """The deployed OTEL ingestion stack (from the `telemetry` Terraform output)."""

    schema_full_name: str
    tables: dict[str, str]  # signal ("metrics"/"logs"/"traces") -> fq table name
    secret_full_name: str  # UC secret the otelHeadersHelper reads (catalog.schema.secret)
    service_principal_application_id: str  # SP client id (not sensitive)
    # Hook-event ingestion facts: {"table": fq, "endpoint": zerobus url}. None when
    # hook_events_enabled = false; "endpoint" may be "" until zerobus_endpoint is set.
    hook_events: dict[str, str] | None = None


@dataclass(frozen=True)
class GatewayContext:
    host: str  # https://<workspace-host> (no trailing slash)
    catalog_name: str
    provider_schemas: dict[str, str]  # provider -> catalog.schema
    endpoints: list[Endpoint]
    telemetry: Telemetry | None = None  # None when telemetry_enabled = false

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


def discover_api_types(
    full_names: list[str],
    profile: str,
    databricks_bin: str = "databricks",
    max_workers: int = 8,
) -> dict[str, list[str]]:
    """Map each model-service full name -> its `supported_api_types`.

    `supported_api_types` is computed by the platform per underlying model and is
    only returned on a single-object GET (not in LIST), so we fan out one GET per
    service. Raises if *every* lookup fails (usually an auth/profile problem).
    """

    def one(full_name: str) -> tuple[str, list[str], bool]:
        try:
            proc = subprocess.run(
                [databricks_bin, "api", "get",
                 f"/api/2.1/unity-catalog/model-services/{full_name}", "--profile", profile],
                check=True, capture_output=True, text=True,
            )
            return full_name, (json.loads(proc.stdout).get("supported_api_types") or []), True
        except Exception:
            return full_name, [], False

    results: dict[str, list[str]] = {}
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for full_name, types, success in pool.map(one, full_names):
            results[full_name] = types
            ok += int(success)

    if full_names and ok == 0:
        raise SystemExit(
            f"Could not read supported_api_types for any model service (profile '{profile}'). "
            "Check auth/profile, or pass --skip-api-discovery."
        )
    return results


MCP_NAME_PREFIX = "mcp-services/"


def filter_mcp_services(
    services: list[dict],
    catalogs: list[str],
    schemas: list[str] | None = None,
) -> list[str]:
    """Filter a raw `mcp_services` list to the full names in scope.

    The API returns each service `name` as `mcp-services/<catalog>.<schema>.<name>`.
    The server-side catalog filter is ignored, so we filter client-side. We keep an
    object when it is an MCP_SERVICE, its catalog is in `catalogs`, and (when
    `schemas` is set) its schema is in `schemas`. We return the sorted three-level
    full names (`<catalog>.<schema>.<name>`) with the `mcp-services/` prefix removed.
    """
    catalog_set = set(catalogs)
    schema_set = set(schemas) if schemas else None
    full_names: list[str] = []
    for svc in services:
        if svc.get("securable_type") != "MCP_SERVICE":
            continue
        raw = svc.get("name", "")
        full_name = raw[len(MCP_NAME_PREFIX):] if raw.startswith(MCP_NAME_PREFIX) else raw
        parts = full_name.split(".")
        if len(parts) != 3:
            continue
        catalog, schema, _leaf = parts
        if catalog not in catalog_set:
            continue
        if schema_set is not None and schema not in schema_set:
            continue
        full_names.append(full_name)
    return sorted(set(full_names))


# A command-runner maps an API endpoint path to the CLI's stdout. It is the seam
# that makes pagination testable without a subprocess or the network.
CommandRunner = Callable[[str], str]

# Guard against a misbehaving server that returns a stable/looping page token.
MCP_PAGE_LIMIT = 1000


def _cli_command_runner(databricks_bin: str, profile: str) -> CommandRunner:
    """Return a runner that fetches an endpoint via `databricks api get`."""

    def run(endpoint: str) -> str:
        try:
            proc = subprocess.run(
                [databricks_bin, "api", "get", endpoint, "--profile", profile],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            raise SystemExit(f"{databricks_bin} not found on PATH; install the Databricks CLI") from exc
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                f"`{databricks_bin} api get {endpoint}` failed (profile '{profile}').\n{exc.stderr.strip()}"
            ) from exc
        return proc.stdout

    return run


def discover_mcp_services(
    catalogs: list[str],
    profile: str,
    schemas: list[str] | None = None,
    databricks_bin: str = "databricks",
    runner: CommandRunner | None = None,
) -> list[str]:
    """Discover the AI Gateway MCP services in scope, as three-level full names.

    Lists `/api/2.1/unity-catalog/mcp-services` through the `runner` (by default the
    databricks CLI), follows `next_page_token`, then filters client-side by catalog
    and schema (the server-side catalog filter is ignored). The `runner` seam lets
    tests inject canned pages. Raises SystemExit on a CLI or JSON parse failure, or if
    the server keeps returning page tokens past a sane cap.
    """
    run = runner or _cli_command_runner(databricks_bin, profile)
    services: list[dict] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MCP_PAGE_LIMIT):
        endpoint = "/api/2.1/unity-catalog/mcp-services"
        if page_token:
            endpoint = f"{endpoint}?page_token={urllib.parse.quote(page_token, safe='')}"
        stdout = run(endpoint)
        try:
            page = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"Could not parse the mcp-services response as JSON ({exc})."
            ) from exc
        services.extend(page.get("mcp_services") or [])
        page_token = page.get("next_page_token") or None
        if not page_token:
            break
        if page_token in seen_tokens:
            raise SystemExit(
                "mcp-services pagination returned a repeated page token; aborting to avoid a loop."
            )
        seen_tokens.add(page_token)
    else:
        raise SystemExit(
            f"mcp-services pagination exceeded {MCP_PAGE_LIMIT} pages; aborting to avoid a loop."
        )
    return filter_mcp_services(services, catalogs, schemas)


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

    tel_raw = value("telemetry")
    telemetry = (
        Telemetry(
            schema_full_name=tel_raw.get("schema_full_name", ""),
            tables=tel_raw.get("tables", {}) or {},
            secret_full_name=tel_raw.get("secret_full_name", ""),
            service_principal_application_id=tel_raw.get("service_principal_application_id", ""),
            hook_events=(tel_raw.get("hook_events") if isinstance(tel_raw.get("hook_events"), dict) else None),
        )
        if isinstance(tel_raw, dict)
        else None
    )

    return GatewayContext(
        host=resolve_host(profile, explicit_host),
        catalog_name=value("catalog_name", ""),
        provider_schemas=value("provider_schemas", {}) or {},
        endpoints=endpoints,
        telemetry=telemetry,
    )
