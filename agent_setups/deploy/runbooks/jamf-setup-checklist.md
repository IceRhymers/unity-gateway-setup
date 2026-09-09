# Jamf setup checklist — first time, for a VM demo

A personal setup list for an operator who has not used Jamf Pro before. The goal is
one recorded demo: your laptop drives the Jamf console, and a Parallels VM receives
the deployment.

`jamf.md` is the operator runbook. It assumes a working Jamf instance. This checklist
gets you to that point, and it says which parts you can skip.

---

## First decide: do you need Jamf for this demo?

Two paths prove different things. Read this before you spend a day on Jamf.

| Question you want to answer | Manual install | Jamf |
|---|---|---|
| Is the config content correct? | **Yes** | Yes |
| Does Claude Desktop route to the gateway? | **Yes** | Yes |
| Does the credential helper mint a token? | **Yes** | Yes |
| Does the SSO LaunchAgent fire and guard itself? | **Yes** | Yes |
| Does a push reach a device with no user action? | No | **Yes** |
| Does scoping target the right machines? | No | **Yes** |
| Can IT remove it centrally? | No | **Yes** |
| **Can the developer remove the profile?** | **Yes, in System Settings** | No |

A manually installed profile still lands in `/Library/Managed Preferences`, and the
app reads it the same way. So a manual install validates the payload completely. It
does not validate distribution.

It also does not validate **enforcement**. A user with admin rights can remove a
manually installed profile in System Settings. A user cannot remove an MDM-delivered
profile. That matters here, because `disableClaudeAiSignIn` and `allowedEgressHosts`
exist to stop a developer bypassing the gateway. Under a manual install they are
advisory. Under an MDM they are enforced.

Test the removal yourself in the VM, and record the result. It is the claim a security
reviewer presses on.

**Do both, in this order.**

1. **Manual first.** It takes about 15 minutes and needs no Jamf. It catches every
   content error: a wrong path, a missing model, a broken helper. Fix those here.
2. **Jamf second.** Now the payload is known good, so any failure is a Jamf failure.

Do not start with Jamf. A first Jamf attempt against an untested payload makes you
debug two systems at once.

---

## Path A — Manual, no Jamf (start here)

This is the whole loop. It is fully headless apart from the profile approval.

1. Build the artifacts on your laptop. Three packages, one profile.

```sh
make agent-claude-code    PROFILE=<profile>
make agent-codex          PROFILE=<profile>
make agent-claude-desktop PROFILE=<profile>
make coding-agents-pkg    PROFILE=<profile>
make claude-desktop-pkg   PROFILE=<profile>
make ug-bootstrap-pkg     PROFILE=<profile>
```

2. Copy all three packages to the VM, and install them over SSH. The order does not
   matter, because they write no file in common.

```sh
for p in $(ls -t dist/coding-agents-*.pkg  | head -1) \
         $(ls -t dist/claude-desktop-*.pkg | head -1) \
         $(ls -t dist/ug-bootstrap-*.pkg   | head -1); do
  scp "$p" "$GUEST":/tmp/pkg.pkg
  ssh "$GUEST" 'sudo installer -pkg /tmp/pkg.pkg -target / && echo INSTALL_OK'
done
```

   You do not install `ug` yourself. `ug-bootstrap.pkg` places `uv`, and its
   LaunchAgent installs `ug` for the logged-in user at first login.

3. Let the first login run. The agent installs `ug`, then opens the browser for SSO.
4. Import `claude-setup.json` in the app, then export the `.mobileconfig`.
5. Copy the `.mobileconfig` to the VM. Double-click it. Approve it in System
   Settings, under General, under Device Management.
6. Verify.

`claude-desktop-vm-test.md` has the full version, with the snapshot and reset steps.

**What to record from this path:** the headless package install, the browser sign-in
appearing on its own, and Claude Desktop answering through the gateway.

---

## Path B — Jamf Pro

Only continue when Path A works.

### Step 1 — Get an instance (the slow part)

Jamf Pro is **not self-serve**. You cannot sign up and start in ten minutes. Pick one.

| Option | Notes |
|---|---|
| Ask Databricks IT for a sandbox instance | Fastest inside the company. Ask for a non-production instance. |
| Request a Jamf Pro trial | Goes through Jamf sales. Expect a lead time. |

**Do not use `jamf.corp.databricks.com`.** It manages real employee devices. A
mis-scoped profile reaches them.

Plan for this step to take days, not hours. Start it before you plan the recording.

### Step 2 — Set up the instance

An admin does this once. Budget an hour.

1. **Create your admin account**, or get one. You need rights to create Packages,
   Policies, and Configuration Profiles.
2. **Add the APNs certificate.** This is required. No MDM can push to a device
   without it. The flow is:
   - In Jamf Pro, open Settings, then Global, then Push Certificates.
   - Download the certificate request that Jamf signs for you.
   - Upload it to the Apple Push Certificates Portal at
     `identity.apple.com`, and sign in with an Apple Account your team controls.
   - Download the certificate Apple returns, and upload it to Jamf Pro.
   - Record the expiry date. It expires every year, and every device unenrolls when
     it lapses.
3. **Skip Apple Business Manager.** It is only needed for Automated Device
   Enrollment, and a VM cannot use that. See Step 3.
4. **Skip the TLS certificate** when you use Jamf Cloud. Jamf provides it. Only a
   self-hosted server needs your own.

### Step 3 — Enrol the VM

A VM has no Apple Business Manager record. So Automated Device Enrollment is not
available, and you enrol by hand. This costs one GUI approval, once per snapshot.

1. Take a clean Parallels snapshot first. You will want to return to it.
2. In the VM, open a browser at `https://<your-instance>.jamfcloud.com/enroll`.
3. Sign in, and follow the pages. Download the profile.
4. Open System Settings. Open General. Open Device Management. Approve the profile.
5. Confirm the enrolment from your laptop.

```sh
ssh "$GUEST" 'profiles status -type=enrollment'
```

Expect `MDM enrollment: Yes`.

6. Take a second snapshot now, named `enrolled-clean`. Every later take starts here,
   so you never repeat the enrolment.

### Step 4 — Upload the two artifacts

Jamf has one object per artifact. Do not mix them.

1. Upload the `.pkg`. Open Computers, then Management, then Packages.
2. Upload the `.mobileconfig`. Open Computers, then Configuration Profiles.

A Configuration Profile cannot install a package. See `jamf.md` for why.

### Step 5 — Create the Policy for the package

1. Open Computers, then Policies. Create a new policy.
2. Add a **Packages** payload. Select your `.pkg`.
3. Set the Trigger to **Recurring Check-in**.
4. Set the Execution Frequency to **Once per computer**.
5. Scope it. See the next step.

### Step 6 — Scope to the VM only

This is the step to get right. A wrong scope reaches machines you did not intend.

1. Open the Scope tab.
2. Set the target to your VM by computer name, **not** to All Computers.
3. Do the same for the Configuration Profile.

Check the scope twice before you save. Then confirm the member count is 1.

### Step 7 — Trigger it now, instead of waiting

Jamf checks in on its own schedule, which is too slow for a test loop. Force it from
the VM.

```sh
ssh "$GUEST" 'sudo jamf policy'      # run pending policies now
ssh "$GUEST" 'sudo jamf recon'       # update inventory now
```

`sudo jamf policy` is the command that makes a Jamf test loop usable. Learn it first.

### Step 8 — Verify on the device

```sh
# The package landed.
ssh "$GUEST" 'ls -l "/Library/Application Support/ClaudeDesktop" /Library/LaunchAgents/ug-sso-bootstrap.plist'

# The profile applied.
ssh "$GUEST" 'profiles list -type=configuration'
ssh "$GUEST" 'ls /Library/Managed\ Preferences/ | grep -i anthropic'

# The token path works. Count the bytes. Never print the token.
ssh "$GUEST" '"/Library/Application Support/ClaudeDesktop/databricks-token.sh" | wc -c'

# What the SSO bootstrap did.
ssh "$GUEST" 'cat ~/Library/Logs/ug-sso-bootstrap.log'
```

---

## What you can skip

New Jamf users often set these up and do not need them here.

| Thing | Skip it? | Why |
|---|---|---|
| Apple Business Manager | **Yes** | A VM has no record in it. |
| Automated Device Enrollment | **Yes** | Needs Apple Business Manager. |
| A code-signing certificate | **Yes** | Jamf installs as root. Gatekeeper does not apply. |
| Notarization | **Yes** | Same reason. |
| A TLS certificate | **Yes**, on Jamf Cloud | Jamf provides it. |
| Self Service | **Yes**, for Claude Desktop | The LaunchAgent triggers the login. |
| Smart Groups | **Yes**, for one VM | Scope to the computer directly. |

---

## Time to budget

| Step | Time |
|---|---|
| Path A, the whole manual loop | About 15 minutes |
| Get a Jamf instance | **Days.** Start early. |
| Jamf instance setup, mostly APNs | About 1 hour |
| Enrol the VM | About 10 minutes |
| Upload, policy, and scope | About 20 minutes |
| Each later take, from the enrolled snapshot | About 5 minutes |

---

## Traps

1. **Scoping to All Computers.** Always scope to the one VM.
2. **Testing on the corporate instance.** It manages real devices.
3. **Enrolling your laptop.** A Mac holds one MDM enrollment, and yours is already
   enrolled in corporate Jamf. Check with `profiles status -type=enrollment`.
4. **Forgetting the APNs certificate.** Nothing pushes without it, and the failure is
   silent rather than loud.
5. **Waiting for a check-in.** Run `sudo jamf policy`.
6. **Letting the profile arrive before the package.** The profile names the credential
   helper by absolute path. Claude Desktop then reports an authentication failure with
   no obvious cause.
7. **No snapshot before enrolment.** Then every take repeats the enrolment.
8. **A full host disk.** Check `df -h /` before each take. Snapshots grow.
