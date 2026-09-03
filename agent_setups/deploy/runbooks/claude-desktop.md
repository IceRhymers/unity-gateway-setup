# Claude Desktop runbook — Unity AI Gateway third-party inference (MDM)

Claude Desktop reads an operator-imported configuration, not a file that MDM places on disk. So the deployment differs from Claude Code, Codex, and opencode. The operator imports the generated JSON into the app, tests the connection, and then exports the OS-native MDM profile from the app.

This generator produces two things per OS:

1. The importable `claude-setup.json` (schema version 2, the nested-object form).
2. The credential helper scripts the JSON references by absolute path.

This generator does not produce the `.mobileconfig` or `.reg` MDM artifacts. The Claude Desktop app exports those after you import the JSON.

---

## Three-phase deployment model

| Phase | Who | What | MDM-pushable? |
|---|---|---|---|
| **A — Helper placement** | IT admin | Place the helper scripts at the absolute path the JSON references | **Yes** |
| **B — Import and export** | IT admin (once) | Import the JSON in the app, test, then export the MDM profile | **No** — the app UI does this once |
| **C — User auth** | Each developer | `databricks auth login --host <host> --profile <profile>` | **No** — browser OAuth (U2M) |

Phase A places the scripts. Phase B produces the MDM profile you distribute to the fleet. Phase C binds each developer's Databricks identity. No phase is optional.

---

## Why a credential helper

Claude Desktop starts under `launchd` (macOS) with a minimal `PATH`. The app cannot find the Databricks CLI on `$PATH`. The helper resolves the CLI from an absolute-path candidate list instead. The helper prints only the OAuth access token to standard output. The app caches the token for `credential.ttlSec` seconds and re-runs the helper when the token expires.

---

## Step 1 — Generate the bundles

Run the generator against the applied Terraform outputs.

```sh
make agent-claude-desktop PROFILE=<profile>
# or, directly:
python3 agent_setups/scripts/generate.py claude-desktop --profile <profile> --out-dir agent_setups/generated
```

The generator writes one bundle per OS to `agent_setups/generated/claude-desktop/<platform>/`. The default platforms are macOS and Windows. Add `--platforms macos,windows,linux` to include Linux.

Each macOS or Linux bundle contains:

- `claude-setup.json`
- `databricks-token.sh`
- `otel-headers-helper.sh` (only when telemetry is wired)

Each Windows bundle contains:

- `claude-setup.json`
- `databricks-token.ps1`
- `databricks-token.cmd` (the shim the JSON points at)
- `otel-headers-helper.ps1` and `otel-headers-helper.cmd` (only when telemetry is wired)

---

## Step 2 — Place the helper scripts (Phase A)

The `credential.command` value in `claude-setup.json` is an absolute path. The helper script must exist at that exact path. The default paths are:

| OS | Helper directory | Command target |
|---|---|---|
| macOS | `/Library/Application Support/ClaudeDesktop` | `databricks-token.sh` |
| Windows | `C:\ProgramData\ClaudeDesktop` | `databricks-token.cmd` |
| Linux | `/etc/claude-desktop` | `databricks-token.sh` |

To change a path, pass `--install-dir-macos`, `--install-dir-windows`, or `--install-dir-linux` at generation time. The JSON then references the path you set.

### macOS and Linux

Run `install.sh` as root, or with `--target-root` for staging.

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos --source agent_setups/generated
```

`install.sh` places `databricks-token.sh` (and `otel-headers-helper.sh` when present) at the helper directory. It sets each script executable (mode 755). It does not place `claude-setup.json`.

### Windows

`install.sh` is a POSIX script. It does not run on Windows. Place the Windows helpers with Intune or a machine-wide script instead. Copy `databricks-token.cmd`, `databricks-token.ps1`, and the OTEL pair to `C:\ProgramData\ClaudeDesktop`. Keep the `.ps1` beside the `.cmd`. The `.cmd` shim runs the `.ps1` from its own directory.

> **Windows scripts are not tested yet.** The PowerShell helpers are theoretical. Test them on a Windows machine before a production rollout.

---

## Step 3 — Import and export (Phase B)

1. Start Claude Desktop.
2. Open Help → Troubleshooting → Enable Developer Mode.
3. Open Developer → Configure third-party inference.
4. Import `claude-setup.json` for this OS.
5. Test the connection.
6. Export the MDM profile from the app. The app produces a `.mobileconfig` on macOS or a `.reg` on Windows.
7. Distribute the exported profile to the fleet with your MDM (Jamf, Intune, or similar).

---

## Step 4 — User auth (Phase C)

Each developer runs this command once. The command opens a browser for SSO. MDM cannot push this step.

```sh
databricks auth login --host <workspace-url> --profile <profile>
```

---

## Telemetry (OpenTelemetry)

The generator wires telemetry when the Terraform `telemetry` output has a traces table. Use `--telemetry on` to require it, or `--telemetry off` to skip it.

Claude Desktop carries a single `otlp.headers` set. So it routes traces to one Unity Catalog table only. It cannot split metrics, logs, and traces to different tables the way Claude Code does. The generator wires **traces** to the traces table.

The `otel-headers-helper` script mints the dedicated telemetry service-principal token. The token is down-scoped to the traces table. The developer must hold `READ_SECRET` on the telemetry Unity Catalog secret. On macOS and Linux the helper is a bash script. On Windows the helper is a PowerShell script behind a `.cmd` shim.

> **Verify the `otlp` key names.** The importable-JSON `otlp` shape can change between Claude Desktop releases. Check the key names against the live Claude Desktop configuration reference before a production rollout.

---

## Prerequisites

| Tool | Criticality |
|---|---|
| `databricks` | Always critical — the credential helper fetches the token with it |
| `python3` | Critical when telemetry is on — the macOS/Linux OTEL helper uses it |

The credential helper `databricks-token.sh` uses `sed` only. It does not need `python3`.

---

## Uninstall

`install.sh` removes the placed helper scripts on macOS and Linux.

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos --uninstall
```

The command removes only the files the version marker records. It preserves other files in the directory. It does not remove `claude-setup.json`, because `install.sh` never placed it. Remove the imported configuration inside the app, and remove the exported MDM profile through your MDM.

---

## Verify

1. Open Claude Desktop.
2. Confirm the app routes to the gateway base URL.
3. Send a test message to confirm inference works.
4. When telemetry is on, confirm trace rows land in the traces table.
