# Claude Desktop runbook — Unity AI Gateway third-party inference (MDM)

Claude Desktop reads an operator-imported configuration, not a file that MDM places on disk. So the deployment differs from Claude Code and Codex. The operator imports the generated JSON into the app, tests the connection, and then exports the OS-native MDM profile from the app.

## What MDM owns, and what `ug` owns

`ug` is the developer surface. A workspace admin authors a managed config with `ug setup` and publishes it with `ug publish`. That config decides the agents, the models, the MCP servers, and the skills. Each developer runs `ug`, and `ug` pulls the config down. `ug configure` writes the agent config files. It records the keys it owns in `~/.ucode/state.json`, so `ug revert` can restore the previous files.

Model selection therefore belongs to `ug`. This repository does not push a model list. A pushed list would duplicate the list `ug` publishes, and the two would disagree.

MDM owns only the settings `ug` cannot enforce:

| Setting | Owner | Why |
|---|---|---|
| Models, model aliases, context size | `ug` | The workspace's published managed config is the single source. |
| Gateway base URL | `ug` | `ug` records it per workspace in its state file. |
| Databricks identity and profile | `ug` | `ug configure` runs the OAuth login and saves the profile. |
| MCP servers, skills | `ug` | `ug configure mcp` and `ug configure skills` install them. |
| Telemetry (`otlp`) | **MDM** | The OTEL target is a fleet decision that is tied to Unity Catalog tables. |
| `authentication.disableClaudeAiSignIn` | **MDM** | A developer who signs in to Claude.ai bypasses the gateway. Only a managed profile stops this. |
| `workspace.allowedEgressHosts` | **MDM** | Network policy. |
| `workspace.disabledBuiltinTools` | **MDM** | Tool policy. |

## What the generator produces

The generator writes one bundle per OS to `agent_setups/generated/claude-desktop/<platform>/`.

1. `claude-setup.json` — the MDM-owned config. It carries policy and telemetry only. It has no `inference` block and no `models` block.
2. `ug-bootstrap-claude-desktop.sh` — the headless script that adds the missing halves. It runs `ug configure`, reads the workspace, the profile, the base URL, and the Claude model pins from `~/.ucode/state.json`, and writes `claude-setup.merged.json`.
3. The helper scripts that the merged config references by absolute path.

The generator does not produce the `.mobileconfig` or `.reg` MDM artifacts. The Claude Desktop app exports those after you import the JSON.

> **`ug` has no Claude Desktop target yet.** Claude Desktop shares Claude Code's gateway base URL (`…/ai-gateway/anthropic`) and its Claude model pins. So the bootstrap runs `ug configure --agents claude` and reads that state. When `ug` adds a custom model surface, it replaces this bootstrap.

---

## Four-phase deployment model

| Phase | Who | What | MDM-pushable? |
|---|---|---|---|
| **A — Helper placement** | IT admin | Place the helper scripts and the bootstrap at the absolute path the config references | **Yes** |
| **B — Bootstrap** | IT admin or developer | Run `ug-bootstrap-claude-desktop.sh` to produce `claude-setup.merged.json` | **Yes**, as a script payload with `--use-pat` |
| **C — Import and export** | IT admin (once) | Import the merged JSON in the app, test, then export the MDM profile | **No** — the app UI does this once |
| **D — User auth** | Each developer | `ug configure --profiles <profile>` | **No** — browser OAuth (U2M) |

Phase B produces the complete config. Phase C produces the MDM profile you distribute to the fleet. Push only the policy and telemetry keys from that profile. Leave inference and models to `ug` on each device.

---

## Why a credential helper

Claude Desktop needs a bearer token on every token refresh. The app caches the token for `credential.ttlSec` seconds, then re-runs the helper.

The helper is a thin wrapper around `ucode auth-token`. That is `ug`'s own token helper, and it is the same one Claude Code's `apiKeyHelper` and Codex's auth command already use. So Claude Desktop authenticates as the same identity, through the same code path, as every agent `ug` launches.

`ucode auth-token` is a hidden command. `ug --help` does not list it. It is nonetheless the supported entry point for exactly this purpose.

### What `ug` handles, so the helper does not

The wrapper carries no authentication logic. `ug` supplies all of it:

- It reads the workspace and the profile from its own state. The helper passes no arguments in the normal case.
- It honours the `use_pat` flag saved in that state, and static-PAT profiles.
- It short-circuits on `$DATABRICKS_BEARER` for CI.
- It resolves the profile from the host when no profile is given.
- It retries token-cache lock contention with a jittered backoff. This matters. Claude Desktop re-runs the helper whenever `ttlSec` expires, and `ug`-launched agents compete for the same token cache.
- It re-authenticates non-interactively (`databricks auth login --no-browser`) when a session expires.

Do not reimplement any of this in the helper.

### What the wrapper does

The wrapper exists for two reasons only:

1. `ucode auth-token` prints the token with a trailing newline. Claude Desktop's credential contract wants the bare token, so the wrapper strips it.
2. Claude Desktop starts under `launchd` (macOS) with a minimal `PATH`. The wrapper resolves the `ucode` binary from an absolute-path candidate list. Set `UCODE_BIN` to override the path.

Set `$DATABRICKS_PROFILE` to force a profile. The generator bakes a profile as well, but the helper passes it only when `ug` has no state file yet, so a fresh device still authenticates.

The helper needs neither `jq` nor `python3`.

---

## Step 1 — Generate the bundles

```sh
make agent-claude-desktop PROFILE=<profile>
# or, directly:
python3 agent_setups/scripts/generate.py claude-desktop --profile <profile> --out-dir agent_setups/generated
```

The default platforms are macOS and Windows. Add `--platforms macos,windows,linux` to include Linux.

Each macOS or Linux bundle contains:

- `claude-setup.json`
- `databricks-token.sh`
- `ug-bootstrap-claude-desktop.sh`
- `otel-headers-helper.sh` (only when telemetry is wired)

Each Windows bundle contains:

- `claude-setup.json`
- `databricks-token.ps1`
- `databricks-token.cmd` (the shim the config points at)
- `otel-headers-helper.ps1` and `otel-headers-helper.cmd` (only when telemetry is wired)

Windows has no bootstrap script. The bootstrap is a POSIX shell script.

---

## Step 2 — Place the helper scripts (Phase A)

The `credential.command` value is an absolute path. The helper script must exist at that exact path. The default paths are:

| OS | Helper directory | Command target |
|---|---|---|
| macOS | `/Library/Application Support/ClaudeDesktop` | `databricks-token.sh` |
| Windows | `C:\ProgramData\ClaudeDesktop` | `databricks-token.cmd` |
| Linux | `/etc/claude-desktop` | `databricks-token.sh` |

To change a path, pass `--install-dir-macos`, `--install-dir-windows`, or `--install-dir-linux` at generation time. The bootstrap then writes the path you set.

### macOS and Linux

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos --source agent_setups/generated
```

`install.sh` places `databricks-token.sh`, `ug-bootstrap-claude-desktop.sh`, and `otel-headers-helper.sh` when it is present. It sets each script executable (mode 755). It does not place `claude-setup.json`.

### Windows

`install.sh` is a POSIX script. It does not run on Windows. Place the Windows helpers with Intune or a machine-wide script instead. Copy `databricks-token.cmd`, `databricks-token.ps1`, and the OTEL pair to `C:\ProgramData\ClaudeDesktop`. Keep the `.ps1` beside the `.cmd`. The `.cmd` shim runs the `.ps1` from its own directory.

### Local test (no root)

```sh
make claude-desktop-install-local PROFILE=<profile>
```

The target generates a bundle for this OS with the helper path set to a user-writable directory (`$HOME/Library/Application Support/ClaudeDesktop` on macOS, `$HOME/.config/claude-desktop` on Linux), then places the helper scripts there. Override the directory with `CD_LOCAL_DIR=<dir>`.

> **Windows scripts are not tested yet.** Test them on a Windows machine before a production rollout. The risk is now smaller for the credential helper: it holds no authentication logic, because `ucode auth-token` is one binary that runs the same way on Windows. The OTEL headers helper still carries a real PowerShell port, so test that one with care.

---

## Step 3 — Run the bootstrap (Phase B)

Run the bootstrap from the bundle directory, because it reads `claude-setup.json` from beside itself.

```sh
sh agent_setups/generated/claude-desktop/macos/ug-bootstrap-claude-desktop.sh --profile <profile>
```

The script runs `ug configure --agents claude`, reads `~/.ucode/state.json`, and writes `claude-setup.merged.json` beside the input config.

| Option | Effect |
|---|---|
| `--profile <name>` | The Databricks CLI profile to configure `ug` against. |
| `--use-pat` | Pass `--use-pat` to `ug`. `ug` reads the PAT from `~/.databrickscfg` and runs no browser login. Use this for CI and for a device-management payload. It requires `--profile`. |
| `--dry-run` | Print the merged config to standard output. Write nothing. |
| `--skip-configure` | Do not run `ug configure`. Read the existing `ug` state only. |
| `--default-tier <t>` | The family listed first, which the app treats as the default (default: `sonnet`). |
| `--small-context` | Do not request 1M context for the opus and sonnet families. |
| `--config <path>` | The MDM-owned config to merge (default: `claude-setup.json` beside the script). |
| `--out <path>` | Where to write the merged config. |

Inspect the result before you import it:

```sh
sh …/ug-bootstrap-claude-desktop.sh --profile <profile> --dry-run
```

The script needs `python3`, because it merges JSON. Claude Desktop never runs this script.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, or `--dry-run` |
| 1 | Usage error |
| 2 | `ug` not found. Set `UG_BIN` to its absolute path. |
| 3 | `ug configure` failed |
| 4 | `ug` state unusable: no workspace, no base URL, or no Claude model pins |
| 5 | The MDM config is missing or unreadable, or the merged write failed |

### Workspace mismatch

The bootstrap compares the telemetry endpoint host in `claude-setup.json` against the workspace `ug` is configured against. When the two differ, it prints a warning and still writes the config. That combination sends traces to one workspace and inference to another. Regenerate the bundle against the workspace `ug` uses, or point `ug` at the workspace that holds the telemetry tables.

---

## Step 4 — Import and export (Phase C)

1. Start Claude Desktop.
2. Open Help → Troubleshooting → Enable Developer Mode.
3. Open Developer → Configure third-party inference.
4. Import `claude-setup.merged.json` for this OS.
5. Test the connection.
6. Export the MDM profile from the app. The app produces a `.mobileconfig` on macOS or a `.reg` on Windows.
7. Remove the inference and model keys from the exported profile. Keep the policy and telemetry keys.
8. Distribute the trimmed profile to the fleet with your MDM (Jamf, Intune, or similar).

> **Verify what the app merges.** Confirm how Claude Desktop combines an MDM-managed profile with an imported configuration before a production rollout. When the app needs one complete configuration, distribute the merged file and use the MDM profile for enforcement only.

---

## Step 5 — User auth (Phase D)

Each developer runs this command once. The command opens a browser for SSO. MDM cannot push this step.

```sh
ug configure --profiles <profile>
```

`ug` performs the OAuth login and records the profile. The Claude Desktop credential helper then reads that profile from `~/.ucode/state.json`.

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
| `ug` (`ucode`) | Always critical — the credential helper mints every token with `ucode auth-token`, and the bootstrap supplies the workspace, profile, base URL, and models |
| `databricks` | Critical when telemetry is on — the OTEL headers helper reads the secret with it. `ug` invokes it internally for tokens. |
| `python3` | Critical for the bootstrap (JSON merge) and for the macOS/Linux OTEL helper |

The credential helper needs no `jq`, no `python3`, and no `sed`. It runs one command and strips a newline.

---

## Uninstall

`install.sh` removes the placed helper scripts on macOS and Linux.

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos --uninstall
```

The command removes only the files the version marker records. It preserves other files in the directory. It does not remove `claude-setup.json` or `claude-setup.merged.json`, because `install.sh` never placed them. Remove the imported configuration inside the app, and remove the exported MDM profile through your MDM.

To undo what `ug` wrote, run `ug revert`.

---

## Verify

1. Open Claude Desktop.
2. Confirm the app routes to the gateway base URL.
3. Confirm the model list matches the models `ug` publishes.
4. Send a test message to confirm inference works.
5. When telemetry is on, confirm trace rows land in the traces table.
