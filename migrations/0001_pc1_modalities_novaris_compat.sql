-- pc1.modalities: relax constraints for RIS sources that don't expose
-- a stable internal modality ID (NovaRIS grids only carry the machine
-- name; the scraper uses source_record_key = modality_machine and
-- conflicts on (facility_id, source_record_key) instead).
--
-- Written to be idempotent so it can be applied to a fresh-state DB
-- or one where pc1.modalities was already created in the target shape.

ALTER TABLE pc1.modalities ALTER COLUMN ris_modality_id DROP NOT NULL;

ALTER TABLE pc1.modalities
    DROP CONSTRAINT IF EXISTS modalities_client_id_ris_modality_id_key;

DROP INDEX IF EXISTS pc1.idx_mod_code_client;

-- pc1.facilities: the scraper resolves facility_id by
-- (client_id, facility_name); add the matching unique constraint so
-- the lookup is well-defined and seed scripts can ON CONFLICT on it.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'pc1.facilities'::regclass
          AND conname  = 'facilities_client_id_facility_name_key'
    ) THEN
        ALTER TABLE pc1.facilities
            ADD CONSTRAINT facilities_client_id_facility_name_key
            UNIQUE (client_id, facility_name);
    END IF;
END$$;
