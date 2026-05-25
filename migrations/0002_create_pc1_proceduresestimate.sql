-- pc1.proceduresestimate: standard-procedure catalog scraped from NovaRIS
-- ViewStandardProcedures.aspx into the pc1 schema.
--
-- Shape rationale:
--   * Multi-tenant by client_id (FK to pc1.clients).
--   * facility_id is a real FK to pc1.facilities (nullable to support
--     the "global default + per-facility override" pattern).
--   * modality_id is a real FK to pc1.modalities (nullable to support
--     the "any-machine" tier of the per-machine override pattern).
--   * Audit + source-system columns match the conventions used in
--     pc1.facilities and pc1.modalities so all three tables are
--     consistent: created_at/updated_at timestamptz, created_by/
--     updated_by FK to pc1.user_profiles with ON DELETE SET NULL,
--     plus the ris_system/ris_sync_status/ris_last_synced_at/
--     ris_metadata/synced_at sync-tracking quintet.
--
-- Per-machine override design (load-bearing):
--   The unique partial index on (client_id, COALESCE(facility_id,0),
--   COALESCE(modality_id,0), source_record_key) WHERE source_record_key
--   IS NOT NULL preserves four coexisting row shapes for the same
--   source_record_key:
--     (NULL, NULL)   → global default
--     (X,    NULL)   → facility-level override
--     (NULL, K)      → per-machine global (rare)
--     (X,    K)      → per-(facility, machine) override
--   The scheduler resolves precedence in Python per (order,
--   candidate_machine); the schema just lets all four coexist.
--
-- Cross-tenant safety:
--   A BEFORE INSERT/UPDATE trigger validates that client_id matches
--   the FK'd facility's client_id and the FK'd modality's client_id.
--   Without this, a buggy writer could silently insert a row tying
--   client A's procedure to client B's facility — the trigger turns
--   that into a loud INSERT failure instead.
--
-- Written to be idempotent so it can be applied to a fresh-state DB
-- or replayed safely on one where pc1.proceduresestimate already exists.

-- ── Cross-tenant consistency trigger function ──────────────────────────────

CREATE OR REPLACE FUNCTION pc1.check_proceduresestimate_consistency()
RETURNS trigger AS $$
DECLARE
    fac_client_id bigint;
    mod_client_id bigint;
BEGIN
    IF NEW.facility_id IS NOT NULL THEN
        SELECT client_id INTO fac_client_id
          FROM pc1.facilities
         WHERE id = NEW.facility_id;
        IF fac_client_id IS NULL THEN
            RAISE EXCEPTION
                'proceduresestimate.facility_id=% references a row that does not exist in pc1.facilities',
                NEW.facility_id;
        END IF;
        IF fac_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'proceduresestimate.client_id=% does not match pc1.facilities(id=%).client_id=%',
                NEW.client_id, NEW.facility_id, fac_client_id;
        END IF;
    END IF;

    IF NEW.modality_id IS NOT NULL THEN
        SELECT client_id INTO mod_client_id
          FROM pc1.modalities
         WHERE id = NEW.modality_id;
        IF mod_client_id IS NULL THEN
            RAISE EXCEPTION
                'proceduresestimate.modality_id=% references a row that does not exist in pc1.modalities',
                NEW.modality_id;
        END IF;
        IF mod_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'proceduresestimate.client_id=% does not match pc1.modalities(id=%).client_id=%',
                NEW.client_id, NEW.modality_id, mod_client_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── Table ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pc1.proceduresestimate (
    id                          bigserial    NOT NULL,
    client_id                   bigint       NOT NULL,
    facility_id                 bigint       NULL,
    modality_id                 bigint       NULL,

    -- Business fields
    modality_type               character varying(20)  NULL,
    procedure_code              text[]                 NOT NULL,
    procedure_desc              text                   NOT NULL,
    required_time               integer                NULL,
    required_slots              integer                NOT NULL,
    anatomical_area             text                   NULL,
    exam_prep_instructions      text                   NULL,
    exam_prep_requires_prompt   boolean                NOT NULL DEFAULT false,

    -- Soft-delete
    is_active                   boolean      NOT NULL DEFAULT true,

    -- Source-system tracking (matches pc1.modalities / pc1.facilities)
    source_record_key           character varying(255) NULL,
    content_hash                character varying(255) NULL,
    ris_system                  character varying(50)  NULL DEFAULT 'konica_exa',
    ris_sync_status             character varying(20)  NULL DEFAULT 'synced',
    ris_last_synced_at          timestamp with time zone NULL,
    ris_metadata                jsonb                  NULL,
    synced_at                   timestamp with time zone NULL,

    -- Audit (matches pc1.modalities / pc1.facilities)
    created_at                  timestamp with time zone NOT NULL DEFAULT now(),
    updated_at                  timestamp with time zone NOT NULL DEFAULT now(),
    created_by                  bigint       NULL,
    updated_by                  bigint       NULL,

    CONSTRAINT proceduresestimate_pkey
        PRIMARY KEY (id),
    CONSTRAINT proceduresestimate_client_id_fkey
        FOREIGN KEY (client_id)   REFERENCES pc1.clients(id),
    CONSTRAINT proceduresestimate_facility_id_fkey
        FOREIGN KEY (facility_id) REFERENCES pc1.facilities(id),
    CONSTRAINT proceduresestimate_modality_id_fkey
        FOREIGN KEY (modality_id) REFERENCES pc1.modalities(id),
    CONSTRAINT fk_pe_created_by
        FOREIGN KEY (created_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL,
    CONSTRAINT fk_pe_updated_by
        FOREIGN KEY (updated_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL
) TABLESPACE pg_default;

-- ── Indexes ────────────────────────────────────────────────────────────────

-- 4-shape unique partial index: lets global / facility / machine / facility+
-- machine rows coexist for the same source_record_key. NULLs are coalesced
-- to sentinel 0 (no real facility/modality has id=0 since both PKs start
-- from 1).
CREATE UNIQUE INDEX IF NOT EXISTS proceduresestimate_source_unique_idx
    ON pc1.proceduresestimate USING btree (
        client_id,
        COALESCE(facility_id, 0::bigint),
        COALESCE(modality_id, 0::bigint),
        source_record_key
    ) TABLESPACE pg_default
    WHERE source_record_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pe_client_id
    ON pc1.proceduresestimate USING btree (client_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_pe_facility_id
    ON pc1.proceduresestimate USING btree (facility_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_pe_modality_id
    ON pc1.proceduresestimate USING btree (modality_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_pe_active
    ON pc1.proceduresestimate USING btree (is_active) TABLESPACE pg_default
    WHERE is_active = true;

-- GIN for array containment (e.g. procedure_code @> ARRAY['76705']).
-- Btree on a text[] column is essentially useless for lookup; GIN is the
-- right shape.
CREATE INDEX IF NOT EXISTS idx_pe_procedure_code
    ON pc1.proceduresestimate USING gin (procedure_code) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_pe_modality_type
    ON pc1.proceduresestimate USING btree (modality_type) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_pe_content_hash
    ON pc1.proceduresestimate USING btree (content_hash) TABLESPACE pg_default
    WHERE content_hash IS NOT NULL;

-- ── Trigger ────────────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS trg_proceduresestimate_consistency_check
    ON pc1.proceduresestimate;

CREATE TRIGGER trg_proceduresestimate_consistency_check
    BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id
    ON pc1.proceduresestimate
    FOR EACH ROW
    EXECUTE FUNCTION pc1.check_proceduresestimate_consistency();
