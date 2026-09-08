# Claude Desktop — MDM deployment guide

This guide deploys Claude Desktop to a fleet, routed through the Databricks Unity
AI Gateway. It covers what MDM pushes, what each developer does, and what you
verify.

**Status.** Two behaviours in this guide are not yet confirmed. Each one is marked
**UNCONFIRMED**. Test them in a VM before a production rollout. See
`claude-desktop-vm-test.md`.

---

## 1. How Claude Desktop differs from Claude Code

Claude Code reads a managed settings file that an MDM tool places on disk. So a
push alone configures it.

Claude Desktop does not work that way. It reads an **operator-imported**
configuration. So the deployment has an extra shape:

1. You generate the configuration.
2. You import it once, into the app, on one machine.
3. The **app** exports the OS-native MDM profile.
4. You push that profile to the fleet.

The generator does not produce the `.mobileconfig` or the `.reg` file. Only the
app produces them. Plan for that import step. You cannot skip it.

---

## 2. Division of labour

Three components own separate concerns. Do not move a concern between them
without reading the ownership table in the root `README.md`.

| Concern | Owner |
|---|---|
| The gateway, the model services, the OTEL tables | Terraform (`terraform/`) |
| The whole importable config: endpoint, models, policy, telemetry | The generator (`agent_setups/`) |
| Every Databricks token | `ug` |
| Fleet enforcement of the policy keys | Your MDM tool |

**The generator owns the models.** `ug` has no Claude Desktop target and cannot
discover models for it. So the generator reads the Terraform outputs and writes
the model list.

**`ug` owns authentication.** The credential helper calls `ug auth-token`. A
developer runs `ug configure` one time. After that the terminal agents and Claude
Desktop draw a token from the same place. There is no second login.

---

## 3. What the generated bundle contains

Generate one bundle for each operating system.

```sh
make agent-claude-desktop PROFILE=<profile>
```

A macOS or Linux bundle holds three files:

| File | Purpose |
|---|---|
| `claude-setup.json` | The complete importable configuration |
| `databricks-token.sh` | The credential helper. Calls `ug auth-token` |
| `otel-headers-helper.sh` | Mints the telemetry token. Present only when telemetry is on |

A Windows bundle holds `claude-setup.json`, `databricks-token.ps1`, a
`databricks-token.cmd` shim, and the two OTEL files.

`claude-setup.json` carries eight top-level keys.

| Key | Contents |
|---|---|
| `$schemaVersion` | `2`, the nested-object form |
| `inference` | The gateway base URL, and the helper-script credential |
| `models` | The Claude models, with family tiers and 1M context flags |
| `chatSurface`, `extensions` | The app surfaces, both enabled |
| `workspace` | The egress allow-list, and the disabled built-in tools |
| `authentication` | The Claude.ai sign-in lockout |
| `otlp` | The OTEL endpoint, the traces table, and the headers helper |

---

## 4. The four phases

| Phase | Who | What | MDM can push it? |
|---|---|---|---|
| **A — Helpers** | IT admin | Place the helper scripts at the absolute path the config names | **Yes** |
| **B — Author** | IT admin, once | Import the config, test it, export the MDM profile | **No.** A GUI flow |
| **C — Push** | IT admin | Push the exported profile to the fleet | **Yes** |
| **D — Auth** | Each developer | `ug configure --profiles <profile>` | **No.** Browser OAuth |

Phase B happens one time, on one machine. Phase D happens one time per developer.

---

## 5. Phase A — place the helper scripts

The `credential.command` value in the config is an absolute path. The helper must
exist at that exact path on every device.

| OS | Directory | Command target |
|---|---|---|
| macOS | `/Library/Application Support/ClaudeDesktop` | `databricks-token.sh` |
| Windows | `C:\ProgramData\ClaudeDesktop` | `databricks-token.cmd` |
| Linux | `/etc/claude-desktop` | `databricks-token.sh` |

To change a directory, pass `--install-dir-macos`, `--install-dir-windows`, or
`--install-dir-linux` when you generate. The config then names the path you set.

On macOS and Linux:

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos \
  --source agent_setups/generated
```

The installer sets each script executable. It does not place `claude-setup.json`,
because an operator imports that file.

On Windows, `install.sh` does not run. Push the two `.cmd` and `.ps1` pairs with
Intune, or with a machine-wide script. Keep each `.ps1` beside its `.cmd`. The
shim runs the `.ps1` from its own directory.

---

## 6. Phase B — author the MDM profile

Do this once, on one machine, with the helper scripts already placed.

1. Start Claude Desktop.
2. Open Help. Open Troubleshooting. Turn on Developer Mode.
3. Open Developer. Open Configure third-party inference.
4. Import `claude-setup.json` for this operating system.
5. Test the connection.
6. Send a test message, and confirm it succeeds.
7. Export the MDM profile. The app writes a `.mobileconfig` on macOS, or a `.reg`
   file on Windows.

> **UNCONFIRMED — what the profile must carry.** We do not yet know whether an
> MDM profile replaces the imported configuration or merges with it. This decides
> whether each device also needs the import.
>
> - If the profile carries the whole configuration, push all of it. A developer
>   then needs no import.
> - If the profile only overlays policy, push the policy keys, and give each
>   device the config to import as well.
>
> Test this before you roll out. Install the profile on a clean machine, perform
> no import, and see whether inference works.

---

## 7. Phase C — push to the fleet

Push the exported profile with your MDM tool. Jamf, Intune, and Kandji all accept
the app's export.

Push the helper scripts in the same wave. A profile that names a helper which does
not exist produces an authentication failure with no clear cause.

Order the wave this way:

1. Push the helper scripts.
2. Push `ug`, if your fleet does not have it.
3. Push the configuration profile.

---

## 8. Phase D — developer authentication

Each developer runs one command. It opens a browser for single sign-on. No MDM
tool can push this step, because the token belongs to the person.

```sh
ug configure --profiles <profile>
```

This is the only authentication step. It also configures the terminal agents that
`ug` launches. A developer does not authenticate twice.

For a headless device, use a personal access token instead.

```sh
ug configure --profiles <profile> --use-pat
```

That form reads the token from `~/.databrickscfg` and runs no browser.

---

## 9. How authentication works

Claude Desktop asks the credential helper for a token whenever
`credential.ttlSec` expires. The helper calls `ug auth-token`.

`ug` supplies all of the token logic:

1. It short-circuits on `$DATABRICKS_BEARER`, for continuous integration.
2. It resolves the Databricks profile from the workspace host.
3. It reads static personal access tokens when the profile holds one.
4. It retries token-cache contention with a jittered backoff.
5. It re-authenticates without a browser when a session expires.

The helper adds three things only:

1. It resolves the `ug` binary by absolute path. Claude Desktop starts under
   `launchd` with a minimal `PATH`. Set `UG_BIN` to override the path.
2. It passes the workspace host, baked at generation time. This pins the token to
   the same workspace as `inference.baseUrl`. A developer with several workspaces
   in `ug` would otherwise get a token for the wrong one.
3. It removes the trailing newline that `ug auth-token` prints.

`ug auth-token` is a hidden command. `ug --help` does not list it. It is still the
supported entry point, and Claude Code and Codex already use it.

---

## 10. Telemetry

The generator wires telemetry when the Terraform `telemetry` output holds a traces
table. Pass `--telemetry on` to require it. Pass `--telemetry off` to skip it.

Claude Desktop carries one `otlp.headers` set. So it routes traces to one Unity
Catalog table. It cannot split metrics, logs, and traces across tables the way
Claude Code does. The generator wires **traces**.

`otel-headers-helper.sh` mints a token for the dedicated telemetry service
principal. The token is down-scoped to the traces table. Each developer needs
`READ_SECRET` on the telemetry Unity Catalog secret.

> **UNCONFIRMED — the `otlp` key names.** The importable-JSON `otlp` shape can
> change between Claude Desktop releases. Check the key names against the live
> Claude Desktop configuration reference before you roll out.

---

## 11. Prerequisites

| Tool | Criticality |
|---|---|
| `ug` | Always critical. The helper mints every token with it |
| `databricks` | Critical when telemetry is on. The OTEL helper reads the secret with it |
| `python3` | Critical when telemetry is on. The macOS and Linux OTEL helper uses it |

The credential helper is POSIX `sh`. It needs no `jq`, no `python3`, and no `sed`.

---

## 12. Verify a deployed device

Run these four checks on a device that received the push.

1. Confirm the profile installed.

```sh
profiles list -type=configuration
```

2. Confirm the helper returns a token. Count the bytes. Never print the token.

```sh
"/Library/Application Support/ClaudeDesktop/databricks-token.sh" | wc -c
```

3. Open Claude Desktop, and confirm the model list matches the gateway.

4. Send a test message. Then confirm a new row in the traces table.

---

## 13. Uninstall

Remove the helper scripts on macOS and Linux.

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos --uninstall
```

The command removes only the files the version marker records. It leaves
`claude-setup.json`, because the installer never placed it.

Remove the configuration profile with your MDM tool. Remove the imported
configuration inside the app. To undo what `ug` wrote, run `ug revert`.

---

## 14. Troubleshooting

| Symptom | Likely cause |
|---|---|
| The app reports an authentication failure | `ug` is absent, or the developer did not run `ug configure`. Run the helper by hand and read standard error |
| The helper prints "ug not found" | `ug` is not on a path the helper checks. Set `UG_BIN` |
| The token is for the wrong workspace | The bundle was generated against a different host. Compare the baked host against `inference.baseUrl` |
| The model list is empty | The gateway exposes no Claude model. The generator fails in this case, so check the generation log |
| No traces arrive | The developer lacks `READ_SECRET` on the telemetry secret. Run the OTEL helper by hand |
| Inference fails after an MDM push, and worked before | The profile may have replaced the imported config. See the UNCONFIRMED note in section 6 |
