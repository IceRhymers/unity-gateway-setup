# Ansible runbook — Unity AI Gateway agent configs (Linux/servers)

## Two-phase deployment model

| Phase | Who | What | Automatable via Ansible? |
|---|---|---|---|
| **A — Config placement** | Ansible (root) | Copy tarball, unpack, run `install.sh` | **Yes** — this playbook snippet |
| **B — User auth** | Each developer | `databricks auth login --host <host> --profile <profile>` | **No** — browser OAuth (U2M); must be interactive, out-of-band |

Phase B cannot be driven by Ansible. After Phase A completes, communicate the Phase-B
instructions to developers separately (internal wiki, Slack, onboarding docs).

---

## Reference playbook snippet

This is a thin-glue snippet, not a full role. Add it to the appropriate play in your
inventory. It assumes the tarball has been built via `make deploy-package` and staged
somewhere reachable by the control node (local path, S3, artifact store — adapt
`local_tarball_path` and the copy task accordingly).

```yaml
---
# Unity AI Gateway — agent config placement (Phase A)
# Phase B (databricks auth login) is per-user/interactive — NOT handled here.

- name: Deploy Unity AI Gateway agent configs
  hosts: developer_machines          # adjust to your inventory group
  become: true                       # install.sh requires root

  vars:
    unity_gateway_tarball_local: "dist/unity-gateway-agents-{{ unity_gateway_version }}-linux.tar.gz"
    unity_gateway_work_dir: "/tmp/unity-gateway-agents-install"

  pre_tasks:
    # Fail fast with a clear message rather than letting install.sh exit 3
    # after the tarball has already been copied.
    - name: Assert required tools are present
      ansible.builtin.assert:
        that:
          - "lookup('ansible.builtin.pipe', 'command -v databricks', errors='ignore') != ''"
          - "lookup('ansible.builtin.pipe', 'command -v python3', errors='ignore') != ''"
        fail_msg: >
          Missing required tools on {{ inventory_hostname }}.
          'databricks' and 'python3' must be on PATH before deploying agent configs.
          Install them as part of your managed baseline, then re-run this play.
        success_msg: "Required tools found on {{ inventory_hostname }}"

    - name: Assert jq and curl present (required when hook-event telemetry is enabled)
      ansible.builtin.assert:
        that:
          - "lookup('ansible.builtin.pipe', 'command -v jq', errors='ignore') != ''"
          - "lookup('ansible.builtin.pipe', 'command -v curl', errors='ignore') != ''"
        fail_msg: >
          Missing jq or curl on {{ inventory_hostname }}.
          These are required when hook-event telemetry is enabled.
        success_msg: "jq and curl found on {{ inventory_hostname }}"
      # Remove or condition this block if your bundle was generated with --hook-telemetry off

  tasks:
    - name: Create work directory
      ansible.builtin.file:
        path: "{{ unity_gateway_work_dir }}"
        state: directory
        mode: "0700"
        owner: root
        group: root

    - name: Copy tarball to host
      ansible.builtin.copy:
        src: "{{ unity_gateway_tarball_local }}"
        dest: "{{ unity_gateway_work_dir }}/unity-gateway-agents.tar.gz"
        mode: "0600"
        owner: root
        group: root

    - name: Unpack tarball
      ansible.builtin.unarchive:
        src: "{{ unity_gateway_work_dir }}/unity-gateway-agents.tar.gz"
        dest: "{{ unity_gateway_work_dir }}"
        remote_src: true
        extra_opts:
          - "--strip-components=1"

    - name: Run install.sh
      ansible.builtin.command:
        cmd: "./install.sh"
        chdir: "{{ unity_gateway_work_dir }}"
      register: install_result
      changed_when: install_result.rc == 0

    - name: Assert install.sh succeeded
      ansible.builtin.assert:
        that: install_result.rc == 0
        fail_msg: >
          install.sh exited {{ install_result.rc }} on {{ inventory_hostname }}.
          Exit codes: 2=not root, 3=missing prereq, 4=missing source file, 5=copy/perm failure.
          Stdout: {{ install_result.stdout }}
          Stderr: {{ install_result.stderr }}

    - name: Clean up work directory
      ansible.builtin.file:
        path: "{{ unity_gateway_work_dir }}"
        state: absent

    - name: Print Phase-B reminder
      ansible.builtin.debug:
        msg: >
          Phase A complete on {{ inventory_hostname }}.
          Each developer must still run Phase B (ONCE, interactively):
            databricks auth login --host <host> --profile fevm-west
          This requires a browser and cannot be automated.
```

---

## Exit code reference

`install.sh` returns a structured exit code; the play surfaces it in the assert message:

| Code | Meaning |
|---|---|
| 0 | Success (or `--dry-run` / `--print-target-dir`) |
| 1 | Usage error |
| 2 | Not root and no `--target-root` set |
| 3 | Critical prereq missing (`databricks` or `python3` always; `jq`/`curl` when emitter present) |
| 4 | Required source file missing (`managed-settings.json`) |
| 5 | Copy or permission failure |

---

## Notes

- **Idempotency:** `install.sh` always re-copies files and updates the version marker.
  Re-running the play on an already-configured machine is safe.
- **`--target-root`:** do not pass this flag here. It is for unprivileged staging
  and unit tests only; a fleet play should use real system paths.
- **Version variable:** set `unity_gateway_version` in your inventory or as an
  extra var (`-e unity_gateway_version=abc1234-20260101`). The version string is
  printed by `make deploy-package` and embedded in the tarball filename.
- **Phase B:** after the play succeeds, notify developers to run
  `databricks auth login --host <host> --profile fevm-west` once, interactively.
  Verify with `/status` in Claude Code or `codex --strict-config doctor` for Codex.
