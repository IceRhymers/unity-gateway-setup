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

### A rollout needs three artifacts, not one

This is the point operators get wrong most often. A configuration profile carries
**settings only**. It cannot place a file, and it cannot run a command. So a profile
alone will never install the credential helper, the SSO bootstrap, or the
LaunchAgent.

| Artifact | Delivers | Produced by |
|---|---|---|
| **`claude-desktop-<v>.pkg`** | The credential and OTEL helper scripts | `make claude-desktop-pkg` |
| **`ug-bootstrap-<v>.pkg`** | `uv`, the SSO bootstrap, its LaunchAgent | `make ug-bootstrap-pkg` |
| **`.mobileconfig`** | The Claude Desktop settings | The Claude Desktop app, on export |

Claude Code and Codex have a fourth artifact, `make coding-agents-pkg`. Each package
is independent, and none writes a file another one writes. See `jamf.md`.

All three go to the fleet. Push both packages before the profile, because the
profile's `credential.command` names a script a package places.

`ug` itself needs no package. `ug-bootstrap.pkg` places `uv`, and its LaunchAgent
installs `ug` for each user at their first login. That deferral is deliberate. A
package script runs as root, so `uv tool install` would write into root's home and
the developer would get nothing. See section 8.

The two packages may arrive in either order.

For the Jamf steps, read `jamf.md`.

> **You do not need an MDM to test the install.** `installer -pkg` over SSH is
> completely headless and exercises the same payload an MDM would push. An MDM adds
> distribution and scoping, not install mechanics. The VM runbook uses `installer`
> for exactly this reason.

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
| `ug-sso-bootstrap.sh` | Guards, then runs `ug configure` for the one-time SSO login |
| `ug-sso-bootstrap.plist` | The LaunchAgent that runs the script at login. macOS only |

Build the installer package from that bundle:

```sh
make claude-desktop-pkg PROFILE=<profile>
# signed for distribution:
make claude-desktop-pkg PROFILE=<profile> PKG_SIGN_ID="Developer ID Installer: <org> (<team>)"
```

It writes `dist/claude-desktop-<version>.pkg`, which places two files.

| Payload path | Mode |
|---|---|
| `/Library/Application Support/ClaudeDesktop/databricks-token.sh` | 755 |
| `/Library/Application Support/ClaudeDesktop/otel-headers-helper.sh` | 755 |

Build the SSO bootstrap package as well.

```sh
make ug-bootstrap-pkg PROFILE=<profile>
```

It writes `dist/ug-bootstrap-<version>.pkg`, which places three files.

| Payload path | Mode |
|---|---|
| `/usr/local/bin/uv` | 755 |
| `/Library/Application Support/ClaudeDesktop/ug-sso-bootstrap.sh` | 755 |
| `/Library/LaunchAgents/ug-sso-bootstrap.plist` | 644 |

Its postinstall loads the LaunchAgent for the user who is logged in, so the ug
install and the SSO prompt start right after the package lands instead of waiting for
a logout. At imaging time there is no console user, and the agent then loads at the
first real login. Pass `--no-autoload` through `ARGS` to suppress that.

> **`uv` must match the fleet's architecture.** The builder packages the `uv` on the
> build machine by default, and warns when that binary is not universal. An arm64-only
> `uv` does not run on an Intel Mac. Pass `UV_BIN=<path>` to package a universal one.

An unsigned package installs through `installer(8)` and through an MDM. Gatekeeper
blocks a double-click install, so sign it for anything a person opens by hand.

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
| **D — Auth** | The MDM triggers it. The developer signs in | A LaunchAgent runs `ug configure`. A browser opens for SSO | **The trigger, yes.** The sign-in belongs to the person |

Phase B happens one time, on one machine. Phase D happens one time per developer,
and the MDM starts it. A developer types no configuration and clicks through no
Settings pane. They sign in to a browser once.

Only Phase B needs a GUI, and only on the machine that authors the profile.

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

### macOS, for a fleet

Deploy the package. This is what an MDM does, and what `installer` does locally.

```sh
sudo installer -pkg dist/claude-desktop-<version>.pkg -target /
```

### macOS and Linux, for a local build or a Linux fleet

`install.sh` places the same files from a generated bundle, with no package.

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos \
  --source agent_setups/generated
```

The installer sets each script executable, places the LaunchAgent in
`/Library/LaunchAgents`, and records what it placed so `--uninstall` can remove it.
It does not place `claude-setup.json`, because an operator imports that file.

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

1. Push the `.pkg`. It places the helper scripts, the bootstrap, and the LaunchAgent
   in one step.
2. Push `ug`, if your fleet does not have it.
3. Push the `.mobileconfig`.

Order 1 before 3: the profile's `credential.command` names a script the package
places. A profile that arrives first points at a file that does not exist yet, and
Claude Desktop reports an authentication failure with no obvious cause.

The order of 2 is free. The bootstrap tolerates a missing `ug` and retries at the
next login.

---

## 7a. Jamf Pro

`jamf.md` is the single Jamf reference for every agent in this repository. It records
which Jamf object carries which artifact, the signing and certificate facts, the
scoping, and the uninstall policies.

Read it for the Jamf steps. In short:

| Artifact | Jamf object |
|---|---|
| The `.pkg` | A Package, run by a Policy |
| The `.mobileconfig` | A Configuration Profile |

Let the Package Policy run before the Configuration Profile reaches the device. See
the ordering note in section 7.

---

## 8. Phase D — developer authentication

The developer does not configure anything. The MDM pushes a LaunchAgent, the agent
runs a script at login, and a browser opens for single sign-on. The developer signs
in. That is the whole step.

**Never use a personal access token.** The login is OAuth single sign-on. A PAT is
a long-lived static secret, it does not carry the developer's identity, and this
deployment does not need one.

### The generator emits both pieces, and ug-bootstrap.pkg ships them

The macOS bundle carries them. Do not hand-write either file.

| File | Placed at | Purpose |
|---|---|---|
| `ug-sso-bootstrap.sh` | The helper directory, mode 755 | Installs `ug` when absent, then runs `ug configure` when needed |
| `ug-sso-bootstrap.plist` | `/Library/LaunchAgents`, mode 644 | Runs the script at each user login |

`ug-bootstrap.pkg` ships both, plus the `uv` the script needs. `install.sh` also
places both from a bundle, but it does not place `uv`.

Pass `--no-sso-bootstrap` at generation time to omit them when your MDM already runs
`ug configure` some other way. Pass `--launchagent-label com.<your-org>.<name>` to use
your own reverse-DNS label. Pass `--ug-ref <tag>` to pin the `ug` version the script
installs.

### Why the ug install is deferred to login

`uv tool install` writes into the invoking user's home. A package preinstall or
postinstall script runs as **root**, so it would install `ug` into `/var/root` and the
developer would get nothing. At imaging time there is no console user at all, which is
exactly when an unattended MDM install runs.

So the script installs `ug` at first login, in the user's own session:

1. It resolves `uv` by absolute path, starting with the packaged `/usr/local/bin/uv`.
   A LaunchAgent inherits a minimal `PATH`, and `uv` normally lives in a per-user
   directory.
2. It runs `uv tool install git+https://github.com/databricks/ucode`.
3. It resolves `ug` again, then continues to the SSO login.

A failure is never fatal. The script logs it and exits 0, and the next login retries.
So a transient network outage costs one login.

`ug` then lands where `uv tool install` puts it, which is also where `ug upgrade`
writes and where the credential helper looks first. So no packaged copy competes with
a developer's own.

One consequence to know: `ug`'s own files are outside the package payload. So
`pkgutil --files`, `install.sh --uninstall`, and Jamf inventory do not see them. Read
the bootstrap log, or use a Jamf Extension Attribute, to confirm `ug` is present.

### Why a LaunchAgent, and not a LaunchDaemon

- A LaunchAgent runs inside the user's GUI session, so it can open a browser.
- A LaunchDaemon runs as root outside that session, so it cannot.

The plist also sets `LimitLoadToSessionType` to `Aqua`. So the agent never fires in
an SSH or background session, where no browser can open.

The plist sets `RunAtLoad` and no `KeepAlive`. So it runs once per login. A
developer who dismisses the browser is not nagged again until the next login.

### Why the script guards itself

`ug configure` sets `force_login` whenever `--use-pat` is absent. So it runs
`databricks auth login` unconditionally, and a browser opens on **every**
invocation, even when the session is still valid.

An unguarded login trigger therefore opens a browser at every login. The generated
script probes first:

1. It resolves `ug` by absolute path. A LaunchAgent does not inherit the user's
   `PATH`.
2. When `ug` is absent it logs the fact and exits. It does not try to configure.
3. It runs `ug auth-token --host <workspace>`. On success it exits silently. **No
   browser opens.**
4. Only on failure does it run `ug configure`.

`ug auth-token` is a safe probe. It never opens a browser, it never waits for
input, and its internal re-auth attempt is bounded at 30 seconds.

`ug configure` waits up to 300 seconds for the browser login. A developer who
misses that window gets another browser at the next login, because the probe fails
again. So the flow is self-healing.

### Why the script passes `--workspaces`, not `--profiles`

`--profiles` requires the named profile to exist in `~/.databrickscfg` already, and
it raises an error when the profile is absent. A freshly imaged device has no such
file. So the script passes `--workspaces <url>`, which accepts a bare workspace URL
and sets the workspace up from nothing.

Use `--profiles` only for a local test on a machine that already has the profile.

### Read the log

The script logs to the user's own log directory:

```sh
cat ~/Library/Logs/ug-sso-bootstrap.log
```

Each run writes one line. Read it first when a device does not authenticate.

### Load the agent without a reboot

The agent loads at the next login. To load it immediately, run this as the
logged-in user:

```sh
launchctl load "/Library/LaunchAgents/ug-sso-bootstrap.plist"
```

---

## 9. How authentication works

Claude Desktop asks the credential helper for a token whenever
`credential.ttlSec` expires. The helper calls `ug auth-token`.

`ug` supplies all of the token logic:

1. It short-circuits on `$DATABRICKS_BEARER`, for continuous integration.
2. It resolves the Databricks profile from the workspace host.
3. It reads static personal access tokens when a profile holds one. This
   deployment does not use that path.
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

2. Confirm the settings reached the app's preference domain. Claude Desktop reads
   `com.anthropic.claudefordesktop`. A profile-delivered value appears under
   `/Library/Managed Preferences`, which is read-only to the user and separate from
   the app's own `~/Library/Preferences` copy.

```sh
ls /Library/Managed\ Preferences/ | grep -i anthropic
defaults read com.anthropic.claudefordesktop
```

   An empty `grep` means no profile is applied. The `defaults read` output then
   shows only the app's local state, such as window positions.

3. Confirm the helper returns a token. Count the bytes. Never print the token.

```sh
"/Library/Application Support/ClaudeDesktop/databricks-token.sh" | wc -c
```

4. Open Claude Desktop, and confirm the model list matches the gateway.

5. Send a test message. Then confirm a new row in the traces table.

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
| The app reports an authentication failure | `ug` is absent, or the SSO login never completed. Run the helper by hand and read standard error |
| A browser opens at every login | The plist runs something other than the generated script, which carries the probe |
| The log says "ug absent and no uv found" | `ug-bootstrap.pkg` did not land, so `uv` is missing. Install it, or set `UV_BIN` |
| The log says "ug install failed" | No network at that login, or the wrong `uv` architecture. The next login retries |
| No browser ever opens | The trigger is a LaunchDaemon, not a LaunchAgent. A daemon runs as root, outside the GUI session |
| The helper prints "ug not found" | `ug` is not on a path the helper checks. Set `UG_BIN` |
| The token is for the wrong workspace | The bundle was generated against a different host. Compare the baked host against `inference.baseUrl` |
| The model list is empty | The gateway exposes no Claude model. The generator fails in this case, so check the generation log |
| No traces arrive | The developer lacks `READ_SECRET` on the telemetry secret. Run the OTEL helper by hand |
| Inference fails after an MDM push, and worked before | The profile may have replaced the imported config. See the UNCONFIRMED note in section 6 |
