"""Install Databricks AI Gateway MCP services into coding-agent USER configs.

This module turns a list of discovered MCP-service full names into stdio MCP
entries and merges them, in place, into the existing user config file of three
harnesses: Claude Code, Codex, and opencode. Each entry runs the stdio bridge
`uvx uc-mcp-proxy --url <gateway-url> --profile <profile>`.

The merge is idempotent. It touches ONLY keys with the server prefix (default
`uc_`): it upserts the currently discovered services, removes stale prefixed
entries, and leaves every other entry untouched. A second run makes no change.

The discovery (network) and the merge (file I/O) are separable. `build_services`
turns injected full names into `McpService` objects, and each `*_entries` builder
turns those into the per-harness data. The merge functions accept explicit paths,
so the logic is unit-testable without network or real user files.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HARNESSES = ("claude-code", "codex", "opencode")
DEFAULT_SERVER_PREFIX = "uc_"

_PROXY = "uc-mcp-proxy"


@dataclass(frozen=True)
class McpService:
    """One AI Gateway MCP service, ready to install as a stdio MCP entry."""

    full_name: str  # catalog.schema.name
    catalog: str
    schema: str
    name: str
    server_key: str  # <prefix><schema>_<name>, sanitized
    gateway_url: str  # https://<host>/ai-gateway/mcp-services/<full_name>


def sanitize_server_key(catalog: str, schema: str, name: str, prefix: str = DEFAULT_SERVER_PREFIX) -> str:
    """Build the server key `<prefix><catalog>_<schema>_<name>`, non-word chars -> `_`.

    The catalog is part of the key, so two services with the same schema.name in
    different catalogs (e.g. `cat_a.tools.search` and `cat_b.tools.search`) get
    distinct keys and never collide.
    """
    base = re.sub(r"[^A-Za-z0-9_]", "_", f"{catalog}_{schema}_{name}")
    return f"{prefix}{base}"


def build_services(
    full_names: list[str],
    host: str,
    profile: str,
    prefix: str = DEFAULT_SERVER_PREFIX,
) -> list[McpService]:
    """Turn three-level full names into `McpService` objects (sorted by key).

    Raises SystemExit if two services still map to the same server key (a defensive
    guard against silent overwrite).
    """
    host = host.rstrip("/")
    services: list[McpService] = []
    for full_name in full_names:
        parts = full_name.split(".")
        if len(parts) != 3:
            continue
        catalog, schema, name = parts
        services.append(
            McpService(
                full_name=full_name,
                catalog=catalog,
                schema=schema,
                name=name,
                server_key=sanitize_server_key(catalog, schema, name, prefix),
                gateway_url=f"{host}/ai-gateway/mcp-services/{full_name}",
            )
        )
    services.sort(key=lambda s: s.server_key)
    _assert_unique_keys(services)
    return services


def _assert_unique_keys(services: list[McpService]) -> None:
    """Raise SystemExit if any two services share a server key."""
    by_key: dict[str, list[str]] = {}
    for svc in services:
        by_key.setdefault(svc.server_key, []).append(svc.full_name)
    collisions = {k: v for k, v in by_key.items() if len(v) > 1}
    if collisions:
        detail = "; ".join(f"{k} <- {', '.join(sorted(v))}" for k, v in sorted(collisions.items()))
        raise SystemExit(f"MCP server-key collision after sanitization: {detail}")


def _proxy_args(svc: McpService, profile: str) -> list[str]:
    """The uc-mcp-proxy arguments (after the `uvx` launcher) for one service."""
    return [_PROXY, "--url", svc.gateway_url, "--profile", profile]


# ---- selection (pure, no TTY/input) ---------------------------------------------


def match_token(token: str, discovered: list[str], prefix: str = DEFAULT_SERVER_PREFIX) -> set[str]:
    """Return the discovered full names that one NAME token matches (case-insensitive).

    A token matches a discovered `<catalog>.<schema>.<name>` when it equals, ignoring
    case, the leaf name (`slack`), the `<schema>.<name>`, the full name, or the server
    key (`uc_system_ai_slack`). One token may match more than one service.
    """
    tok = token.strip().lower()
    if not tok:
        return set()
    matched: set[str] = set()
    for full_name in discovered:
        parts = full_name.split(".")
        if len(parts) != 3:
            continue
        catalog, schema, name = parts
        candidates = {
            full_name.lower(),
            f"{schema}.{name}".lower(),
            name.lower(),
            sanitize_server_key(catalog, schema, name, prefix).lower(),
        }
        if tok in candidates:
            matched.add(full_name)
    return matched


def parse_selection(
    discovered: list[str],
    preselected: set[str],
    response_text: str,
    prefix: str = DEFAULT_SERVER_PREFIX,
) -> set[str]:
    """Parse an interactive selection response into a set of discovered full names.

    The response is a comma-separated list of 1-based menu numbers and/or names. The
    words `all` and `none` are special. An empty response confirms `preselected`. A
    numeric token out of range, or a name token that matches nothing, raises SystemExit.
    """
    text = response_text.strip()
    if not text:
        return set(preselected)
    tokens = [raw.strip() for raw in text.split(",") if raw.strip()]
    # `all` and `none` are whole-answer words. Mixing either with other tokens is
    # ambiguous (e.g. `all,none`), so it is an error, not a silently-wrong guess.
    lows = {tok.lower() for tok in tokens}
    if lows & {"all", "none"}:
        if len(tokens) != 1:
            raise SystemExit("'all' and 'none' must be used on their own, not combined with other selections.")
        return set(discovered) if tokens[0].lower() == "all" else set()
    selected: set[str] = set()
    for tok in tokens:
        if tok.isdigit():
            idx = int(tok)
            if idx < 1 or idx > len(discovered):
                raise SystemExit(
                    f"selection number out of range: {idx} (choose 1..{len(discovered)})."
                )
            selected.add(discovered[idx - 1])
            continue
        matches = match_token(tok, discovered, prefix)
        if not matches:
            raise SystemExit(f"selection '{tok}' matched no discovered MCP service.")
        selected |= matches
    return selected


# ---- per-harness entry builders -------------------------------------------------


def claude_entry(svc: McpService, profile: str) -> dict:
    """Claude Code entry: command/args are split; type `stdio`."""
    return {"type": "stdio", "command": "uvx", "args": _proxy_args(svc, profile)}


def codex_entry(svc: McpService, profile: str) -> dict:
    """Codex entry: command/args are split (no `type` key in Codex tables)."""
    return {"command": "uvx", "args": _proxy_args(svc, profile)}


def opencode_entry(svc: McpService, profile: str) -> dict:
    """opencode entry: the whole command+args is one `command` array; type `local`."""
    return {
        "type": "local",
        "command": ["uvx", *_proxy_args(svc, profile)],
        "enabled": True,
    }


# ---- file helpers ---------------------------------------------------------------


def _backup(path: Path) -> Path:
    """Copy an existing file to `<path>.bak-<UTC timestamp>` and return the backup path.

    The timestamp carries microseconds plus a short random suffix, so two backups in
    the same second never collide.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = uuid.uuid4().hex[:6]
    backup = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
    backup.write_bytes(path.read_bytes())
    return backup


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically: temp file in the same dir, then os.replace.

    os.replace is atomic on POSIX, so a crash mid-write cannot truncate or corrupt the
    live config: either the old file or the fully written new file is present.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass
class MergeResult:
    """The outcome of one harness merge."""

    harness: str
    path: Path
    added: list[str]
    updated: list[str]
    removed: list[str]
    changed: bool
    backup: Path | None
    written: bool

    def summary(self) -> str:
        if not self.changed:
            return f"{self.harness}: no change ({self.path})"
        bits = []
        if self.added:
            bits.append(f"+{len(self.added)}")
        if self.updated:
            bits.append(f"~{len(self.updated)}")
        if self.removed:
            bits.append(f"-{len(self.removed)}")
        verb = "would update" if not self.written else "updated"
        line = f"{self.harness}: {verb} {self.path} ({', '.join(bits)})"
        if self.removed:
            line += "\n  removed: " + ", ".join(self.removed)
        return line

    def diff_lines(self) -> list[str]:
        lines = [f"# {self.harness}: {self.path}"]
        for key in self.added:
            lines.append(f"  + {key}")
        for key in self.updated:
            lines.append(f"  ~ {key}")
        for key in self.removed:
            lines.append(f"  - {key}")
        if not self.changed:
            lines.append("  (no change)")
        return lines


def _classify(existing_keys: set[str], desired: dict[str, object], prefix: str) -> tuple[list[str], list[str], list[str]]:
    """Return (added, updated, removed) prefixed keys, comparing desired vs existing.

    `updated` marks a prefixed key present in both. The caller decides whether the
    value actually changed. `removed` are stale prefixed keys no longer discovered.
    """
    desired_keys = set(desired)
    prefixed_existing = {k for k in existing_keys if k.startswith(prefix)}
    added = sorted(desired_keys - prefixed_existing)
    updated = sorted(desired_keys & prefixed_existing)
    removed = sorted(prefixed_existing - desired_keys)
    return added, updated, removed


# ---- JSON harnesses (Claude Code, opencode) -------------------------------------


def _merge_json(
    path: Path,
    top_key: str,
    desired: dict[str, dict],
    prefix: str,
    harness: str,
    dry_run: bool,
    allow_empty: bool,
) -> MergeResult:
    """Idempotent in-place merge of `desired` under `config[top_key]` in a JSON file.

    Preserves every non-prefixed entry and every other top-level key. Removes stale
    prefixed entries, upserts the current ones. Writes only when the bytes change.
    """
    original_bytes = path.read_bytes() if path.exists() else b""
    if original_bytes.strip():
        try:
            config = json.loads(original_bytes)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"{harness}: could not parse JSON at {path} ({exc}). "
                "Fix the file or restore it from a .bak-* backup."
            ) from exc
    else:
        config = {}
    if not isinstance(config, dict):
        raise SystemExit(f"{harness}: expected a JSON object at {path}")

    servers = config.get(top_key)
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise SystemExit(
            f"{harness}: expected key '{top_key}' in {path} to be an object, "
            f"got {type(servers).__name__}. Refusing to overwrite it."
        )
    existing_keys = set(servers)

    added, updated, removed = _classify(existing_keys, desired, prefix)
    if not desired and not allow_empty:
        removed = []  # empty discovery must not wipe existing entries
    changed_keys = [k for k in updated if servers.get(k) != desired[k]]

    merged = dict(servers)
    for key in removed:
        merged.pop(key, None)
    for key, value in desired.items():
        merged[key] = value

    new_config = dict(config)
    new_config[top_key] = merged
    new_bytes = (json.dumps(new_config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    changed = new_bytes != original_bytes
    written = False
    backup = None
    if changed and not dry_run:
        if path.exists():
            backup = _backup(path)
        _atomic_write(path, new_bytes)
        written = True

    return MergeResult(
        harness=harness,
        path=path,
        added=added,
        updated=changed_keys,
        removed=removed,
        changed=changed,
        backup=backup,
        written=written,
    )


# ---- Codex (TOML, round-trip preserving) ----------------------------------------


def _merge_codex(
    path: Path,
    desired: dict[str, dict],
    prefix: str,
    dry_run: bool,
    allow_empty: bool,
) -> MergeResult:
    """Idempotent in-place merge of `[mcp_servers.<key>]` tables in Codex config.toml.

    Uses tomlkit to round-trip the document, so the user's comments, formatting, and
    other tables survive. Removes stale prefixed servers, upserts the current ones,
    and leaves everything else untouched. Writes only when the bytes change.
    """
    try:
        import tomlkit
        from tomlkit.exceptions import TOMLKitError
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "The codex MCP merge needs tomlkit for a comment-preserving TOML round-trip.\n"
            "Install it: pip install -r agent_setups/scripts/requirements.txt"
        ) from exc

    original_bytes = path.read_bytes() if path.exists() else b""
    if original_bytes.strip():
        try:
            doc = tomlkit.parse(original_bytes.decode("utf-8"))
        except (TOMLKitError, UnicodeDecodeError) as exc:
            raise SystemExit(
                f"codex: could not parse TOML at {path} ({exc}). "
                "Fix the file or restore it from a .bak-* backup."
            ) from exc
    else:
        doc = tomlkit.document()

    servers = doc.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        doc["mcp_servers"] = servers
    elif not isinstance(servers, dict):
        raise SystemExit(
            f"codex: expected `mcp_servers` in {path} to be a table, "
            f"got {type(servers).__name__}. Refusing to overwrite it."
        )
    existing_keys = set(servers.keys())

    added, updated, removed = _classify(existing_keys, desired, prefix)
    if not desired and not allow_empty:
        removed = []  # empty discovery must not wipe existing entries

    def _same(key: str) -> bool:
        current = servers.get(key)
        if current is None:
            return False
        want = desired[key]
        return {k: list(v) if isinstance(v, list) else v for k, v in dict(current).items()} == want

    changed_keys = [k for k in updated if not _same(k)]

    for key in removed:
        del servers[key]
    for key, value in desired.items():
        tbl = tomlkit.table()
        for field, field_value in value.items():
            tbl[field] = field_value
        servers[key] = tbl

    new_bytes = tomlkit.dumps(doc).encode("utf-8")
    changed = new_bytes != original_bytes
    written = False
    backup = None
    if changed and not dry_run:
        if path.exists():
            backup = _backup(path)
        _atomic_write(path, new_bytes)
        written = True

    return MergeResult(
        harness="codex",
        path=path,
        added=added,
        updated=changed_keys,
        removed=removed,
        changed=changed,
        backup=backup,
        written=written,
    )


# ---- default paths --------------------------------------------------------------


def default_claude_path() -> Path:
    """`$CLAUDE_CONFIG_DIR/.claude.json` when set, else `~/.claude.json`."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def default_codex_path() -> Path:
    """`$CODEX_HOME/config.toml` when set, else `~/.codex/config.toml`."""
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "config.toml"


def default_opencode_path() -> Path:
    """`$XDG_CONFIG_HOME/opencode/opencode.json` when set, else `~/.config/...`."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "opencode" / "opencode.json"


# ---- read installed keys (for pre-marking the selection menu) -------------------


def installed_prefixed_keys(harness: str, path: Path, prefix: str = DEFAULT_SERVER_PREFIX) -> set[str]:
    """Return the server keys with `prefix` already present in a harness's config.

    Reads the current config file without changing it. A missing, empty, or
    unparseable file yields an empty set, so pre-marking never blocks the flow.
    """
    if not path.exists():
        return set()
    if harness in ("claude-code", "opencode"):
        top_key = "mcpServers" if harness == "claude-code" else "mcp"
        try:
            raw = path.read_bytes()
        except OSError:
            return set()
        if not raw.strip():
            return set()
        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        if not isinstance(config, dict):
            return set()
        servers = config.get(top_key)
        if not isinstance(servers, dict):
            return set()
        return {k for k in servers if k.startswith(prefix)}
    if harness == "codex":
        try:
            raw = path.read_bytes()
        except OSError:
            return set()
        if not raw.strip():
            return set()
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
            return set()
        try:
            doc = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            return set()
        servers = doc.get("mcp_servers")
        if not isinstance(servers, dict):
            return set()
        return {k for k in servers if k.startswith(prefix)}
    raise SystemExit(f"unknown harness: {harness}")


# ---- top-level installer --------------------------------------------------------


def install_harness(
    harness: str,
    services: list[McpService],
    profile: str,
    path: Path,
    prefix: str = DEFAULT_SERVER_PREFIX,
    dry_run: bool = False,
    allow_empty: bool = False,
) -> MergeResult:
    """Merge the discovered services into one harness's config file.

    When `services` is empty and `allow_empty` is False, existing prefixed entries
    are kept (an empty discovery must not silently wipe the installed servers).
    """
    _assert_unique_keys(services)
    if harness == "claude-code":
        desired = {s.server_key: claude_entry(s, profile) for s in services}
        return _merge_json(path, "mcpServers", desired, prefix, harness, dry_run, allow_empty)
    if harness == "opencode":
        desired = {s.server_key: opencode_entry(s, profile) for s in services}
        return _merge_json(path, "mcp", desired, prefix, harness, dry_run, allow_empty)
    if harness == "codex":
        desired = {s.server_key: codex_entry(s, profile) for s in services}
        return _merge_codex(path, desired, prefix, dry_run, allow_empty)
    raise SystemExit(f"unknown harness: {harness}")
