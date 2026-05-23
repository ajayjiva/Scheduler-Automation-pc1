-- pc1.modalities: relax constraints for RIS sources that don't expose
-- a stable internal modality ID (NovaRIS grids only carry the machine
-- name; the scraper uses source_record_key = modality_machine and
-- conflicts on (facility_id, source_record_key) instead).

ALTER TABLE pc1.modalities ALTER COLUMN ris_modality_id DROP NOT NULL;

ALTER TABLE pc1.modalities DROP CONSTRAINT modalities_client_id_ris_modality_id_key;

DROP INDEX IF EXISTS pc1.idx_mod_code_client;

-- pc1.facilities: the scraper resolves facility_id by
-- (client_id, facility_name); add the matching unique constraint so
-- the lookup is well-defined and seed scripts can ON CONFLICT on it.

ALTER TABLE pc1.facilities
    ADD CONSTRAINT facilities_client_id_facility_name_key
    UNIQUE (client_id, facility_name);
