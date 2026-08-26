-- OTEL metrics table for Claude Code telemetry ingestion.
-- {{TABLE}} is substituted with the fully-qualified catalog.schema.table name.
-- Schema mirrors the OTLP metrics data model expected by the Databricks
-- /api/2.0/otel ingestion endpoint (otel.schemaVersion = v1).
CREATE TABLE IF NOT EXISTS {{TABLE}} (
  name STRING,
  description STRING,
  unit STRING,
  metric_type STRING,
  gauge STRUCT<
    start_time_unix_nano: LONG,
    time_unix_nano: LONG,
    value: DOUBLE,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: MAP<STRING, STRING>
    >>,
    attributes: MAP<STRING, STRING>,
    flags: INT
  >,
  sum STRUCT<
    start_time_unix_nano: LONG,
    time_unix_nano: LONG,
    value: DOUBLE,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: MAP<STRING, STRING>
    >>,
    attributes: MAP<STRING, STRING>,
    flags: INT,
    aggregation_temporality: STRING,
    is_monotonic: BOOLEAN
  >,
  histogram STRUCT<
    start_time_unix_nano: LONG,
    time_unix_nano: LONG,
    count: LONG,
    sum: DOUBLE,
    bucket_counts: ARRAY<LONG>,
    explicit_bounds: ARRAY<DOUBLE>,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: MAP<STRING, STRING>
    >>,
    attributes: MAP<STRING, STRING>,
    flags: INT,
    min: DOUBLE,
    max: DOUBLE,
    aggregation_temporality: STRING
  >,
  exponential_histogram STRUCT<
    attributes: MAP<STRING, STRING>,
    start_time_unix_nano: LONG,
    time_unix_nano: LONG,
    count: LONG,
    sum: DOUBLE,
    scale: INT,
    zero_count: LONG,
    positive_bucket: STRUCT<
      offset: INT,
      bucket_counts: ARRAY<LONG>
    >,
    negative_bucket: STRUCT<
      offset: INT,
      bucket_counts: ARRAY<LONG>
    >,
    flags: INT,
    exemplars: ARRAY<STRUCT<
      time_unix_nano: LONG,
      value: DOUBLE,
      span_id: STRING,
      trace_id: STRING,
      filtered_attributes: MAP<STRING, STRING>
    >>,
    min: DOUBLE,
    max: DOUBLE,
    zero_threshold: DOUBLE,
    aggregation_temporality: STRING
  >,
  summary STRUCT<
    start_time_unix_nano: LONG,
    time_unix_nano: LONG,
    count: LONG,
    sum: DOUBLE,
    quantile_values: ARRAY<STRUCT<
      quantile: DOUBLE,
      value: DOUBLE
    >>,
    attributes: MAP<STRING, STRING>,
    flags: INT
  >,
  metadata MAP<STRING, STRING>,
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
  metric_schema_url STRING
) USING DELTA
TBLPROPERTIES (
  'otel.schemaVersion' = 'v1'
);
