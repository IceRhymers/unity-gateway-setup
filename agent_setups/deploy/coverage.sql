-- Fleet deployment-coverage + adoption view (v1: hook_events ONLY).
--
-- first_seen / last_seen are DERIVED (min/max of event_time), not columns in the table.
-- `user` is backtick-quoted — it is a reserved word / builtin in Spark SQL.
--
-- Schema confirmed against terraform/modules/telemetry/templates/hook_events.sql:
--   event_id, event_time TIMESTAMP, category, event_name, session_id,
--   user STRING, machine STRING, agent STRING, plugin_name, attributes VARIANT

SELECT `user`, machine, agent,
       COUNT(*)                                       AS events,
       MIN(event_time)                                AS first_seen,
       MAX(event_time)                                AS last_seen,
       DATEDIFF(current_timestamp(), MAX(event_time)) AS days_since_seen
FROM telemetry.hook_events
GROUP BY `user`, machine, agent
ORDER BY last_seen DESC;

-- ---------------------------------------------------------------------------
-- DEFERRED — do NOT gate AC8 on this block.
--
-- OTEL union (otel_logs / otel_traces):
--   Identity fields (workspace user, machine) are carried only inside the
--   `attributes` MAP and `resource` STRUCT columns with runtime-defined keys
--   that are NOT declared in this repo. Until those key names are confirmed
--   and stable, the union cannot be written without brittle string constants.
--   Track remaining open item in .omc/plans/open-questions.md (item 3).
--
-- Example shape (DO NOT enable until keys are confirmed):
--
-- UNION ALL
-- SELECT
--     resource['service.instance.id']            AS `user`,   -- UNCONFIRMED key
--     attributes['host.name']                    AS machine,  -- UNCONFIRMED key
--     attributes['telemetry.sdk.name']           AS agent,    -- UNCONFIRMED key
--     COUNT(*)                                   AS events,
--     MIN(timestamp)                             AS first_seen,
--     MAX(timestamp)                             AS last_seen,
--     DATEDIFF(current_timestamp(), MAX(timestamp)) AS days_since_seen
-- FROM system.otel_logs   -- table name also unconfirmed; placeholder only
-- GROUP BY 1, 2, 3
-- ---------------------------------------------------------------------------
