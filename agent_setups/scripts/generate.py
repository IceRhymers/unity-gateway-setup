#!/usr/bin/env python3
"""Entrypoint for generating coding-agent configs from the Unity AI Gateway.

Reads the Terraform outputs of terraform/infra (the deployed model services) and
emits opinionated config files for a coding agent. First supported agent:
Claude Code (managed-settings.json for MDM deployment).

Examples:
  # Generate Claude Code managed settings from the applied Terraform state.
  ./generate.py claude-code --profile fevm-west

  # Print to stdout instead of writing files.
  ./generate.py claude-code --profile fevm-west --stdout

  # Use a saved `terraform output -json` (no terraform invocation).
  ./generate.py claude-code --tf-output-json /tmp/tf.json --host https://myws.cloud.databricks.com

  # Discover AI Gateway MCP services and pick which to install (interactive menu).
  ./generate.py mcp --profile fevm-west --catalog my_catalog --schema tools

  # List the discovered services (marking installed ones) and write nothing.
  ./generate.py mcp --catalog my_catalog --list

  # Non-interactive: make the named services the complete set for every harness.
  ./generate.py mcp --catalog my_catalog --select slack,search

  # Install every discovered service; preview the merge as a diff and write nothing.
  ./generate.py mcp --catalog my_catalog --all --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents import REGISTRY
from gateway import DEFAULT_INFRA_DIR, build_context, discover_mcp_services, resolve_host

import mcp_install
import mcp_menu

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "generated"


def _split_csv(values: list[str] | None) -> list[str]:
    """Flatten a repeatable/comma-separated CLI option into a de-duplicated list."""
    out: list[str] = []
    for value in values or []:
        for item in value.split(","):
            item = item.strip()
            if item and item not in out:
                out.append(item)
    return out


def _add_mcp_parser(sub: argparse._SubParsersAction) -> None:
    """Register the `mcp` subcommand.

    Unlike the generate-and-write agents, `mcp` discovers the AI Gateway MCP
    services once and merges stdio entries into each harness's USER config in place.
    It does not read Terraform state. It needs only the workspace host and profile.
    """
    ap = sub.add_parser(
        "mcp",
        help="Discover AI Gateway MCP services and install stdio entries into harness user configs.",
        description=(
            "Discover the Databricks AI Gateway MCP services in a catalog/schema and "
            "install stdio MCP entries (via uc-mcp-proxy) into the USER config of the "
            "coding-agent harnesses, with an idempotent in-place merge."
        ),
    )
    ap.add_argument("--profile", default="fevm-west", help="Databricks CLI profile (default: fevm-west).")
    ap.add_argument("--host", default=None, help="Workspace URL override (else DATABRICKS_HOST or the profile's host).")
    ap.add_argument("--catalog", action="append", required=True,
                    help="Catalog to include (repeatable or comma-separated). Required.")
    ap.add_argument("--schema", action="append", default=None,
                    help="Schema to include (repeatable or comma-separated). Omit for all schemas in the catalog(s).")
    ap.add_argument("--harness", action="append", default=None,
                    help=f"Harnesses to install (comma list). Default: all of {', '.join(mcp_install.HARNESSES)}.")
    ap.add_argument("--server-prefix", default=mcp_install.DEFAULT_SERVER_PREFIX,
                    help=(
                        "Server-key prefix that defines the ownership namespace of this merge "
                        f"(default: {mcp_install.DEFAULT_SERVER_PREFIX}). The merge upserts and "
                        "removes ONLY keys with this prefix. It must not be empty."))
    ap.add_argument("--allow-empty", action="store_true",
                    help="Allow an empty discovery to remove all prefixed entries (default: refuse and keep them).")
    # Selection. With none of these, the run is interactive (needs a terminal).
    ap.add_argument("--list", action="store_true",
                    help="Print the discovered services (marking installed ones) and exit without writing.")
    ap.add_argument("--all", action="store_true", help="Select every discovered service.")
    ap.add_argument("--select", action="append", default=None,
                    help=("Select services by name (repeatable or comma-separated). A token matches the "
                          "leaf name, <schema>.<name>, the full <catalog>.<schema>.<name>, or the server key. "
                          "Selection is declarative: the chosen set becomes the complete config, and any "
                          "installed prefixed server you do not select is removed."))
    ap.add_argument("--dry-run", action="store_true", help="Print a diff and write nothing.")
    ap.add_argument("--databricks-bin", default="databricks", help="Databricks CLI binary (default: databricks).")
    # Per-harness config-path overrides, so tests and cautious runs never touch real files.
    ap.add_argument("--claude-config", type=Path, default=None, help="Override the Claude Code config path.")
    ap.add_argument("--codex-config", type=Path, default=None, help="Override the Codex config.toml path.")
    ap.add_argument("--opencode-config", type=Path, default=None, help="Override the opencode.json path.")


def _harness_paths(args: argparse.Namespace, harnesses: list[str]) -> dict[str, Path]:
    """Resolve the config path for each requested harness (override else default)."""
    overrides = {
        "claude-code": args.claude_config,
        "codex": args.codex_config,
        "opencode": args.opencode_config,
    }
    defaults = {
        "claude-code": mcp_install.default_claude_path,
        "codex": mcp_install.default_codex_path,
        "opencode": mcp_install.default_opencode_path,
    }
    return {h: (overrides[h] or defaults[h]()) for h in harnesses}


def _print_menu(services: list[mcp_install.McpService], preselected: set[str]) -> None:
    """Print a numbered menu of discovered services, marking installed ones with `*`."""
    for i, svc in enumerate(services, start=1):
        mark = "*" if svc.full_name in preselected else " "
        print(f"  {i:>2}. [{mark}] {svc.server_key}  ({svc.full_name})")
    print("  (* = installed in at least one target harness)")


def run_mcp(args: argparse.Namespace) -> int:
    """Discover once, select services, then merge into each requested harness's config."""
    prefix = args.server_prefix
    if not prefix or not prefix.strip():
        raise SystemExit(
            "--server-prefix must not be empty or whitespace. It defines the ownership "
            "namespace of the merge (only keys with this prefix are upserted/removed)."
        )

    host = resolve_host(args.profile, args.host)
    catalogs = _split_csv(args.catalog)
    schemas = _split_csv(args.schema) or None
    harnesses = _split_csv(args.harness) or list(mcp_install.HARNESSES)
    for harness in harnesses:
        if harness not in mcp_install.HARNESSES:
            raise SystemExit(f"unknown harness '{harness}'. Choose from: {', '.join(mcp_install.HARNESSES)}")

    full_names = discover_mcp_services(
        catalogs=catalogs,
        profile=args.profile,
        schemas=schemas,
        databricks_bin=args.databricks_bin,
    )
    services = mcp_install.build_services(full_names, host, args.profile, prefix)
    discovered = [s.full_name for s in services]

    scope = ", ".join(catalogs) + ("" if schemas is None else f" [{', '.join(schemas)}]")
    print(f"Discovered {len(services)} MCP service(s) in {scope}.")

    # Which discovered services are already installed (as prefixed keys) in any target.
    paths = _harness_paths(args, harnesses)
    installed_keys: set[str] = set()
    for harness in harnesses:
        installed_keys |= mcp_install.installed_prefixed_keys(harness, paths[harness], prefix)
    preselected = {s.full_name for s in services if s.server_key in installed_keys}

    # --list: show the menu and exit without writing.
    if args.list:
        _print_menu(services, preselected)
        return 0

    if not services and not args.allow_empty:
        raise SystemExit(
            f"Discovery found no MCP services in {scope}. Refusing to modify configs "
            "(this would remove any installed entries). Check --catalog/--schema, or pass "
            "--allow-empty to remove all prefixed entries on purpose."
        )

    # Resolve the selected set of discovered full names.
    if args.all:
        selected = set(discovered)
    elif args.select:
        selected = set()
        for token in _split_csv(args.select):
            matches = mcp_install.match_token(token, discovered, prefix)
            if not matches:
                raise SystemExit(f"--select: '{token}' matched no discovered MCP service in {scope}.")
            selected |= matches
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SystemExit(
                "no interactive terminal; pass --select NAMES or --all, or run in a terminal."
            )
        labels = [f"{s.server_key}  ({s.full_name})" for s in services]
        preselected_idx = [i for i, s in enumerate(services) if s.full_name in preselected]
        header = (f"Select MCP services for {', '.join(harnesses)} "
                  "(the chosen set becomes the complete config):")
        try:
            chosen = mcp_menu.choose(labels, preselected_idx, header=header)
        except mcp_menu.MenuUnavailable:
            # No raw-mode terminal (e.g. a dumb TTY): fall back to a numbered prompt.
            _print_menu(services, preselected)
            response = input(
                "Select services (comma-separated numbers or names; 'all', 'none'; "
                "empty = keep the marked set): "
            )
            selected = mcp_install.parse_selection(discovered, preselected, response, prefix)
        else:
            if chosen is None:
                raise SystemExit("Selection cancelled; no changes made.")
            selected = {services[i].full_name for i in chosen}

    selected_services = [s for s in services if s.full_name in selected]
    print(f"Selected {len(selected_services)} service(s) to install.")

    for harness in harnesses:
        result = mcp_install.install_harness(
            harness=harness,
            services=selected_services,
            profile=args.profile,
            path=paths[harness],
            prefix=prefix,
            dry_run=args.dry_run,
            # The zero-discovery guard above already protects against an empty discovery
            # wiping entries. Past it, an explicit empty selection is a real choice, so we
            # let the merge apply the resulting removals.
            allow_empty=True,
        )
        if args.dry_run:
            print("\n".join(result.diff_lines()))
        else:
            print(result.summary())
            if result.backup:
                print(f"  backed up -> {result.backup}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description="Generate coding-agent configs from the Databricks Unity AI Gateway Terraform outputs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="agent", required=True, metavar="AGENT")

    for name, generator_cls in REGISTRY.items():
        ap = sub.add_parser(name, help=generator_cls.help, description=generator_cls.help)
        # Shared source/auth flags.
        ap.add_argument("--profile", default="fevm-west", help="Databricks CLI profile (default: fevm-west).")
        ap.add_argument("--host", default=None, help="Workspace URL override (else DATABRICKS_HOST or the profile's host).")
        ap.add_argument("--infra-dir", type=Path, default=DEFAULT_INFRA_DIR, help="Path to terraform/infra.")
        ap.add_argument("--tf-output-json", type=Path, default=None, help="Path to a saved `terraform output -json` (skips running terraform).")
        # Output flags.
        ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Directory to write generated files into.")
        ap.add_argument("--stdout", action="store_true", help="Print generated file(s) to stdout instead of writing.")
        # Agent-specific flags.
        generator_cls.add_arguments(ap)

    _add_mcp_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # The `mcp` subcommand installs into user configs; it does not read Terraform state.
    if args.agent == "mcp":
        return run_mcp(args)

    generator = REGISTRY[args.agent]()

    ctx = build_context(
        profile=args.profile,
        infra_dir=args.infra_dir,
        tf_output_json=args.tf_output_json,
        explicit_host=args.host,
    )

    files = generator.generate(ctx, args)

    if args.stdout:
        for rel, content in files.items():
            if len(files) > 1:
                print(f"# ===== {rel} =====")
            sys.stdout.write(content)
        return 0

    for rel, content in files.items():
        dest = args.out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        print(f"wrote {dest}")

    notes = generator.install_notes(args)
    if notes:
        print("\n" + notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
