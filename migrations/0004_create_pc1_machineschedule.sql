-- pc1.machineschedule: the slot calendar. One row per
-- (client_id, facility_id, modality_id, date_and_time) representing a
-- bookable time slot. Populated by the blank-calendar generator
-- (Phase 2 of this feature) and mutated by the exception reconciler
-- (Phase 3) and the scheduling engine (Phase 4 / orders work).
--
-- Shape rationale:
--   * Multi-tenant by client_id (FK to pc1.clients). All three of
--     client_id / facility_id / modality_id are NOT NULL — every slot
--     belongs to a real machine at a real facility for a real tenant.
--     No "global slot" tier (unlike proceduresestimate's 4-shape
--     override pattern).
--   * date_and_time is timestamptz storing UTC. The legacy public-
--     schema table stored timestamp-without-tz and was later migrated
--     to UTC; pc1 ships UTC-from-day-one so the type itself encodes
--     the contract. The generator iterates facility-local wall clock
--     (pc1.facilities.timezone) and converts to UTC at write time via
--     Python's zoneinfo (DST handled automatically).
--   * Slot-window resolution is per-(client, facility):
--       slot_size:            pc1.facilities.slot_size (override) ->
--                             pc1.clients.slot_size (default)
--       opening/closing_time: pc1.facilities.opening_time/closing_time
--                             (override) -> pc1.clients values (default)
--       advance_booking_days: pc1.facilities.advance_booking_days
--                             (override) -> pc1.clients value (default)
--       timezone:             pc1.facilities.timezone (NOT NULL)
--     The generator does the merge in Python; the schema doesn't
--     enforce it.
--   * exceptions text[] and exception_ids text[] are paired
--     positionally — element N of exception_ids is a
--     pc1.scheduleexceptions.source_record_key whose human label sits
--     at element N of exceptions. The reconciler is the only writer
--     of these columns and maintains the invariant. Postgres can't
--     enforce paired-length without a trigger; accept the discipline.
--   * order_id is a bigint with NO FK constraint yet — pc1.orders
--     doesn't exist at the time of this migration. Phase 4 (orders
--     work) will ALTER TABLE to add the FK. Until then, the column is
--     populated only by future booking code, never by the generator
--     or the reconciler.
--   * Several legacy columns are intentionally NOT carried forward:
--       - modality_type / modality_machine / facility (text) -> replaced
--         by the modality_id / facility_id FKs
--       - cumm_open_slots_below / cumm_open_slots_above -> stale-data
--         footgun; recompute in the engine
--       - temp_reservation_id -> no documented use; remove
--       - is_active -> slots aren't soft-deleted (no upstream RIS
--         source); would be 100% true forever
--       - source_record_key / content_hash / ris_* / synced_at ->
--         slots are generator-produced, not RIS-scraped, so the
--         source-tracking quintet is meaningless here
--   * Several legacy columns ARE preserved on the user's request even
--     though they're not actively written today:
--       - seq, start_time, end_time -> referenced by the legacy
--         scheduling engine
--       - scheduled -> reserved for future booking state alongside
--         availability
--       - capacity -> defaults to 1, kept for future multi-patient
--         slot support
--
-- Cross-tenant safety:
--   A BEFORE INSERT/UPDATE trigger validates that client_id matches
--   the FK'd facility's client_id and the FK'd modality's client_id.
--   Same shape as pc1.proceduresestimate and pc1.scheduleexceptions.
--   The OF client_id, facility_id, modality_id clause skips the
--   trigger on hot-path UPDATEs (the reconciler updating availability
--   + arrays, the engine updating order_id).
--
-- Written to be idempotent so it can be applied to a fresh DB or
-- replayed safely on one where pc1.machineschedule already exists.

-- ── Cross-tenant consistency trigger function ──────────────────────────────

CREATE OR REPLACE FUNCTION pc1.check_machineschedule_consistency()
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
                'machineschedule.facility_id=% references a row that does not exist in pc1.facilities',
                NEW.facility_id;
        END IF;
        IF fac_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'machineschedule.client_id=% does not match pc1.facilities(id=%).client_id=%',
                NEW.client_id, NEW.facility_id, fac_client_id;
        END IF;
    END IF;

    IF NEW.modality_id IS NOT NULL THEN
        SELECT client_id INTO mod_client_id
          FROM pc1.modalities
         WHERE id = NEW.modality_id;
        IF mod_client_id IS NULL THEN
            RAISE EXCEPTION
                'machineschedule.modality_id=% references a row that does not exist in pc1.modalities',
                NEW.modality_id;
        END IF;
        IF mod_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'machineschedule.client_id=% does not match pc1.modalities(id=%).client_id=%',
                NEW.client_id, NEW.modality_id, mod_client_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── Table ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pc1.machineschedule (
    id                          bigserial    NOT NULL,
    client_id                   bigint       NOT NULL,
    facility_id                 bigint       NOT NULL,
    modality_id                 bigint       NOT NULL,

    -- Slot identity / ordering
    seq                         bigint                       NULL,
    slot_seq                    integer                      NULL,
    date_and_time               timestamp with time zone     NOT NULL,
    start_time                  time without time zone       NULL,
    end_time                    time without time zone       NULL,

    -- Slot state
    capacity                    integer                      NULL,
    availability                integer                      NULL,
    scheduled                   integer                      NULL,

    -- Exception overlays (reconciler-maintained, paired positionally)
    exceptions                  text[]       NOT NULL DEFAULT '{}'::text[],
    exception_ids               text[]       NOT NULL DEFAULT '{}'::text[],

    -- Order linkage (FK to pc1.orders deferred to Phase 4)
    order_id                    bigint                       NULL,

    -- Audit (matches pc1.proceduresestimate / pc1.scheduleexceptions)
    created_at                  timestamp with time zone NOT NULL DEFAULT now(),
    updated_at                  timestamp with time zone NOT NULL DEFAULT now(),
    created_by                  bigint                       NULL,
    updated_by                  bigint                       NULL,

    CONSTRAINT machineschedule_pkey
        PRIMARY KEY (id),
    CONSTRAINT machineschedule_client_id_fkey
        FOREIGN KEY (client_id)   REFERENCES pc1.clients(id),
    CONSTRAINT machineschedule_facility_id_fkey
        FOREIGN KEY (facility_id) REFERENCES pc1.facilities(id),
    CONSTRAINT machineschedule_modality_id_fkey
        FOREIGN KEY (modality_id) REFERENCES pc1.modalities(id),
    CONSTRAINT fk_ms_created_by
        FOREIGN KEY (created_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL,
    CONSTRAINT fk_ms_updated_by
        FOREIGN KEY (updated_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL,
    CONSTRAINT machineschedule_business_key
        UNIQUE (client_id, facility_id, modality_id, date_and_time)
) TABLESPACE pg_default;

-- ── Indexes ────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_ms_client_id
    ON pc1.machineschedule USING btree (client_id) TABLESPACE pg_default;

-- Reconciler hot read path: "all slots for this facility in this date range"
CREATE INDEX IF NOT EXISTS idx_ms_client_facility_dt
    ON pc1.machineschedule USING btree (client_id, facility_id, date_and_time)
    TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_ms_modality_id
    ON pc1.machineschedule USING btree (modality_id) TABLESPACE pg_default;

-- Engine lookup: "which slots are booked against this order?"
-- Partial because the overwhelming majority of slots have order_id IS NULL.
CREATE INDEX IF NOT EXISTS idx_ms_order_id
    ON pc1.machineschedule USING btree (order_id) TABLESPACE pg_default
    WHERE order_id IS NOT NULL;

-- "All slots affected by exception X" — array containment via GIN.
-- Matches the legacy public.machineschedule shape.
CREATE INDEX IF NOT EXISTS idx_ms_exception_ids
    ON pc1.machineschedule USING gin (exception_ids) TABLESPACE pg_default;

-- ── Trigger ────────────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS trg_machineschedule_consistency_check
    ON pc1.machineschedule;

CREATE TRIGGER trg_machineschedule_consistency_check
    BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id
    ON pc1.machineschedule
    FOR EACH ROW
    EXECUTE FUNCTION pc1.check_machineschedule_consistency();
