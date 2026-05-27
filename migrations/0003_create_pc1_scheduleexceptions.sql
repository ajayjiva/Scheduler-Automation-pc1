-- pc1.scheduleexceptions: scheduling exception rules scraped from NovaRIS
-- ModalityScheduling.aspx into the pc1 schema.
--
-- Each row is one rule (LUNCH, holiday, machine downtime, technician out,
-- etc.) that blocks (`type='Hard'`, availability=0) or annotates
-- (`type='Soft'`, display-only) one or more slots on the calendar. Rules
-- can be one-off (recurrence='None') or repeat Daily / Weekly / Monthly.
--
-- Shape rationale:
--   * Multi-tenant by client_id (FK to pc1.clients).
--   * facility_id and modality_id are real FKs to pc1.facilities /
--     pc1.modalities (replacing the legacy text columns `facility` and
--     `name`). Both are NULL-able for forward compatibility — the scraper
--     itself always writes non-NULL values (NovaRIS's UI forces a
--     facility + modality selection per rule) but the schema accommodates
--     future RIS systems that may permit facility-wide or
--     machine-agnostic rules.
--   * Recurrence is carried as flat boolean columns: weekdays_only +
--     7 day-of-week flags (is_sunday..is_saturday) for Weekly +
--     31 day-of-month flags (day_1..day_31) for Monthly. JSONB was
--     evaluated and rejected: flat columns are smaller on disk, validate
--     at the schema level (no typo'd keys), and index naturally.
--   * `repeat_every` is preserved even though no current tenant uses it
--     — NovaRIS exposes it on the popup ("every N weeks/months") and
--     future tenants may.
--   * CHECK constraints pin `recurrence` and `type` to their documented
--     vocabularies — catches scraper bugs at INSERT time rather than at
--     reconcile time.
--   * Audit + source-system columns match the conventions used in
--     pc1.facilities / pc1.modalities / pc1.proceduresestimate so all
--     four tables are consistent: created_at/updated_at timestamptz,
--     created_by/updated_by FK to pc1.user_profiles with ON DELETE SET
--     NULL, plus the ris_system/ris_sync_status/ris_last_synced_at/
--     ris_metadata/synced_at sync-tracking quintet.
--
-- Uniqueness:
--   No 4-shape override pattern here (unlike proceduresestimate) —
--   exception rules are operational events, not catalog entries with
--   per-machine timing overrides. Each rule is a real, specific event.
--   A simple unique partial index on
--   (client_id, facility_id, source_record_key) WHERE source_record_key
--   IS NOT NULL enforces "one row per NovaRIS frequencyId per (tenant,
--   facility)" while letting manually-inserted rows (source_record_key
--   NULL) coexist.
--
-- Cross-tenant safety:
--   A BEFORE INSERT/UPDATE trigger validates that client_id matches the
--   FK'd facility's client_id and the FK'd modality's client_id. Without
--   it, a buggy writer could silently insert a row tying client A's
--   exception to client B's facility — the trigger turns that into a
--   loud INSERT failure instead. Short-circuits when both FKs are NULL.
--
-- Written to be idempotent so it can be applied to a fresh DB or replayed
-- safely on one where pc1.scheduleexceptions already exists.

-- ── Cross-tenant consistency trigger function ──────────────────────────────

CREATE OR REPLACE FUNCTION pc1.check_scheduleexceptions_consistency()
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
                'scheduleexceptions.facility_id=% references a row that does not exist in pc1.facilities',
                NEW.facility_id;
        END IF;
        IF fac_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'scheduleexceptions.client_id=% does not match pc1.facilities(id=%).client_id=%',
                NEW.client_id, NEW.facility_id, fac_client_id;
        END IF;
    END IF;

    IF NEW.modality_id IS NOT NULL THEN
        SELECT client_id INTO mod_client_id
          FROM pc1.modalities
         WHERE id = NEW.modality_id;
        IF mod_client_id IS NULL THEN
            RAISE EXCEPTION
                'scheduleexceptions.modality_id=% references a row that does not exist in pc1.modalities',
                NEW.modality_id;
        END IF;
        IF mod_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'scheduleexceptions.client_id=% does not match pc1.modalities(id=%).client_id=%',
                NEW.client_id, NEW.modality_id, mod_client_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── Table ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pc1.scheduleexceptions (
    id                          bigserial    NOT NULL,
    client_id                   bigint       NOT NULL,
    facility_id                 bigint       NULL,
    modality_id                 bigint       NULL,

    -- Business fields
    modality_type               character varying(20)    NULL,
    description                 text                     NULL,
    start_date                  date                     NULL,
    start_time                  time without time zone   NULL,
    end_date                    date                     NULL,
    end_time                    time without time zone   NULL,
    recurrence                  text                     NULL,
    type                        text                     NULL,
    repeat_every                integer                  NOT NULL DEFAULT 1,
    weekdays_only               boolean                  NOT NULL DEFAULT false,

    -- Weekly day-of-week mask
    is_sunday                   boolean      NOT NULL DEFAULT false,
    is_monday                   boolean      NOT NULL DEFAULT false,
    is_tuesday                  boolean      NOT NULL DEFAULT false,
    is_wednesday                boolean      NOT NULL DEFAULT false,
    is_thursday                 boolean      NOT NULL DEFAULT false,
    is_friday                   boolean      NOT NULL DEFAULT false,
    is_saturday                 boolean      NOT NULL DEFAULT false,

    -- Monthly day-of-month mask
    day_1                       boolean      NOT NULL DEFAULT false,
    day_2                       boolean      NOT NULL DEFAULT false,
    day_3                       boolean      NOT NULL DEFAULT false,
    day_4                       boolean      NOT NULL DEFAULT false,
    day_5                       boolean      NOT NULL DEFAULT false,
    day_6                       boolean      NOT NULL DEFAULT false,
    day_7                       boolean      NOT NULL DEFAULT false,
    day_8                       boolean      NOT NULL DEFAULT false,
    day_9                       boolean      NOT NULL DEFAULT false,
    day_10                      boolean      NOT NULL DEFAULT false,
    day_11                      boolean      NOT NULL DEFAULT false,
    day_12                      boolean      NOT NULL DEFAULT false,
    day_13                      boolean      NOT NULL DEFAULT false,
    day_14                      boolean      NOT NULL DEFAULT false,
    day_15                      boolean      NOT NULL DEFAULT false,
    day_16                      boolean      NOT NULL DEFAULT false,
    day_17                      boolean      NOT NULL DEFAULT false,
    day_18                      boolean      NOT NULL DEFAULT false,
    day_19                      boolean      NOT NULL DEFAULT false,
    day_20                      boolean      NOT NULL DEFAULT false,
    day_21                      boolean      NOT NULL DEFAULT false,
    day_22                      boolean      NOT NULL DEFAULT false,
    day_23                      boolean      NOT NULL DEFAULT false,
    day_24                      boolean      NOT NULL DEFAULT false,
    day_25                      boolean      NOT NULL DEFAULT false,
    day_26                      boolean      NOT NULL DEFAULT false,
    day_27                      boolean      NOT NULL DEFAULT false,
    day_28                      boolean      NOT NULL DEFAULT false,
    day_29                      boolean      NOT NULL DEFAULT false,
    day_30                      boolean      NOT NULL DEFAULT false,
    day_31                      boolean      NOT NULL DEFAULT false,

    -- Soft-delete
    is_active                   boolean      NOT NULL DEFAULT true,

    -- Source-system tracking (matches pc1.proceduresestimate / pc1.modalities)
    source_record_key           character varying(255) NULL,
    content_hash                character varying(255) NULL,
    ris_system                  character varying(50)  NULL DEFAULT 'konica_exa',
    ris_sync_status             character varying(20)  NULL DEFAULT 'synced',
    ris_last_synced_at          timestamp with time zone NULL,
    ris_metadata                jsonb                  NULL,
    synced_at                   timestamp with time zone NULL,

    -- Audit (matches pc1.proceduresestimate / pc1.modalities)
    created_at                  timestamp with time zone NOT NULL DEFAULT now(),
    updated_at                  timestamp with time zone NOT NULL DEFAULT now(),
    created_by                  bigint       NULL,
    updated_by                  bigint       NULL,

    CONSTRAINT scheduleexceptions_pkey
        PRIMARY KEY (id),
    CONSTRAINT scheduleexceptions_client_id_fkey
        FOREIGN KEY (client_id)   REFERENCES pc1.clients(id),
    CONSTRAINT scheduleexceptions_facility_id_fkey
        FOREIGN KEY (facility_id) REFERENCES pc1.facilities(id),
    CONSTRAINT scheduleexceptions_modality_id_fkey
        FOREIGN KEY (modality_id) REFERENCES pc1.modalities(id),
    CONSTRAINT fk_se_created_by
        FOREIGN KEY (created_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL,
    CONSTRAINT fk_se_updated_by
        FOREIGN KEY (updated_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL,
    CONSTRAINT scheduleexceptions_recurrence_chk
        CHECK (recurrence IS NULL OR recurrence IN ('None', 'Daily', 'Weekly', 'Monthly')),
    CONSTRAINT scheduleexceptions_type_chk
        CHECK (type IS NULL OR type IN ('Hard', 'Soft'))
) TABLESPACE pg_default;

-- ── Indexes ────────────────────────────────────────────────────────────────

-- Source-key uniqueness: one row per (tenant, facility, NovaRIS frequencyId).
-- Partial WHERE source_record_key IS NOT NULL lets manually-inserted rows
-- (no upstream key) coexist with scraper rows.
CREATE UNIQUE INDEX IF NOT EXISTS scheduleexceptions_source_unique_idx
    ON pc1.scheduleexceptions USING btree (
        client_id,
        facility_id,
        source_record_key
    ) TABLESPACE pg_default
    WHERE source_record_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_se_client_id
    ON pc1.scheduleexceptions USING btree (client_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_se_facility_id
    ON pc1.scheduleexceptions USING btree (facility_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_se_modality_id
    ON pc1.scheduleexceptions USING btree (modality_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_se_active
    ON pc1.scheduleexceptions USING btree (is_active) TABLESPACE pg_default
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_se_start_date
    ON pc1.scheduleexceptions USING btree (start_date) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_se_modality_type
    ON pc1.scheduleexceptions USING btree (modality_type) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_se_content_hash
    ON pc1.scheduleexceptions USING btree (content_hash) TABLESPACE pg_default
    WHERE content_hash IS NOT NULL;

-- ── Trigger ────────────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS trg_scheduleexceptions_consistency_check
    ON pc1.scheduleexceptions;

CREATE TRIGGER trg_scheduleexceptions_consistency_check
    BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id
    ON pc1.scheduleexceptions
    FOR EACH ROW
    EXECUTE FUNCTION pc1.check_scheduleexceptions_consistency();
