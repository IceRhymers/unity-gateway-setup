# Claude Desktop runbook — Unity AI Gateway third-party inference (MDM)

> **Related documents.** This runbook covers the generator and the bundle. For the
> fleet rollout, read `claude-desktop-mdm.md`. To rehearse the rollout in a
> Parallels VM, read `claude-desktop-vm-test.md`.

Claude Desktop reads an operator-imported configuration, not a file that MDM places on disk. So the deployment differs from Claude Code and Codex. The operator imports the generated JSON into the app, tests the connection, and then exports the OS-native MDM profile from the app.

This generator produces two things per OS:

1. The importable `claude-setup.json` (schema version 2, the nested-object form).
2. The credential helper scripts the JSON references by absolute path.

This generator does not produce the `.mobileconfig` or `.reg` MDM artifacts. The Claude Desktop app exports those after you import the JSON.

For a fleet you also need an installer package, because a configuration profile carries settings only and cannot place a file. Build it with `make claude-desktop-pkg`. See `claude-desktop-mdm.md` for the two-artifact model.

---

## Three-phase deployment model

| Phase | Who | What | MDM-pushable? |
|---|---|---|---|
| **A — Helper placement** | IT admin | Place the helper scripts at the absolute path the JSON references | **Yes** |
| **B — Import and export** | IT admin (once) | Import the JSON in the app, test, then export the MDM profile | **No** — the app UI does this once |
| **C — User auth** | The MDM triggers it. The developer signs in | The `ug-sso-bootstrap` LaunchAgent runs `ug configure`. A browser opens for SSO | **The trigger, yes.** The sign-in belongs to the person |

Phase A places the scripts. Phase B produces the MDM profile you distribute to the fleet. Phase C binds each developer's Databricks identity. No phase is optional.

---

## Why a credential helper

Claude Desktop needs a bearer token on every token refresh. The app caches the token for `credential.ttlSec` seconds, then runs the helper again.

The helper is a thin wrapper around `ug auth-token`. That is `ug`'s own token helper, and it is the same one Claude Code's `apiKeyHelper` and Codex's auth command use. So a developer authenticates once with `ug configure`, and every surface after that reads its token from one place. This removes the second auth path that a direct `databricks auth token` call created.

`ug auth-token` is a hidden command. `ug --help` does not list it. It is nonetheless the supported entry point for this purpose.

### What `ug` handles, so the helper does not

The wrapper carries no authentication logic. `ug` supplies all of it:

- It short-circuits on `$DATABRICKS_BEARER` for CI.
- It resolves the CLI profile from the workspace host.
- It honours static-PAT profiles and the `use_pat` flag saved in its state. The
  MDM deployment does not use that path. It uses OAuth single sign-on.
- It retries token-cache lock contention with a jittered backoff. This matters. Claude Desktop runs the helper whenever `ttlSec` expires, and `ug`-launched agents compete for the same token cache.
- It re-authenticates non-interactively when a session expires.

Do not reimplement any of this in the helper.

### What the wrapper does

The wrapper has three jobs only:

1. It resolves the `ug` binary from an absolute-path candidate list, because Claude Desktop starts under `launchd` (macOS) with a minimal `PATH`. Set `UG_BIN` to override the path.
2. It passes `--host`, baked at generation time. This pins the token to the same workspace as `inference.baseUrl`. A developer with several workspaces configured in `ug` would otherwise get a token for whichever workspace `ug` selected last.
3. It strips the trailing newline that `ug auth-token` prints, because Claude Desktop's credential contract wants the bare token.

Set `$DATABRICKS_PROFILE` to force a profile. Otherwise `ug` resolves the profile from the baked host.

The helper is POSIX `sh`. It needs no `jq`, no `python3`, and no `sed`.

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
- `ug-sso-bootstrap.sh` and `ug-sso-bootstrap.plist` (the MDM-triggered SSO login;
  the plist is macOS only, and `--no-sso-bootstrap` omits both)

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

### Local test (no root)

To test the config on your own machine without root, run one target:

```sh
make claude-desktop-install-local PROFILE=<profile>
```

The target generates a bundle for this OS with the helper path set to a user-writable directory (`$HOME/Library/Application Support/ClaudeDesktop` on macOS, `$HOME/.config/claude-desktop` on Linux), then places the helper scripts there. Override the directory with `CD_LOCAL_DIR=<dir>`. The generated `claude-setup.json` references the same directory, so the import works at once. The target prints the JSON path to import.

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

This command runs once per developer. It opens a browser for SSO.

```sh
ug configure --profiles <profile>
```

`--profiles` works here because your own machine already has the profile in
`~/.databrickscfg`. It fails on a freshly imaged device, where no such file exists.

So this form is for a local test only. In a fleet the MDM triggers the login through
the generated `ug-sso-bootstrap` LaunchAgent, which passes `--workspaces <url>`
instead and guards itself against opening a browser at every login. See
`claude-desktop-mdm.md` section 8.

This is the only authentication step, and it serves every surface. It sets up the terminal agents `ug` launches, and it is what the Claude Desktop credential helper reads from. A developer does not authenticate twice.

To verify the helper independently of the app:

```sh
"/Library/Application Support/ClaudeDesktop/databricks-token.sh" | wc -c
```

It must print a byte count near 800 and exit 0. Count the bytes. Never print the
token, because it is a live credential.

It must print the first characters of a token and exit 0. Diagnostics go to standard error, so they do not corrupt the token contract.

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
| `ug` | Always critical — the credential helper mints every token with `ug auth-token`, and `ug configure` performs the one-time login |
| `databricks` | Critical when telemetry is on — the OTEL headers helper reads the UC secret with it. `ug` invokes it internally for tokens. |
| `python3` | Critical when telemetry is on — the macOS/Linux OTEL helper uses it |

The credential helper `databricks-token.sh` is POSIX `sh`. It needs no `jq`, no `python3`, and no `sed`. It runs one command and strips a newline.

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
