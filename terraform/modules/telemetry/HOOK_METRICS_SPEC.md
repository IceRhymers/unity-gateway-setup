# Hook Metrics Specification — Claude Code and Codex

This document defines the events that Claude Code and Codex deployment hooks collect.
It covers three purposes: reliability and user experience, adoption and productivity,
and security and governance. It does not cover cost attribution.

All events land in the existing `claude_hook_events` table. The telemetry module
creates that table. The agent generators emit the hook scripts. See
`agent_setups/scripts/agents/claude_code.py` and `agent_setups/scripts/agents/codex.py`.

## Status

This is a design specification. No code implements the new events yet. The team
approved the scope and the constraints below through a requirements interview on
2026-08-31.

## Ground rules

The following rules apply to every event in this document.

1. Each hook is report-only. A hook never blocks a tool call. A hook always exits 0.
2. Each event rides the existing spool-and-flush path to Zerobus.
3. Each event records its owner agent. The two agents expose different hook surfaces.
4. Sensitive fields use classified buckets. The hooks store no raw command text, no
   full file path, and no full destination URL.
5. Adoption events record only directly-observed hook events. The hooks infer nothing.

## Verification basis and version skew

The team verified the Codex hook surface against the codex-cli native binary. The
local binary was version 0.142.0-alpha.1. The repository targets version 0.150.1.

Treat every fact that came from the 0.142 binary as provisional. Reconfirm each fact
against 0.150.1 before implementation. Two facts need attention:

- The 0.142 binary exposes no `SessionEnd` hook event. The generator at
  `agent_setups/scripts/agents/codex.py:508` registers a `SessionEnd` flush boundary.
  That flush may never fire. Verify the event on 0.150.1. Treat a missing event as a bug.
- The 0.142 binary exposes no `StopFailure` hook event. Claude Code exposes one.

## The reliability asymmetry

Claude Code captures turn failures and API errors through its `StopFailure` hook. The
`stop_failure` event records these failures.

Codex exposes no equivalent hook. Codex turn failures appear only in the
`codex exec --json` output stream. That stream covers scripted runs. It does not cover
the interactive session. A hook cannot collect a Codex turn failure.

The team accepts this asymmetry. Turn-failure telemetry stays Claude-only. Codex
reliability telemetry stays at the command level. The parity matrix documents the gap.
An out-of-band `codex exec --json` collector is out of scope for this specification.

## Event catalog

Each table lists the event, its trigger, its category, its `attributes` fields, and its
owner agent. The `attributes` fields describe bucketed values, not raw values.

### Reliability and user experience

| Event | Trigger | attributes (bucketed) | Claude | Codex |
|-------|---------|-----------------------|--------|-------|
| `stop_failure` | Claude `StopFailure` | variant, error_class, model, origin | Yes | No |
| `tool_error` | PostToolUse, shell exit code is not 0 | tool_name, error_class, exit_code | Yes | Yes |
| `command_timeout` | PostToolUse, `wall_time_seconds` over a threshold | tool_name, elapsed_bucket | Yes | Yes |
| `session_summary` | Session end boundary | turn_count, tool_call_count, duration_bucket, end_state | Yes | Yes |
| `telemetry_health` | OAuth mint fails, or endpoint is dormant | failure_kind | Yes | Yes |

Notes:
- `tool_error` reads `exit_code` from the shell `tool_response`. It stores no command text.
- A non-zero exit code is common. Downstream queries filter benign error classes.
- `command_timeout` reads `wall_time_seconds` from the same `tool_response`.
- `session_summary` uses each agent's session boundary. Confirm the Codex boundary
  against 0.150.1. See the version-skew section.

### Adoption and productivity

| Event | Trigger | attributes (bucketed) | Claude | Codex |
|-------|---------|-----------------------|--------|-------|
| `plugin_inventory` | SessionStart | plugin_count, plugins | Yes | Yes |
| `slash_command` | UserPromptSubmit, `/` prefix | command_name | Yes | No |
| `skill_used` | PostToolUse, Skill tool | skill_name | Yes | No |
| `subagent_used` | SubagentStart | subagent_type | Yes | Yes |
| `mcp_tool_used` | PostToolUse, MCP tool | server, tool_name, ok_flag | Yes | Yes |
| `doc_read` | PostToolUse, Read tool, doc pattern | doc_class | Yes | No |
| `pr_pushed` | PostToolUse, Bash, push or PR create | pr_number | Yes | Yes |

Notes:
- Codex has no Skill tool. So `skill_used` stays Claude-only.
- Codex does not surface a Read tool to hooks. So `doc_read` stays Claude-only.
- Codex has no `/` slash-command surface for hooks. So `slash_command` stays Claude-only.
- `mcp_tool_used` measures the MCP autodiscovery investment. Confirm the Codex MCP
  tool-name shape against 0.150.1.

### Security and governance

| Event | Trigger | attributes (bucketed) | Claude | Codex |
|-------|---------|-----------------------|--------|-------|
| `command_flagged` | PreToolUse, Bash, risk pattern match | tool_name, risk_class | Yes | Yes |
| `secret_detected` | PreToolUse, Bash, secret prefilter match | tool_name, secret_class | Yes | Yes |
| `sensitive_file_access` | PreToolUse, Read or Edit, sensitive path match | sensitivity_class, tool_name | Yes | partial |
| `network_egress` | PreToolUse, Bash, external network command | egress_flag | Yes | Yes |
| `secret_in_tool_args` | PreToolUse, secret-shaped string in tool arguments | tool_name, secret_class | Yes | Yes |
| `managed_config_drift` | Local config differs from managed config | agent, drift_kind | Yes | Yes |

Notes:
- `sensitivity_class` holds a class such as `secret`, `config`, or `source`. It holds no path.
- `egress_flag` holds `internal` or `external`. It holds no host and no URL.
- `secret_class` holds the matched pattern family. It holds no secret value.
- `sensitive_file_access` on Codex covers only shell-based access. Codex hooks do not
  see a Read or Edit tool. Confirm the Codex file-tool surface against 0.150.1.

## Envelope schema

The table keeps a single physical schema. The `agent` dimension separates Claude from
Codex. Promote the following fields to typed envelope columns. Queries group and join on
these fields often.

- `event_id`
- `event_time`
- `category`
- `event_name`
- `agent`
- `agent_version`
- `repo`
- `permission_mode`
- `session_id`
- `parent_session_id`
- `end_state`
- `user`
- `machine`

Keep every other field in the VARIANT `attributes` bag. Query a bucketed field as
`attributes:field`.

The `repo` column holds a normalized `org/repo` slug. It holds no full git remote. A
full remote can leak an internal host name.

## Parity matrix

The event catalog tables above hold the parity matrix. The `Claude` and `Codex` columns
state which agent emits each event. Three values appear:

- `Yes` — the agent emits the event.
- `No` — the agent cannot emit the event. The hook surface does not support it.
- `partial` — the agent emits a reduced form. A note explains the limit.

The single schema does not imply feature parity. The stream shares one table. Each
agent emits the events its hook surface supports.

## Phasing

The team ranked the events by value per unit of work.

**P0**
- `tool_error` (both agents)
- `session_summary` (both agents)
- `sensitive_file_access` (Claude, plus the Codex shell form)

**P1**
- `command_timeout` (both agents)
- `mcp_tool_used` (both agents)
- `network_egress` (both agents)
- `secret_in_tool_args` (both agents)
- `telemetry_health` (both agents)

**P2**
- `managed_config_drift` (both agents)

Claude keeps its existing `stop_failure`. The catalog carries it for completeness.

## Open items to verify before implementation

1. Confirm the Codex session-end boundary on 0.150.1. The 0.142 binary has no `SessionEnd`.
2. Confirm the Codex PostToolUse `tool_response` still carries `exit_code` and
   `wall_time_seconds` on 0.150.1.
3. Confirm the Codex MCP tool-name shape on 0.150.1.
4. Fix or confirm the `SessionEnd` flush at `agent_setups/scripts/agents/codex.py:508`.
