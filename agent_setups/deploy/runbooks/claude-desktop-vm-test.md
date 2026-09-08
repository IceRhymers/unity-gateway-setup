# Runbook — test the Claude Desktop MDM rollout in a Parallels VM

This runbook drives a macOS guest from the host to test the Claude Desktop
rollout. It exists to rehearse and record a demo. Follow it in order.

**Audience.** The operator who records the demo. It assumes you built a bundle
already. See `claude-desktop.md` for the generator and `claude-desktop-mdm.md`
for the fleet deployment itself.

---

## What this environment can and cannot do headlessly

Read this section first. It decides how much of the demo you script.

| Step | Headless? | How |
|---|---|---|
| Start, suspend, and stop the guest | Yes | `prlctl` |
| Snapshot and revert between takes | Yes | `prlctl snapshot` |
| Copy the helper scripts to the guest | Yes | `scp` over SSH |
| Install `ug` | Yes | `ssh` |
| Authenticate `ug` | Yes | `ug configure --use-pat` |
| Verify the token and the routing | Yes | `ssh` |
| Capture a screenshot | Yes | `prlctl capture` |
| **Install the `.mobileconfig`** | **No** | GUI pane, or a real MDM channel |
| **Import `claude-setup.json` into the app** | **No** | GUI flow in the app |

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
4. Create a Databricks personal access token for the headless login. Store it
   somewhere you can read from the host.

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

## Step 3 — Deploy the helper scripts

The generated config names the helper scripts by absolute path. So place them at
the exact path the config expects. The default is
`/Library/Application Support/ClaudeDesktop`.

Generate the bundle on the host first.

```sh
make agent-claude-desktop PROFILE=<profile>
```

Copy the helpers to the guest, then place them as root.

```sh
BUNDLE=agent_setups/generated/claude-desktop/macos
DEST="/Library/Application Support/ClaudeDesktop"

scp "$BUNDLE"/databricks-token.sh "$BUNDLE"/otel-headers-helper.sh "$GUEST":/tmp/
ssh "$GUEST" "sudo mkdir -p '$DEST' \
  && sudo cp /tmp/databricks-token.sh /tmp/otel-headers-helper.sh '$DEST'/ \
  && sudo chmod 755 '$DEST'/databricks-token.sh '$DEST'/otel-headers-helper.sh \
  && ls -l '$DEST'"
```

Confirm the path in the config matches the path on the guest.

```sh
python3 -c "import json;print(json.load(open('$BUNDLE/claude-setup.json'))['inference']['credential']['command'])"
```

The two paths must be identical. If they differ, generate the bundle again with
`--install-dir-macos "<path>"`.

---

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

## Step 6 — Authenticate `ug` headlessly

`ug configure` opens a browser by default, which a headless run cannot use. Use a
personal access token instead.

1. Write the token into the guest's Databricks configuration file.

```sh
ssh "$GUEST" "cat > ~/.databrickscfg <<'CFG'
[<profile>]
host  = https://<workspace-host>
token = <personal-access-token>
CFG
chmod 600 ~/.databrickscfg"
```

2. Configure `ug` against that profile, with no browser.

```sh
ssh "$GUEST" 'ug configure --profiles <profile> --use-pat \
  --agents claude --skip-validate --skip-upgrade --verbose low'
```

3. Confirm the state `ug` recorded.

```sh
ssh "$GUEST" 'ug status'
```

> Treat the token as a secret. Delete `~/.databrickscfg` from the guest after the
> demo, or revert to the baseline snapshot.

---

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
