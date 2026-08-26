#!/usr/bin/env python3
"""Create one OTEL Delta table via the Databricks Statement Execution API.

Invoked by the telemetry module's null_resource (one call per signal). Reads a
DDL template, substitutes the fully-qualified table name for `{{TABLE}}`, and
runs it on a SQL warehouse through `databricks api post /api/2.0/sql/statements`.

The DDL is `CREATE TABLE IF NOT EXISTS`, so re-running is a no-op. Exits non-zero
(failing `terraform apply`) if the statement does not reach SUCCEEDED, so table
creation failures are visible rather than silent.

Standard library only; shells out to the Databricks CLI (already a repo
dependency) so it reuses the same profile/auth as the rest of the pipeline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# A warehouse may be COLD (serverless scale-to-zero); allow generous total wait.
POLL_INTERVAL_S = 3
MAX_WAIT_S = 300


def _api_post(path: str, body: dict, profile: str, databricks_bin: str) -> dict:
    """POST `body` to a Databricks REST path via the CLI, returning parsed JSON."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(body, fh)
        body_path = fh.name
    try:
        proc = subprocess.run(
            [databricks_bin, "api", "post", path, "--profile", profile, "--json", f"@{body_path}"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"`databricks api post {path}` failed:\n{exc.stderr.strip()}") from exc
    finally:
        Path(body_path).unlink(missing_ok=True)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _api_get(path: str, profile: str, databricks_bin: str) -> dict:
    try:
        proc = subprocess.run(
            [databricks_bin, "api", "get", path, "--profile", profile],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"`databricks api get {path}` failed:\n{exc.stderr.strip()}") from exc
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def run_statement(statement: str, warehouse_id: str, profile: str, databricks_bin: str) -> None:
    resp = _api_post(
        "/api/2.0/sql/statements",
        {
            "warehouse_id": warehouse_id,
            "statement": statement,
            "wait_timeout": "50s",
            "on_wait_timeout": "CONTINUE",
        },
        profile,
        databricks_bin,
    )
    statement_id = resp.get("statement_id")
    state = (resp.get("status") or {}).get("state")
    if not statement_id and state != "SUCCEEDED":
        raise SystemExit(f"Statement submission returned no statement_id (state={state!r}); response: {resp}")

    waited = 0
    while state in ("PENDING", "RUNNING") and waited < MAX_WAIT_S:
        time.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
        resp = _api_get(f"/api/2.0/sql/statements/{statement_id}", profile, databricks_bin)
        state = (resp.get("status") or {}).get("state")

    if state != "SUCCEEDED":
        err = (resp.get("status") or {}).get("error") or {}
        raise SystemExit(
            f"Statement {statement_id} ended in state {state!r} (waited {waited}s): "
            f"{err.get('message', 'no error message')}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ddl-file", type=Path, required=True, help="DDL template containing a {{TABLE}} placeholder.")
    ap.add_argument("--table", required=True, help="Fully-qualified table name (catalog.schema.table).")
    ap.add_argument("--warehouse-id", required=True, help="SQL warehouse to run the DDL on.")
    ap.add_argument("--profile", required=True, help="Databricks CLI profile.")
    ap.add_argument("--databricks-bin", default="databricks", help="Path to the databricks CLI.")
    args = ap.parse_args(argv)

    ddl = args.ddl_file.read_text().replace("{{TABLE}}", args.table)
    print(f"[create_otel_table] ensuring {args.table} on warehouse {args.warehouse_id}", file=sys.stderr)
    run_statement(ddl, args.warehouse_id, args.profile, args.databricks_bin)
    print(f"[create_otel_table] {args.table} ready", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
