# Runbook — test the Claude Desktop MDM rollout in a Parallels VM

This runbook drives a macOS guest from the host to test the Claude Desktop
rollout. It exists to rehearse and record a demo. Follow it in order.

**Audience.** The operator who records the demo. It assumes you built a bundle
already. See `claude-desktop.md` for the generator and `claude-desktop-mdm.md`
for the fleet deployment itself.

---

## What this environment can and cannot do headlessly

Read this section first. It decides how much of the demo you script.

**What headless means here.** No GUI pane to click through, and no
developer-typed configuration. A browser that opens and asks the developer to
sign in through SSO is **not** a violation. It is the intended flow. This
deployment uses no personal access tokens.

| Step | Needs the GUI? | How |
|---|---|---|
| Start, suspend, and stop the guest | No | `prlctl` |
| Snapshot and revert between takes | No | `prlctl snapshot` |
| Install the helper payload | No | A `.pkg` through `installer` over SSH |
| Install `ug` | No | `ssh` |
| Trigger `ug configure` | No | `ssh`, or the LaunchAgent the MDM pushes |
| **Complete the SSO login** | **A browser** | The developer signs in once. This is intended |
| Verify the token and the routing | No | `ssh` |
| Capture a screenshot | No | `prlctl capture` |
| **Install the `.mobileconfig`** | **A Settings pane** | Or a real MDM channel |
| **Import `claude-setup.json` into the app** | **The app window** | GUI flow in the app |

### Why the profile install is not headless

Apple removed profile installation from the `profiles` tool. The macOS 26 man
page states it directly:

> Starting with macOS 11.0 (profiles tool 8.0 or later), this tool cannot be
> used to install configuration profiles. You should add your profiles using the
> System Settings Profiles preference pane.

The tool keeps `list`, `show`, `status`, and `remove`. So you can **verify** and
**clean up** a profile from a script. You cannot **install** one.

You have two ways to handle this:

1. **Approve the profile by hand in each take.** Nothing extra to build. The
   approval is also what a developer sees on a machine that no MDM manages, so
   the demo stays honest.
2. **Enrol the guest in an MDM once.** A user approves the enrolment one time.
   After that the MDM channel installs profiles silently, and every later push is
   headless. This models Jamf or Intune correctly, so prefer it if you have an
   MDM to point at.

### Why this runbook uses SSH, not `prlctl exec`

`prlctl exec` needs Parallels Tools inside the guest. The macOS guest reports
`GuestTools: state=not_installed`, and it is an Apple Virtualization framework
guest (a `.macvm` bundle) on Apple Silicon. Host Shared Folders is also off.

SSH avoids all of that. You enable Remote Login one time in the guest. Every step
after that runs from the host.

---

## Before you start

1. Confirm free disk space on the host. Run `df -h /`.
2. Reserve at least 30 GB. The guest is about 29 GB, and each snapshot grows as
   the guest diverges from it.
3. Stop if you have less than 30 GB free. A full host disk suspends the guest and
   can corrupt a take. This is the most likely cause of a failed recording.
4. Confirm the guest can reach your identity provider. The login uses browser
   SSO, so the guest needs network access to the provider.

> **Do not skip step 3.** A macOS guest writes a memory state file when it
> suspends. That file is as large as the guest's RAM.

---

## Step 1 — Prepare the guest, one time

Do these four things once, through the guest GUI. Every later step is headless.

1. Start the guest: `prlctl start macOS`.
2. Open System Settings. Open General. Open Sharing. Turn on Remote Login.
3. Record the guest IP address: `prlctl list -f`.
4. Copy your host SSH key to the guest:
   `ssh-copy-id <guest-user>@<guest-ip>`.

Define two shell variables on the host. Every later command uses them.

```sh
VM=macOS
GUEST=<guest-user>@<guest-ip>
```

Confirm the connection works.

```sh
ssh "$GUEST" 'sw_vers -productVersion && whoami'
```

---

## Step 2 — Take the baseline snapshot

Take this snapshot before you change anything in the guest. You revert to it
between takes, so each take starts from the same state.

```sh
prlctl snapshot "$VM" --name clean-baseline
prlctl snapshot-list "$VM"
```

To reset between takes:

```sh
prlctl snapshot-switch "$VM" --id <snapshot-id>
```

---

## Step 3 — Deploy the payload as a package

A configuration profile carries settings only. It cannot place a file. So the
helper scripts, the SSO bootstrap, and the LaunchAgent arrive in a `.pkg`, which is
also the artifact an MDM deploys. Installing it with `installer` over SSH is fully
headless, and it exercises the same payload Jamf would push.

Build the bundle and the package on the host.

```sh
make agent-claude-desktop PROFILE=<profile>
make packages             PROFILE=<profile>
```

`make packages` builds all three packages. Use the individual targets
(`claude-desktop-pkg`, `ug-bootstrap-pkg`, `coding-agents-pkg`) to rebuild one.

`claude-desktop.pkg` carries the helper scripts. `ug-bootstrap.pkg` carries the SSO
bootstrap and its LaunchAgent. Install both. Neither carries `uv`, which the guest
needs already.

Copy the package to the guest and install it.

```sh
for p in $(ls -t dist/claude-desktop-*.pkg | head -1) \
         $(ls -t dist/ug-bootstrap-*.pkg   | head -1); do
  scp "$p" "$GUEST":/tmp/pkg.pkg
  ssh "$GUEST" 'sudo installer -pkg /tmp/pkg.pkg -target / && echo INSTALL_OK'
done
```

That is the whole install, with no GUI at any point.

Confirm what landed, and with which modes:

```sh
ssh "$GUEST" 'ls -l "/Library/Application Support/ClaudeDesktop" /Library/LaunchAgents/ug-sso-bootstrap.plist'
```

Expect the three scripts at mode 755, and the plist at 644. launchd refuses a
group- or world-writable agent plist, so 644 is not cosmetic.

Confirm the path the config expects matches the path on disk:

```sh
python3 -c "import json;print(json.load(open('agent_setups/generated/claude-desktop/macos/claude-setup.json'))['inference']['credential']['command'])"
```

The two must be identical. If they differ, regenerate with
`--install-dir-macos "<path>"` and rebuild the package.

> **The postinstall may already have started the SSO login.** The package loads the
> LaunchAgent for the logged-in user, so a browser can appear as soon as the install
> finishes. That is intended. Step 6 covers it. To install without that, add
> `ARGS=--no-autoload` when building the package.

### Without a package

`install.sh` places the same files from a generated bundle. Use this to test a
change without rebuilding a package.

```sh
sh agent_setups/deploy/install.sh --agents claude-desktop --os macos \
  --source agent_setups/generated
```

## Step 4 — Install the configuration profile

This step needs the GUI. Pick one of the two ways from the first section.

### By hand, in each take

1. Copy the profile to the guest:
   `scp claude-desktop.mobileconfig "$GUEST":~/Desktop/`.
2. Open the guest window.
3. Double-click the profile on the Desktop.
4. Open System Settings. Open General. Open Device Management.
5. Approve the profile.

### Through an MDM

1. Enrol the guest in your MDM one time. A user approves the enrolment.
2. Push the profile through the MDM.
3. The profile installs with no GUI step.

### Verify, headlessly, either way

```sh
ssh "$GUEST" 'profiles list -type=configuration'
ssh "$GUEST" 'profiles status -type=enrollment'
```

To remove the profile between takes:

```sh
ssh "$GUEST" 'sudo profiles remove -identifier <profile-identifier> -forced'
```

---

## Step 5 — Install `ug`

Install `ug` in the guest from the host.

```sh
ssh "$GUEST" 'command -v ug || echo "ug absent"'
```

Install it with the method your team uses. Then confirm the version.

```sh
ssh "$GUEST" 'ug --version'
```

The credential helper resolves `ug` by absolute path, not by `$PATH`, because
Claude Desktop starts under `launchd`. Confirm `ug` sits on one of the paths the
helper checks.

```sh
ssh "$GUEST" 'ls -l ~/.local/bin/ug /opt/homebrew/bin/ug 2>/dev/null'
```

If it sits somewhere else, set `UG_BIN` for the app, or move the binary.

---

## Step 6 — Trigger the SSO login the way the MDM does

The bundle carries the same two files the MDM pushes. Test those, not a hand-typed
command. Do not use a personal access token.

| File | Placed at |
|---|---|
| `ug-sso-bootstrap.sh` | The helper directory, mode 755 |
| `ug-sso-bootstrap.plist` | `/Library/LaunchAgents`, mode 644 |

Step 3 already placed both, because `ug-bootstrap.pkg` carries them.

At the first login the agent installs `ug` with `uv`, then runs the SSO login. So take
1 below exercises both steps.

> **Confirm three prerequisites in the guest before take 1.** No package carries any
> of them, and each one stops the chain at a different point.
>
> ```sh
> ssh "$GUEST" 'ls -l ~/.local/bin/uv /opt/homebrew/bin/uv /usr/local/bin/uv 2>/dev/null'
> ssh "$GUEST" 'cat ~/.config/uv/uv.toml 2>/dev/null'
> ssh "$GUEST" 'command -v databricks'
> ```
>
> 1. **`uv`**, or the bootstrap cannot install `ug`.
> 2. **A `uv` index configuration**, when the network blocks `pypi.org`. Copy the one
>    your own machine uses. Without it `uv tool install` fails on a connection refused.
> 3. **The `databricks` CLI**, because `ug` shells out to it. When it is absent, `ug`
>    tries to install it with `sudo`, which cannot prompt from a LaunchAgent.
>
> Each failure is recorded in the bootstrap log, and each one retries at the next login.

### Take 1 — a device with no authentication

Load the agent as the logged-in guest user. It also loads on its own at the next
login, which is what a real device does.

```sh
ssh "$GUEST" 'launchctl load "/Library/LaunchAgents/ug-sso-bootstrap.plist"'
```

A browser must open in the guest. Sign in there. `ug` waits up to 300 seconds.

Read the log to confirm which path the script took:

```sh
ssh "$GUEST" 'cat ~/Library/Logs/ug-sso-bootstrap.log'
```

It must say `ug absent; installing with`, then
`not authenticated ... starting ug configure`. On a guest that already has `ug`, the
install line is absent.

> **Confirm the browser opens in the guest's session.** The plist sets
> `LimitLoadToSessionType` to `Aqua`, so the agent runs only in a GUI session. That
> is what makes the browser possible. Record whether `launchctl load` over SSH
> reaches that session. If it does not, log in to the guest GUI and let the agent
> fire at login instead. The answer decides nothing about the MDM payload, which
> always fires at login.

### Take 2 — the same device, second login

Run the script directly, so no browser can appear from a stale agent:

```sh
ssh "$GUEST" '"/Library/Application Support/ClaudeDesktop/ug-sso-bootstrap.sh"'
ssh "$GUEST" 'tail -1 ~/Library/Logs/ug-sso-bootstrap.log'
```

The log must say `already authenticated ... no browser needed`, and **no browser
must open**. This is the guard. Test it. An unguarded trigger opens a browser at
every login, and that is the failure most likely to spoil a recording.

### Confirm the state `ug` recorded

```sh
ssh "$GUEST" 'ug status'
```

### Reset authentication for another take

```sh
ssh "$GUEST" 'ug revert || true; rm -f ~/.databrickscfg ~/Library/Logs/ug-sso-bootstrap.log'
```

Reverting to the baseline snapshot also clears it, and clears everything else too.

## Step 7 — Verify the token path, headlessly

Run the credential helper the way Claude Desktop runs it. This proves the auth
chain without the app.

```sh
ssh "$GUEST" '"/Library/Application Support/ClaudeDesktop/databricks-token.sh" | wc -c'
```

The command must print a byte count near 800 and exit 0. An empty result means
the chain is broken.

Read the diagnostics separately. The helper writes them to standard error, so
they never corrupt the token.

```sh
ssh "$GUEST" '"/Library/Application Support/ClaudeDesktop/databricks-token.sh" >/dev/null'
```

Silence means success.

> **Never print the token itself to a terminal you record.** A bare token is a
> live credential. Count the bytes instead.

---

## Step 8 — Import the config and record the demo

This step needs the GUI, and it is the step the demo shows.

1. Copy the config to the guest:
   `scp "$BUNDLE"/claude-setup.json "$GUEST":~/Desktop/`.
2. Start Claude Desktop in the guest.
3. Open Help. Open Troubleshooting. Turn on Developer Mode.
4. Open Developer. Open Configure third-party inference.
5. Import `claude-setup.json`.
6. Test the connection.
7. Send a test message.

Capture a screenshot from the host at any point.

```sh
prlctl capture "$VM" --file ~/Desktop/take-01.png
```

---

## Step 9 — Confirm telemetry

Query the traces table after you send a test message. Run this on the host.

```sh
databricks --profile <profile> api post /api/2.0/sql/statements \
  --json '{"warehouse_id":"<id>","statement":"SELECT count(*) FROM <catalog>.<schema>.claude_otel_traces"}'
```

The count must grow after the test message. Telemetry batches, so wait one
minute before you query again.

---

## Step 10 — Reset for the next take

```sh
prlctl snapshot-switch "$VM" --id <clean-baseline-id>
prlctl suspend "$VM"
df -h /
```

Check free space after every take. Snapshots grow.

---

## Known unknowns

Record what you observe for these. Each one changes the guide.

1. **Does the MDM profile replace the imported config, or merge with it?** If the
   profile carries the whole config, an employee needs no import. If it only
   overlays policy, each machine needs both. Test this with the profile installed
   and no import.
2. **Can the app import be scripted?** If Claude Desktop stores the third-party
   inference config in a file, a script could place it. Look for the file after a
   manual import, and compare it against `claude-setup.json`.
3. **Does the Windows path work?** The PowerShell helper and the `.cmd` shim have
   never run on Windows. A Windows 11 guest exists on this host. Test them before
   you claim Windows support.
