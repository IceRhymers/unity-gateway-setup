# terraform/bootstrap — Runbook

This runbook covers the full lifecycle of the Lakebase Postgres backend for
`terraform/infra`.

---

## First Bootstrap

Run this procedure once, in one terminal, before any operator runs `make tf-init`.

**Preconditions:**

- A workspace admin created the workspace-level Databricks group `terraform_writers`.
- A workspace admin added you to that group.
- You have a Databricks CLI profile that authenticates to the correct workspace.

Steps:

1. Run `databricks auth login --profile <p>` to confirm authentication.
2. Run `make tf-bootstrap-state PROFILE=<p>`.
3. Run `make tf-bootstrap-state PROFILE=<p>` a second time.
4. Confirm the second run prints no changes and exits 0.
5. Run `make tf-init PROFILE=<p>`.
6. Run `make tf-plan PROFILE=<p>` to verify the backend connects.

The script prints the profile, project, endpoint host, database, schema, group role,
object owner, and token expiry time. It never prints the token.

---

## Onboarding a Second Operator

An existing group member performs steps 1 and 2. The new operator performs steps 3,
4, and 5.

1. A workspace admin adds the new operator to the workspace-level group `terraform_writers`.
2. An existing member runs `bootstrap-state.sh --grant-to <email> --profile <p>`.
3. The new operator runs `databricks auth login --profile <p>`.
4. The new operator runs `make tf-bootstrap-state ARGS=--env-only PROFILE=<p>`.
5. The new operator runs `make tf-init PROFILE=<p>`.

No credential is shared at any step. The Makefile default profile is `fevm-west`. If
your profile name is different, pass `PROFILE=<your-profile>` to every `make tf-*`
command.

---

## Day-to-Day Use

Run these commands in order for each change:

1. Run `make tf-snapshot PROFILE=<p>` before any high-risk change.
2. Run `make tf-plan PROFILE=<p>` to review the change.
3. Run `make tf-apply PROFILE=<p>` to apply the change.
4. Run `make tf-state-info PROFILE=<p>` after every apply.

The wrapper prints `token_expires=<time>` to stderr before each command. For applies
that may run for a long time, use the Saved-Plan Workflow below.

---

## Saved-Plan Workflow

Use this workflow when an apply may run for a long time.

The token TTL is capped at one hour. Expiry is enforced only at login. An open
connection survives past the expiry time. Using a saved plan lets you review the output
with no clock running. The apply step mints a fresh token at t=0.

1. Run `make tf-plan PROFILE=<p> ARGS="-out=tfplan"`.
2. Review the plan output.
3. Run `make tf-apply PROFILE=<p> ARGS="tfplan"`.
4. Delete `tfplan` after the apply completes.

The plan file contains state data. Delete it promptly.

---

## Stuck-Lock Recovery

`terraform force-unlock` does not work with the `pg` backend.

1. Retry the command first. The lock is session-scoped. If the Terraform process is
   gone, its session is gone and the lock is released.
2. Run this query to find the lock holder:

```sql
SELECT l.objid, a.pid, a.usename, a.state, a.backend_start, a.state_change, a.query
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype = 'advisory';
```

3. Confirm the row shows `state='idle'` and an old `state_change` timestamp.
4. Run `SELECT pg_terminate_backend(<pid>);` to end the session.

Whether the operator role holds `pg_signal_backend` (required for `pg_terminate_backend`)
is unverified. See L4a.

Alternatively, wait for scale-to-zero. Scale-to-zero ends every session. It releases
every advisory lock.

**Never delete rows to clear a lock.** The row in `tfstate_infra.states` is your
Terraform state. Deleting the row destroys the state.

`pg_try_advisory_lock(-1)` is a short creation sentinel. If it appears stuck, a first
`init` call ended mid-flight. Follow the same four steps above.

---

## Rollback

### Primary Path — Credential-Free

This path works even when Lakebase is unreachable. Run step 1 while the backend still
responds.

1. Run `make tf-state-backup PROFILE=<p>`.
2. Run `mv terraform/infra/backend.tf terraform/infra/backend.tf.rollback`.
3. Run `rm -rf terraform/infra/.terraform`.
4. Run `terraform -chdir=terraform/infra init -input=false`.
5. Run `terraform -chdir=terraform/infra state push state-backup-<UTC>.tfstate`.
6. Run `terraform -chdir=terraform/infra state list`.
7. Confirm the output matches the pre-rollback list.

Do not use `git stash` in place of `mv`. Every worktree shares one stash stack. A
`git stash pop` in another worktree applies this deletion to that worktree.

To reconnect the remote backend later, run `mv terraform/infra/backend.tf.rollback terraform/infra/backend.tf`.

### Alternative Path — True Migration (Requires Credentials)

Do not use `-reconfigure`. It discards the backend association. It does not migrate the
state. The remote state becomes orphaned.

`-migrate-state` reads the remote state. It requires a live database connection.

1. Run `make tf-state-backup PROFILE=<p>`.
2. Run `eval "$(terraform/bootstrap/lakebase-env.sh --print --profile <p>)"`. This
   command emits a live credential. Use it for this command only.
3. Run `mv terraform/infra/backend.tf terraform/infra/backend.tf.rollback`. Leave
   `.lakebase.env` in place.
4. Run `terraform -chdir=terraform/infra init -migrate-state -force-copy`.
5. Run `unset PGPASSWORD PGHOST PGPORT PGUSER PGDATABASE PGSSLMODE`.
6. Run `terraform -chdir=terraform/infra state list`.

`state push` into an existing remote whose serial number is at your value or higher
requires `-force`. Record the serial number and what `-force` overwrites before you run it.

---

## Snapshot Restore — UNVERIFIED

**This procedure is UNVERIFIED. L7a has not passed.** Do not use it as your primary
recovery path. Use the primary rollback path above until L7a passes end to end.

This procedure restores state when the state data is wrong, not when it is lost.

1. Identify the `presnap-<ts>` branch to restore from.
2. Provision a Postgres role and credential against that branch. Each branch requires its
   own role creation step.
3. Connect to the restore branch.
4. Run `SELECT data FROM tfstate_infra.states WHERE name='default';`.
5. Push the data back into the `production` branch with `terraform state push`. The
   existing remote has a higher serial number. Use `-force`. Record exactly what
   `-force` overwrites before you run it.
6. Confirm the state is correct with `terraform state list`.
7. Delete the restore branch.

A restore branch consumes one of the 10 unarchived-branch slots.

---

## Live-Validation Checklist

Run this checklist on a live workspace after first deploy. Run L1a first. Its result
can change how subsequent steps work.

---

**L1a.**

Determine whether Lakebase creates a Postgres role automatically on the first OAuth
login. Record the result. The result determines whether `--grant-to` must run
`create-role` or only issue `GRANT`.

---

**L1b.**

Run `psql "sslmode=verify-full sslrootcert=system"` against the state endpoint.
`sslrootcert=system` requires libpq 16. The local `psql` client is v14. Record what
actually happens. Do not write expected output before you observe it. If the command
succeeds, update the default `PGSSLMODE` in `.lakebase.env` to `verify-full`.

---

**L1.**

Run `make tf-bootstrap-state PROFILE=<p>` twice. Confirm the second run makes no
change and exits 0.

---

**L2.**

Confirm `terraform_writers` owns `tfstate_infra` and `tfstate_infra.states`.
Run `SELECT tableowner FROM pg_tables WHERE tablename='states';` to confirm.

---

**L3.**

1. Run `make tf-init PROFILE=<p>`, `make tf-plan PROFILE=<p>`, and `make tf-apply PROFILE=<p>`.
2. Run `SELECT id, name, length(data) FROM tfstate_infra.states;`.
3. Confirm the query returns a row.

---

**L3a.**

Force a state-persist failure. Record the actual error text. Record the actual name of
any recovery file that Terraform writes. Do not write expected strings before you observe
them. Update this runbook with the observed text after you complete the test.

---

**L4.**

Test lock contention with two different operator identities.

1. In Terminal 1, run `make tf-apply PROFILE=<p>`.
2. Hold Terminal 1 at the confirmation prompt.
3. In Terminal 2, run `make tf-plan PROFILE=<p>`.
4. Confirm Terminal 2 blocks or reports a lock error.
5. Run `SELECT locktype, objid, pid FROM pg_locks WHERE locktype='advisory';` to confirm the lock exists.

---

**L4a.**

Confirm the operator role holds the `pg_signal_backend` permission.
`pg_terminate_backend` requires this permission. Stuck-lock recovery requires it.

---

**L5.**

Attempt an OAuth connection to `read_write_pooled_host`. Record the actual response
text. Do not write expected text before you observe it.

---

**L6.**

1. Lower the inactivity timeout to trigger scale-to-zero.
2. Let the database suspend.
3. Run `make tf-plan PROFILE=<p>`.
4. Record what the client sees on the first connection attempt. The cold-connect error
   is not documented. Do not invent it.

---

**L6a.**

Record the observed behavior of the retry policy. The policy runs 3 attempts with
2-second then 5-second backoff. Base the retry count on attempts, not on error-string
matching.

---

**L7.**

1. Run `make tf-snapshot PROFILE=<p>`.
2. Confirm the branch exists.
3. Confirm pruning occurs when the unarchived branch count reaches 10.
4. Confirm the TTL is set to 7 days.

---

**L7a.**

Run the snapshot restore procedure end to end. Follow the steps in the Snapshot Restore
section above. Until this test passes, the Snapshot Restore procedure stays marked
UNVERIFIED.

---

**L8.**

1. Run `make agents PROFILE=<p>`.
2. Compare the output to the step-0 baseline.
3. Confirm the output is byte-identical to the baseline.
