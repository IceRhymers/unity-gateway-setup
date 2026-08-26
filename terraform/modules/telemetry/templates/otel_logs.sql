-- OTEL logs table for Claude Code telemetry ingestion.
-- {{TABLE}} is substituted with the fully-qualified catalog.schema.table name.
-- Schema mirrors the OTLP logs data model expected by the Databricks
-- /api/2.0/otel ingestion endpoint (otel.schemaVersion = v1).
CREATE TABLE IF NOT EXISTS {{TABLE}} (
  event_name STRING,
  trace_id STRING,
  span_id STRING,
  time_unix_nano LONG,
  observed_time_unix_nano LONG,
  severity_number STRING,
  severity_text STRING,
  body STRING,
  attributes MAP<STRING, STRING>,
  dropped_attributes_count INT,
  flags INT,
  resource STRUCT<
    attributes: MAP<STRING, STRING>,
    dropped_attributes_count: INT
  >,
  resource_schema_url STRING,
  instrumentation_scope STRUCT<
    name: STRING,
    version: STRING,
    attributes: MAP<STRING, STRING>,
    dropped_attributes_count: INT
  >,
  log_schema_url STRING
) USING DELTA
TBLPROPERTIES (
  'otel.schemaVersion' = 'v1'
);
