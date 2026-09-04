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
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents import REGISTRY
from gateway import DEFAULT_INFRA_DIR, build_context

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "generated"


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
