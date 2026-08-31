# Jamf runbook — Unity AI Gateway agent configs (macOS fleet)

## Two-phase deployment model

Every machine deployment has two phases with a hard boundary between them:

| Phase | Who | What | Can it be MDM-pushed? |
|---|---|---|---|
| **A — Config placement** | IT / Jamf admin | Unpack tarball, run `install.sh` as root | **Yes** — this runbook |
| **B — User auth** | Each developer | `databricks auth login --host <host> --profile <profile>` | **No** — browser OAuth (U2M). Must be interactive. |

Phase A places the managed-settings files. Phase B binds each developer's Databricks identity. Neither phase is optional.

---

## Prerequisites

`install.sh` checks prereqs and reports them. It does **not** install anything. IT is
responsible for these as part of the macOS MDM baseline:

| Tool | Criticality |
|---|---|
| `databricks` | Always critical — auth helpers and hook emitter require it |
| `python3` | Always critical — both auth helpers shell out to `python3` on every token mint |
| `jq` | Critical only when hook-event telemetry is enabled (emitter uses it) |
| `curl` | Critical only when hook-event telemetry is enabled (emitter uses it) |

If a critical prereq is absent, `install.sh` exits 3. Jamf then marks the policy failed.
Check that these tools are on PATH for all session types (login shell, non-interactive)
before you scope the policy to machines.

> **Exception:** a `DATABRICKS_BEARER`-only deployment can omit `databricks` and
> `python3`. In this deployment, every developer sets `DATABRICKS_BEARER` in their
> environment. The CLI then never needs a token refresh. This deployment is unusual and
> not the default.

---

## Step 1 — Build the package

On an admin workstation with the repo checked out:

```bash
make deploy-package
```

This produces `dist/unity-gateway-agents-<version>-macos.tar.gz`. The tarball is
self-contained: bundle files, `install.sh`, runbooks, and a `VERSION` file. The target
machine needs no network access.

Upload `dist/unity-gateway-agents-<version>-macos.tar.gz` to your Jamf distribution
point (or a Jamf Pro package).

---

## Step 2 — Create a Script policy

In Jamf Pro, create a **Script** (Computers → Management → Scripts → New). Paste the
following as the script body:

```sh
#!/bin/sh
set -eu

PAYLOAD="/tmp/unity-gateway-agents.tar.gz"
WORK_DIR="/tmp/unity-gateway-agents-install"

# --- unpack ---
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# Jamf copies the package to a temp location; adapt the path to your distribution
# method. If you attach the tarball as a package payload, reference $4/$5 instead.
cp "$PAYLOAD" "$WORK_DIR/"
tar -xzf "$WORK_DIR/unity-gateway-agents.tar.gz" -C "$WORK_DIR" --strip-components=1

# --- run installer as root (already root in a Jamf policy) ---
cd "$WORK_DIR"
./install.sh
EXIT_CODE=$?

# --- clean up ---
rm -rf "$WORK_DIR"

if [ "$EXIT_CODE" -ne 0 ]; then
  echo "install.sh exited $EXIT_CODE — policy failed" >&2
  exit "$EXIT_CODE"
fi

echo "Unity AI Gateway agent configs installed (Phase A complete)."
echo "Each developer must complete Phase B — see the Self Service item."
```

> **Note on `--target-root`:** the script above uses real system paths (no
> `--target-root`). This is correct for a fleet push. `--target-root` is for
> unprivileged staging and unit tests only.

Attach the script to a **Policy** scoped to your target machines (see Step 3).
Set the **Execution Frequency** to "Once per computer" for initial rollout. Change it to
"Once per computer per user" or "Ongoing" for re-rollout when the config version
changes.

---

## Unsigned-script caveat

Jamf executes this script as **root without notarization or code-signing checks**.
Treat it accordingly:

- Store the tarball at rest on your Jamf distribution point under access controls.
- Check the SHA-256 of the tarball before you upload it (CI produces a checksum alongside
  the tarball in `dist/`).
- `install.sh` itself is POSIX `sh`. Review it before you deploy it to a new macOS
  major version.
- IT owns prereqs (`databricks`, `python3`, `jq`, `curl`) as a managed baseline.
  `install.sh` only checks and reports them (exit 3 on critical missing dep).

---

## Step 3 — Scoping

Suggested scope for the Phase-A policy:

- **Targets:** Smart Group based on macOS version + `databricks` CLI managed
  (check the MDM baseline is present before you target machines).
- **Exclusions:** Machines already running the target version (check
  `/Library/Application Support/ClaudeCode/.unity-gateway-version` if you want to
  be explicit, or rely on `install.sh`'s idempotent re-copy behaviour).
- **Trigger:** Check-in + Enrollment Complete, or a manual trigger for initial rollout.

---

## Step 4 — Phase B: Self Service item (per-user, one time)

Create a **Self Service** item (or send internal comms) that tells each developer to
run the following command once, interactively, in their terminal:

```
Config placement is complete. To finish connecting your tools to the
AI Gateway, run this command ONCE in your terminal:

    databricks auth login --host <host> --profile fevm-west

Your browser will open for Single Sign-On. After login, verify with:
  - Claude Code:  type /status in a conversation
  - Codex:        run  codex --strict-config doctor

You only need to do this once per machine.
```

Replace `<host>` with your Databricks workspace URL (e.g.
`https://myworkspace.cloud.databricks.com`).

You **cannot automate** this step. It requires interactive browser OAuth (U2M).
