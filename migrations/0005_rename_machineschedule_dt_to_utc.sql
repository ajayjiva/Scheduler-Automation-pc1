-- Rename pc1.machineschedule.date_and_time -> date_and_time_utc and
-- record the new "seq" semantics agreed during Phase 2 verification.
--
-- Why this migration exists
-- -------------------------
-- During Phase 2 verification we discovered two related ergonomics
-- problems in pc1.machineschedule:
--
--   1. `date_and_time` does not advertise its UTC nature in the name.
--      The pc1 design deliberately stores UTC; the legacy public-
--      schema column was migrated to UTC under the same name. But
--      `start_time` and `end_time` on the same row carry facility-
--      LOCAL time-of-day values, so a row looks like this:
--
--          date_and_time = '2026-05-28 07:00:00+00'  (UTC)
--          start_time    = '00:00:00'                (LOCAL, PT)
--          end_time      = '00:15:00'                (LOCAL, PT)
--
--      The three fields all describe the same slot but appear to
--      disagree at a glance. Renaming the column to `date_and_time_utc`
--      makes the contrast explicit and prevents future readers from
--      mis-treating it as facility-local.
--
--   2. The `seq` column was reserved for future engine use in
--      migration 0004 but never populated. The scheduling engine
--      (legacy, and the planned Phase 4 port) extracts the facility-
--      local DATE from each slot via `str(seq)[:8]` -- see
--      next_modalitytype_scheduler.same_day() and
--      all_modality_scheduler.build_chain_for_anchor() in the legacy
--      code. For this contract to work across slots whose UTC date
--      differs from their facility-local date (every evening slot in
--      a non-UTC time zone), `seq` MUST be a YYYYMMDDHHMMSS integer
--      of the FACILITY-LOCAL clock, not UTC.
--
--      This migration only renames the column. The generator
--      (generate_machineschedule.py) ships in the same PR with the
--      logic that populates `seq` as the facility-local
--      YYYYMMDDHHMMSS bigint. Operators should wipe and re-run the
--      generator after applying both changes -- see the README at
--      the bottom of this file for the SQL.
--
-- Index coverage
-- --------------
-- Postgres rewrites btree index definitions when the underlying
-- column is renamed (the index continues to function, the new column
-- name appears in `\d` output), so we do NOT need to drop and
-- recreate idx_ms_client_facility_dt. The index name keeps its
-- short form (no `_utc` suffix) so historical references in any
-- already-applied environment stay valid.
--
-- Idempotency
-- -----------
-- ALTER TABLE ... RENAME COLUMN has no IF EXISTS clause. We wrap the
-- rename in a DO block that checks information_schema first, so
-- re-running this migration on an already-renamed table is a no-op.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'pc1'
           AND table_name   = 'machineschedule'
           AND column_name  = 'date_and_time'
    ) THEN
        ALTER TABLE pc1.machineschedule
            RENAME COLUMN date_and_time TO date_and_time_utc;
    END IF;
END$$;

-- After applying:
--
-- 1) Wipe the slots already inserted (no production data, per the PR
--    discussion):
--
--       TRUNCATE pc1.machineschedule;
--
-- 2) Re-run the generator so every row gets the new seq value:
--
--       python generate_machineschedule.py
--
-- 3) Spot-check that seq[:8] equals the facility-local YYYYMMDD for
--    every row, including evening slots whose UTC date differs:
--
--       SELECT seq,
--              substring(seq::text, 1, 8)         AS seq_day,
--              to_char(
--                  date_and_time_utc AT TIME ZONE f.timezone,
--                  'YYYYMMDD'
--              )                                  AS facility_local_day,
--              date_and_time_utc,
--              start_time
--         FROM pc1.machineschedule ms
--         JOIN pc1.facilities      f ON f.id = ms.facility_id
--        WHERE start_time IN ('00:00:00', '23:45:00')   -- edge slots
--        ORDER BY date_and_time_utc
--        LIMIT 20;
--
--    seq_day must match facility_local_day on every row. Any row
--    where it doesn't indicates a generator bug -- file an issue.
