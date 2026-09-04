-- create-state-objects.sql
-- Run as the bootstrap identity (the project creator, who MUST be a member of
-- terraform_writers — see bootstrap README §3.d). Ownership is transferred to
-- the group role at the end so every group member can operate the backend by
-- inheritance, with no additional grants required.

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

-- Converge ownership onto the group role, every run (idempotent, self-repairing).
-- ALTER ... OWNER TO requires the issuer to be a member of terraform_writers;
-- the bootstrap preflight asserts this before any DDL runs.
ALTER SCHEMA   tfstate_infra                      OWNER TO terraform_writers;
ALTER SEQUENCE tfstate_infra.global_states_id_seq OWNER TO terraform_writers;
ALTER TABLE    tfstate_infra.states               OWNER TO terraform_writers;
