-- create-state-objects.sql
-- Run this file connected AS the group role `terraform_writers`, not as an
-- individual. In Lakebase you do not inherit a group role transitively; you
-- authenticate as it (PGUSER=terraform_writers) when you belong to the backing
-- workspace group. bootstrap-state.sh opens the connection that way, so every
-- object below is created and owned by `terraform_writers` directly. Every
-- group member operates the backend by assuming the same role, so no ownership
-- transfer and no per-member GRANT are needed.
--
-- The group role needs CREATE on the database before this runs. The bootstrap
-- grants that first, as the database owner.

CREATE SCHEMA IF NOT EXISTS tfstate_infra;

CREATE SEQUENCE IF NOT EXISTS tfstate_infra.global_states_id_seq AS bigint;

-- NOTE: `name text UNIQUE` AND the separate unique index below both exist because
-- that is exactly what the pg backend creates. The duplication is deliberate — it
-- mirrors upstream so the pre-created table cannot diverge. Do not "fix" it.
CREATE TABLE IF NOT EXISTS tfstate_infra.states (
  id   bigint NOT NULL DEFAULT nextval('tfstate_infra.global_states_id_seq') PRIMARY KEY,
  name text UNIQUE,
  data text
);

CREATE UNIQUE INDEX IF NOT EXISTS states_by_name ON tfstate_infra.states (name);
