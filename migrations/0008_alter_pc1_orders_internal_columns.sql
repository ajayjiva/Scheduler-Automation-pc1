-- pc1.orders: add internal/logic columns for the Phase 3.5 compatibility layer.
--
-- pc1.orders already exists as a RIS-shaped table (ris_order_id, ris_order_status,
-- ris_order_type, ris_requesting_date, ... + the sync quintet). This migration
-- adds the PLAIN internal columns that pc1.orders_v reads, following the same
-- ris_* (raw source) vs plain (internal/cleaned) convention established on
-- pc1.patients (migration 0006):
--
--   plain column            <- raw source
--   order_status            <- ris_order_status
--   order_type              <- ris_order_type
--   requesting_date         <- ris_requesting_date
--   procedure_description   <- ris_procedure_description (both added here)
--   preferred_language      <- (internal-only; no RIS source)
--   is_active               <- (internal soft-delete; no RIS source)
--
-- Notes:
--   * cpt_codes is eliminated and the studies table is not used for scheduling.
--     Orders link to the catalog via procedure_description = proceduresestimate.
--     procedure_desc (EXACT text). pc1.orders carried no procedure column at
--     all, so we add ris_procedure_description (raw) + procedure_description
--     (plain join key).
--   * requesting_date is a facility-LOCAL date. When set on an order, the
--     scheduling engine (Phase 3.6) pins its search start-floor to that local
--     date instead of "now" (e.g. "requesting date is 15 days out" -> only look
--     at that day). Date, not timestamp -- time-of-day isn't meaningful here.
--   * order_status / order_type vocabularies are TBD -> no CHECK constraints.
--     order_type is expected to be NovaRIS 'P'/'S' (meaning unconfirmed).
--   * A cross-tenant safety trigger (validating client_id against the FK'd
--     facility AND patient) was absent on pc1.orders; this migration adds it to
--     match the sibling tables.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE FUNCTION, DROP
-- TRIGGER IF EXISTS before CREATE TRIGGER, replay-safe backfill.

-- -- Columns ------------------------------------------------------------------

ALTER TABLE pc1.orders
    ADD COLUMN IF NOT EXISTS ris_procedure_description text         NULL,  -- raw source (procedure text)
    ADD COLUMN IF NOT EXISTS procedure_description     text         NULL,  -- plain join key -> proceduresestimate.procedure_desc
    ADD COLUMN IF NOT EXISTS preferred_language        varchar(50)  NULL,  -- internal-only
    ADD COLUMN IF NOT EXISTS order_status              varchar(50)  NULL,  -- <- ris_order_status (vocabulary TBD)
    ADD COLUMN IF NOT EXISTS order_type                varchar(20)  NULL,  -- <- ris_order_type ('P'/'S' TBD)
    ADD COLUMN IF NOT EXISTS requesting_date           date         NULL,  -- <- ris_requesting_date (facility-local)
    ADD COLUMN IF NOT EXISTS is_active                 boolean      NOT NULL DEFAULT true;

-- -- Backfill plain columns from their raw ris_* sources -----------------------
-- Replay-safe. Does NOT touch updated_at (structural backfill, not a sync write).
-- preferred_language and is_active have no raw source (DEFAULT handles is_active).
UPDATE pc1.orders SET
    order_status          = ris_order_status,
    order_type            = ris_order_type,
    requesting_date       = ris_requesting_date,
    procedure_description = ris_procedure_description;

-- -- Cross-tenant consistency trigger -----------------------------------------

CREATE OR REPLACE FUNCTION pc1.check_orders_consistency()
RETURNS trigger AS $$
DECLARE
    fac_client_id bigint;
    pat_client_id bigint;
BEGIN
    IF NEW.facility_id IS NOT NULL THEN
        SELECT client_id INTO fac_client_id
          FROM pc1.facilities WHERE id = NEW.facility_id;
        IF fac_client_id IS NULL THEN
            RAISE EXCEPTION
                'orders.facility_id=% references a row that does not exist in pc1.facilities',
                NEW.facility_id;
        END IF;
        IF fac_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'orders.client_id=% does not match pc1.facilities(id=%).client_id=%',
                NEW.client_id, NEW.facility_id, fac_client_id;
        END IF;
    END IF;

    IF NEW.patient_id IS NOT NULL THEN
        SELECT client_id INTO pat_client_id
          FROM pc1.patients WHERE id = NEW.patient_id;
        IF pat_client_id IS NULL THEN
            RAISE EXCEPTION
                'orders.patient_id=% references a row that does not exist in pc1.patients',
                NEW.patient_id;
        END IF;
        IF pat_client_id <> NEW.client_id THEN
            RAISE EXCEPTION
                'orders.client_id=% does not match pc1.patients(id=%).client_id=%',
                NEW.client_id, NEW.patient_id, pat_client_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_orders_consistency_check ON pc1.orders;

CREATE TRIGGER trg_orders_consistency_check
    BEFORE INSERT OR UPDATE OF client_id, facility_id, patient_id
    ON pc1.orders
    FOR EACH ROW
    EXECUTE FUNCTION pc1.check_orders_consistency();

-- -- Indexes ------------------------------------------------------------------
-- Supports the orders_v join (procedure_description) and active-order filtering.
CREATE INDEX IF NOT EXISTS idx_orders_procedure_description
    ON pc1.orders USING btree (procedure_description) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_orders_active
    ON pc1.orders USING btree (is_active) TABLESPACE pg_default
    WHERE is_active = true;
