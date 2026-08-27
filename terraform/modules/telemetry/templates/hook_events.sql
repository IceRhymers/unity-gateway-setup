-- Claude Code hook-event table: custom reporting events that native OTEL does
-- not emit (agent-usage attribution, reliability stalls, governance signals,
-- workflow adoption). Populated by the generated emit_hook_events.sh hook via
-- the Zerobus REST ingest API.
--
-- {{TABLE}} is substituted with the fully-qualified catalog.schema.table name.
--
-- One wide table for every category, distinguished by `category` + `event_name`.
-- Common fields are typed for clean querying; category-specific fields live in
-- `attributes` as VARIANT, so the table stays stable as new event kinds are added
-- and downstream can query fields natively (attributes:skill_name) without
-- parse_json(). Zerobus maps VARIANT to a JSON-encoded string on the wire — which
-- is exactly what the hook sends — and materializes it as VARIANT on write.
--
-- Managed Delta table (no LOCATION) — required by Zerobus. `event_time` receives
-- a Unix-microseconds integer from the hook (Zerobus timestamp convention).
-- VARIANT requires a recent Delta reader (DBR 15.3+ / serverless SQL); readers on
-- older compute would need STRING + parse_json() instead.
CREATE TABLE IF NOT EXISTS {{TABLE}} (
  event_id     STRING    COMMENT 'Client-generated UUID; dedupe key (Zerobus is at-least-once)',
  event_time   TIMESTAMP COMMENT 'Emit time (Unix microseconds on the wire)',
  category     STRING    COMMENT 'usage | reliability | governance | adoption',
  event_name   STRING    COMMENT 'e.g. skill_used, stop_failure, secret_detected, doc_read',
  session_id   STRING    COMMENT 'Claude Code session; joins per-event rows to plugin_inventory',
  user         STRING    COMMENT 'Workspace user identity (email) the session runs as; falls back to OS user, or overridden',
  machine      STRING    COMMENT 'Host name, for fleet-level rollups',
  agent        STRING    COMMENT 'Coding agent that emitted the event (claude-code)',
  plugin_name  STRING    COMMENT 'Owning plugin when the event carries one',
  attributes   VARIANT   COMMENT 'Event-specific fields (JSON-encoded string on the wire)'
) USING DELTA
TBLPROPERTIES (
  'unity_gateway.schemaVersion' = 'v1'
);
