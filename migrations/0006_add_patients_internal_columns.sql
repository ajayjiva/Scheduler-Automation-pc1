-- pc1.patients: add internal/logic columns (plain, un-prefixed) alongside the
-- existing ris_* source columns.
--
-- Convention (NEW — see docs/orders.md "ris_* vs plain columns"):
--   * ris_* columns hold RAW values exactly as scraped from the source RIS.
--   * plain (un-prefixed) columns hold the values used by INTERNAL logic
--     (scheduling engine, pc1.orders_v). Today they are a straight copy of the
--     matching ris_* column; the split exists so future cleanup / normalization
--     (name casing, language-code mapping, etc.) can be written to the plain
--     column WITHOUT mutating the source-of-truth ris_* value.
--
-- Known dependency: when a future patient scraper refreshes the ris_* columns,
-- it must ALSO refresh these plain columns (or a trigger must), otherwise the
-- plain columns go stale. For the Phase 3.5 stub (hand-crafted data) this is a
-- non-issue. Documented in docs/orders.md.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + a replay-safe backfill.

ALTER TABLE pc1.patients
    ADD COLUMN IF NOT EXISTS patient_full_name varchar(200) NULL,   -- <- ris_full_name
    ADD COLUMN IF NOT EXISTS first_name        varchar(100) NULL,   -- <- ris_first_name
    ADD COLUMN IF NOT EXISTS middle_name       varchar(100) NULL,   -- <- ris_middle_name
    ADD COLUMN IF NOT EXISTS last_name         varchar(100) NULL,   -- <- ris_last_name
    ADD COLUMN IF NOT EXISTS patient_dob       date         NULL,   -- <- ris_birth_date
    ADD COLUMN IF NOT EXISTS language          varchar(50)  NULL;   -- <- ris_language

-- One-time backfill: copy current source values into the plain columns.
-- Re-runnable (rewrites the same values). Deliberately does NOT touch
-- updated_at -- this is a structural backfill, not a sync write.
UPDATE pc1.patients SET
    patient_full_name = ris_full_name,
    first_name        = ris_first_name,
    middle_name       = ris_middle_name,
    last_name         = ris_last_name,
    patient_dob       = ris_birth_date,
    language          = ris_language;
