# Jamf runbook — Unity AI Gateway agent configs (macOS fleet)

This runbook is the single Jamf reference for every agent this repository deploys.
It covers Claude Code, Codex, and Claude Desktop.

The other runbooks describe what the generator produces. This one describes how Jamf
delivers it. `claude-desktop-mdm.md` covers the Claude Desktop rollout in full and
points here for the Jamf steps.

This runbook assumes a working Jamf instance. If you have never set one up, read
`jamf-setup-checklist.md` first. It covers the instance setup, the APNs certificate,
the VM enrolment, and what you can skip.

---

## Certificates and signing: what you need, and what you do not

Read this first. It is the question operators ask most often, and the answer is
shorter than expected.

| Artifact | Signature needed for Jamf? | Why |
|---|---|---|
| `.pkg` installer | **No** | Jamf installs it as root. Gatekeeper does not apply. |
| Script policy body | **No** | Jamf runs it as root with no code-signing check. |
| `.mobileconfig` profile | **No** | Jamf signs the profiles it serves. The MDM channel is authenticated. |

**A self-signed certificate does not help.** Gatekeeper trusts only a
Developer ID Installer certificate, which Apple issues to Apple Developer Program
members. A self-signed certificate stays untrusted, so it adds nothing over an
unsigned package.

Sign a package only when a person installs it by hand. Gatekeeper blocks an
unsigned double-click install. Then you need a real Developer ID Installer
certificate, and Apple notarization as well.

```sh
# Only with a real Developer ID Installer certificate:
make claude-desktop-pkg PROFILE=<profile> \
  PKG_SIGN_ID="Developer ID Installer: <org> (<team-id>)"
```

Check whether you hold one:

```sh
security find-identity -v | grep "Developer ID Installer"
```

### The certificates Jamf itself needs

These belong to the Jamf instance, not to this repository. An admin configures them
once.

1. **An APNs certificate.** Every MDM server needs one to push to devices. You
   generate a certificate request, Jamf signs it as an MDM vendor, and you upload it
   to the Apple Push Certificates Portal. It expires every year. Renew it.
2. **A TLS certificate.** Jamf Cloud provides its own. A self-hosted Jamf Pro server
   needs a certificate that every managed device trusts. This is the one place where
   a self-signed certificate is a real option, and every device must then trust it.
3. **An Automated Device Enrollment token.** This is optional, and it needs Apple
   Business Manager.

---

## Deployment phases

Each phase has a hard boundary. No phase is optional.

| Phase | Who | What | Can Jamf push it? |
|---|---|---|---|
| **A — Config placement** | Jamf admin | Place the managed files | **Yes** |
| **B — Settings** | Jamf admin | Apply the Claude Desktop profile | **Yes**, Claude Desktop only |
| **C — User auth** | Each developer | Sign in through the browser | **The trigger, yes.** The sign-in is the person's |

Phase A places files. Phase B applies settings. Phase C binds each developer's
Databricks identity.

---

## Which Jamf object carries which artifact

Jamf has one object per job. Match them correctly, or the deployment fails in ways
that are hard to read.

| Artifact | Jamf object | Agents |
|---|---|---|
| `coding-agents-<version>.pkg` | **Package**, run by a Policy | Claude Code, Codex |
| `claude-desktop-<version>.pkg` | **Package**, run by a Policy | Claude Desktop |
| `ug-bootstrap-<version>.pkg` | **Package**, run by a Policy | The `ug` login trigger |
| `.mobileconfig` | **Configuration Profile** | Claude Desktop |
| `.tar.gz` bundle | **Script** policy that unpacks it | Claude Code, Codex (fallback) |

The three packages version independently. So you can stage or roll back one piece
without touching the others. `ug-bootstrap` carries the tool rather than any agent's
config, which is why it is separate.

**You do not deploy `ug` itself.** The `ug-bootstrap.pkg` LaunchAgent installs `ug`
for each user at their first login. A package script runs as root, so it cannot do a
per-user install. See Step 5.

**You do deploy `uv`.** It is a prerequisite, because it also installs per-user and
self-updates. Deploy it the way you deploy `databricks` and `python3`.

The tarball is the fallback. Prefer a package: Jamf then records a receipt, and it
reports the install state.

A Configuration Profile carries settings only. It cannot place a file, and it cannot
run a command. So it never installs a helper script.

A Package places files and runs an install script. Prefer it when a package exists.

---

## Prerequisites

`install.sh` checks prerequisites and reports them. It installs nothing. IT owns
these as part of the macOS baseline.

| Tool | Criticality |
|---|---|
| `databricks` | Always critical. The Claude Code and Codex auth helpers call it. |
| `python3` | Critical for the Claude Code and Codex auth helpers, and for the OTEL helper. |
| `ug` | Critical for Claude Desktop. `ug-bootstrap.pkg` installs it at first login, so IT does not deploy it. |
| `uv` | Critical, because the bootstrap installs `ug` with it. Deploy it as its own Package or Policy. |
| `jq` | Critical only when hook-event telemetry is on. |
| `curl` | Critical only when hook-event telemetry is on. |

If a critical prerequisite is absent, `install.sh` exits 3. Jamf then marks the
policy failed. Confirm each tool is present for every session type before you scope
the policy.

You do not deploy `ug` through Jamf. `ug-bootstrap.pkg` places `uv`, and its
LaunchAgent installs `ug` per user at first login. A Jamf policy script runs as root,
so it cannot install a per-user tool without dropping privileges, and at imaging time
there is no user to drop to.

A failed install is not fatal. The script logs it and the next login retries.

> **Exception:** a `DATABRICKS_BEARER`-only deployment can omit `databricks` and
> `python3`. Every developer then sets `DATABRICKS_BEARER` in their environment, and
> the CLI never refreshes a token. This deployment is unusual, and it is not the
> default.

---

## Step 1 — Build the artifacts

On an admin workstation with the repository checked out.

### For Claude Code and Codex, an installer package

```sh
make agent-claude-code PROFILE=<profile>
make agent-codex       PROFILE=<profile>
make coding-agents-pkg PROFILE=<profile>
```

This writes `dist/coding-agents-<version>.pkg`. It places six files.

| Payload path | Mode |
|---|---|
| `/Library/Application Support/ClaudeCode/managed-settings.json` | 644 |
| `/Library/Application Support/ClaudeCode/otel-headers-helper.sh` | 755 |
| `/Library/Application Support/ClaudeCode/emit_hook_events.sh` | 755 |
| `/etc/codex/managed_config.toml` | 644 |
| `/etc/codex/requirements.toml` | 644 |
| `/etc/codex/emit_hook_events.sh` | 755 |

The two helper scripts appear only when telemetry or hook events are on. The package
has no postinstall, because neither agent runs a daemon. Claude Code reads its
managed file at the next launch, and Codex reads its file at the next run.

Pass `--skip-codex` or `--skip-claude-code` through `ARGS` to place one agent only.

> **Codex must be in managed mode.** `install.sh` silently skips a user-mode Codex
> bundle, so a package built from one would deploy nothing and still exit 0. The
> builder refuses that bundle instead, and exits 4. Regenerate with `make agent-codex`.

### For Claude Code and Codex, a tarball (fallback)

```sh
make deploy-package
```

This writes `dist/unity-gateway-agents-<version>-macos.tar.gz`. The tarball holds the
bundle files, `install.sh`, the runbooks, and a `VERSION` file. The target machine
needs no network access.

Use it when you want `install.sh` to do the placement, such as for a Linux fleet or a
container. For a macOS fleet, prefer the package.

### For Claude Desktop, an installer package

```sh
make agent-claude-desktop PROFILE=<profile>
make claude-desktop-pkg   PROFILE=<profile>
```

This writes `dist/claude-desktop-<version>.pkg`. It places four files.

| Payload path | Mode |
|---|---|
| `/Library/Application Support/ClaudeDesktop/databricks-token.sh` | 755 |
| `/Library/Application Support/ClaudeDesktop/otel-headers-helper.sh` | 755 |
| `/Library/Application Support/ClaudeDesktop/ug-sso-bootstrap.sh` | 755 |
| `/Library/LaunchAgents/ug-sso-bootstrap.plist` | 644 |

Its postinstall loads the LaunchAgent for the user who is logged in. So the browser
sign-in starts as soon as the package lands. At imaging time there is no console
user, and the agent loads at the first real login instead.

Upload each artifact to Jamf. Upload both `.pkg` files as Packages. Upload a tarball,
if you use one, to a distribution point.

---

## Step 2a — A Policy for each package

Create one Policy per package. Separate policies keep the two versions and their
scopes independent.

1. Open Computers. Open Management. Open Packages. Upload the `.pkg`.
2. Create a Policy. Add a Packages payload. Select the package.
3. Set the Trigger to Recurring Check-in.
4. Set the Execution Frequency to Once per computer.
5. Scope it. See Step 4.

Repeat for the second package.

| Package | Places |
|---|---|
| `coding-agents-<version>.pkg` | The Claude Code and Codex managed configs |
| `claude-desktop-<version>.pkg` | The Claude Desktop helper scripts |
| `ug-bootstrap-<version>.pkg` | The SSO bootstrap and its LaunchAgent |

No script is needed. Jamf installs a package as root.

The three packages write no file in common. They share only the parent directories
`/Library/Application Support` and `/Library/Application Support/ClaudeDesktop`, so
none clobbers another. The install order between them does not matter.

---

## Step 2b — A Script policy for the tarball (fallback)

Use this only when you deploy the tarball instead of the package. A tarball needs a
script, because Jamf cannot unpack one on its own.

Open Computers. Open Management. Open Scripts. Create a new script with this body.

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
echo "Each developer must complete Phase C — see the Self Service item."
```

> **Note on `--target-root`:** the script above uses real system paths. That is
> correct for a fleet push. `--target-root` is for unprivileged staging and unit
> tests only.

Attach the script to a Policy, and scope it. Set the Execution Frequency to Once per
computer for the first rollout. Change it when the config version changes.

`install.sh` handles every agent, Claude Desktop included. Prefer the packages. A
package gives Jamf a receipt and an install state to report.

---

## Step 3 — A Configuration Profile for Claude Desktop settings

The Claude Desktop app exports this profile. The generator does not produce it. See
`claude-desktop-mdm.md` section 6 for the export.

1. Open Computers. Open Configuration Profiles. Upload the exported
   `.mobileconfig`.
2. Scope it to the same group as the package Policy.

**Let the package Policy run first.** The profile names the credential helper by
absolute path. A profile that arrives first points at a file that does not exist yet.
Claude Desktop then reports an authentication failure with no obvious cause.

The profile writes to the `com.anthropic.claudefordesktop` preference domain. Confirm
it applied on a device:

```sh
ls /Library/Managed\ Preferences/ | grep -i anthropic
```

An empty result means no profile is applied.

---

## Exit code reference

`install.sh` returns a structured exit code. Jamf marks the policy failed on any
non-zero code.

| Code | Meaning |
|---|---|
| 0 | Success (or `--dry-run` / `--uninstall` with all files removed) |
| 1 | Usage error |
| 2 | Not root and no `--target-root` set |
| 3 | Critical prereq missing |
| 4 | Required source file missing (`managed-settings.json`) |
| 5 | Copy or permission failure |
| 6 | Uninstall failure. A file or the marker could not be removed. The marker is left intact for retry. |

---

## Unsigned-payload caveat

Jamf runs a script policy as **root with no notarization or code-signing check**. The
same is true of a package it installs. Treat both accordingly.

- Store the tarball and the package under access controls on the distribution point.
- Check the SHA-256 of the tarball before you upload it. CI writes a checksum beside
  the tarball in `dist/`.
- `install.sh` is POSIX `sh`. Review it before a new macOS major version.
- IT owns the prerequisites as a managed baseline. `install.sh` only reports them.

---

## Step 4 — Scoping

A suggested scope for the Phase-A policy.

- **Targets:** a Smart Group on macOS version, plus the managed `databricks` CLI.
  Confirm the baseline is present before you target machines.
- **Exclusions:** machines already at the target version. Check
  `/Library/Application Support/ClaudeCode/.unity-gateway-version`, or rely on the
  idempotent re-copy in `install.sh`.
- **Trigger:** Check-in and Enrollment Complete, or a manual trigger for the first
  rollout.

Scope the Claude Desktop package and its Configuration Profile to the same group.

---

## Step 5 — Phase C: user authentication

Each developer authenticates once. A browser opens for single sign-on. Jamf cannot
push the sign-in itself, because the identity belongs to the person.

The command differs by agent, because the two auth paths differ.

### Claude Code and Codex

Their auth helpers call the Databricks CLI. So the developer runs the CLI login.
Create a Self Service item with this text.

```
Config placement is complete. To finish connecting your tools to the
AI Gateway, run this command ONCE in your terminal:

    databricks auth login --host <host> --profile fevm-west

Your browser will open for Single Sign-On. After login, verify with:
  - Claude Code:  type /status in a conversation
  - Codex:        run  codex --strict-config doctor

You only need to do this once per machine.
```

Replace `<host>` with your workspace URL.

### Claude Desktop

Its credential helper calls `ug auth-token`, so the login goes through `ug`. **Jamf
does not need a Self Service item for this.** `ug-bootstrap.pkg` installs a LaunchAgent
that runs at each user login, and the agent guards itself so a browser opens only when
the developer is not already authenticated.

At the first login the agent does two things, in order:

1. It installs `ug` with `uv`, when `ug` is absent.
2. It runs `ug configure`, which opens the browser for single sign-on.

The developer signs in to the browser. They type nothing.

A developer who dismisses the browser gets another one at the next login. So the flow
recovers on its own. See `claude-desktop-mdm.md` section 8.

To confirm on a device, read the log the bootstrap writes:

```sh
cat ~/Library/Logs/ug-sso-bootstrap.log
```

---

## Testing: two constraints worth knowing early

**A Mac holds one MDM enrollment.** A machine already enrolled in a corporate Jamf
instance cannot also enroll in a test instance. So test in a VM, not on your own
machine. See `claude-desktop-vm-test.md`.

**Do not test against a corporate Jamf instance.** It manages real devices, and a
mis-scoped profile reaches them. Use an instance you own, or ask the owning team to
scope a test profile to one device.

**A VM cannot use Automated Device Enrollment.** Enrollment needs an Apple Business
Manager record, and a VM has none. So enroll a VM by hand: install the enrollment
profile, and approve it once. Every profile push after that is silent.

---

## Uninstall

### The Claude Desktop package

`install.sh --uninstall` removes the helper scripts and the LaunchAgent. It unloads
the agent first.

```sh
sh install.sh --agents claude-desktop --os macos --uninstall
```

Remove the Configuration Profile through Jamf. Removing the package does not remove
the profile.

### The tarball agents

Create a separate Script policy. The uninstall needs only `--os` and the real system
paths. It does not need `--source`.

```sh
#!/bin/sh
set -eu

# Jamf passes the tarball path as parameter 4. Set it in the policy script parameters.
PAYLOAD="${4:?set the package path in Jamf parameter 4}"
WORK_DIR="/tmp/unity-gateway-agents-install"

# Unpack the tarball to get install.sh (the same tarball used for installation).
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
cp "$PAYLOAD" "$WORK_DIR/"
tar -xzf "$WORK_DIR/unity-gateway-agents.tar.gz" -C "$WORK_DIR" --strip-components=1

cd "$WORK_DIR"
# Run the uninstall. Capture the exit code before cleanup.
# An `if` guard is required: under set -e, a non-zero exit terminates the script
# before EXIT_CODE is assigned, so cleanup and the error message never run.
if ./install.sh --uninstall; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi

rm -rf "$WORK_DIR"

if [ "$EXIT_CODE" -ne 0 ]; then
  echo "install.sh --uninstall exited $EXIT_CODE — policy failed" >&2
  exit "$EXIT_CODE"
fi

echo "Unity AI Gateway agent configs removed (Phase A reversed)."
```

Notes on uninstall behaviour.

- `install.sh --uninstall` reads the `files=` list from the version marker. It
  removes only files the installer placed.
- Files outside the marker are not removed. That includes user files and `.bak-*`
  backups.
- The script removes the install directory when it is empty. It leaves a non-empty
  directory in place, and warns.
- Exit 6 means a file could not be removed. The marker stays intact. Run the policy
  again to retry.
- `--uninstall` never removes a `.bak-*` file. Remove those by hand.
- To undo what `ug` wrote on a device, run `ug revert`.
