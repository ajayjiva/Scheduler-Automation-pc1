-- pc1.orders: STUB order table for the Phase 3.5 compatibility layer.
--
-- Purpose: give pc1.orders_v an underlying table so the scheduling-engine port
-- (Phase 3.6) can be built and tested NOW, before the team finalizes the real
-- orders schema (Phase 4). When the real schema lands, this table is REPLACED;
-- pc1.orders_v's column contract stays stable so the engine code is unaffected.
--
-- Design notes (see docs/orders.md for the full write-up):
--   * cpt_codes table is ELIMINATED. Orders link to the procedure catalog via
--     procedure_description = pc1.proceduresestimate.procedure_desc (EXACT text
--     match). The studies table is NOT used for scheduling.
--   * No procedure_code column here -- procedure_code is sourced from
--     proceduresestimate via the view.
--   * ris_* vs plain columns: when the real RIS-sourced orders schema lands it
--     will carry ris_order_status / ris_order_type / ris_requesting_date (raw)
--     plus these plain columns (internal/cleaned). The stub only needs the
--     plain columns the view reads, so that is all we create now.
--   * order_status and order_type vocabularies are TBD -> no CHECK constraints
--     yet. order_type is expected to be NovaRIS 'P' / 'S' (meaning unconfirmed).
--   * Cross-tenant safety trigger validates client_id against the FK'd facility
--     AND patient (matches the sibling tables proceduresestimate /
--     scheduleexceptions).
--
-- Idempotent: CREATE OR REPLACE FUNCTION, CREATE TABLE IF NOT EXISTS,
-- CREATE INDEX IF NOT EXISTS, DROP TRIGGER IF EXISTS before CREATE TRIGGER.

-- -- Cross-tenant consistency trigger function --------------------------------

CREATE OR REPLACE FUNCTION pc1.check_orders_consistency()
RETURNS trigger AS $$
DECLARE
    fac_client_id bigint;
    pat_client_id bigint;
BEGIN
    IF NEW.facility_id IS NOT NULL THEN
        SELECT client_id INTO fac_client_id
          FROM pc1.facilities
         WHERE id = NEW.facility_id;
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
          FROM pc1.patients
         WHERE id = NEW.patient_id;
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

-- -- Table --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pc1.orders (
    id                    bigserial    NOT NULL,
    client_id             bigint       NOT NULL,   -- no DEFAULT (multi-tenant footgun)
    facility_id           bigint       NOT NULL,
    patient_id            bigint       NOT NULL,

    -- Business fields (plain / internal; raw ris_* counterparts arrive in Phase 4)
    procedure_description text         NOT NULL,    -- exact-match join key to proceduresestimate.procedure_desc
    preferred_language    varchar(50)  NULL,
    order_status          varchar(50)  NULL,        -- vocabulary TBD; no CHECK yet
    order_type            varchar(20)  NULL,        -- NovaRIS 'P'/'S'; meaning TBD; no CHECK yet
    requesting_date       date         NULL,        -- date-only for the stub; widen to timestamptz if the RIS carries a time

    -- Soft-delete
    is_active             boolean      NOT NULL DEFAULT true,

    -- Audit (matches sibling tables)
    created_at            timestamp with time zone NOT NULL DEFAULT now(),
    updated_at            timestamp with time zone NOT NULL DEFAULT now(),
    created_by            bigint       NULL,
    updated_by            bigint       NULL,

    CONSTRAINT orders_pkey
        PRIMARY KEY (id),
    CONSTRAINT orders_client_id_fkey
        FOREIGN KEY (client_id)   REFERENCES pc1.clients(id),
    CONSTRAINT orders_facility_id_fkey
        FOREIGN KEY (facility_id) REFERENCES pc1.facilities(id),
    CONSTRAINT orders_patient_id_fkey
        FOREIGN KEY (patient_id)  REFERENCES pc1.patients(id),
    CONSTRAINT fk_orders_created_by
        FOREIGN KEY (created_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL,
    CONSTRAINT fk_orders_updated_by
        FOREIGN KEY (updated_by)  REFERENCES pc1.user_profiles(id) ON DELETE SET NULL
) TABLESPACE pg_default;

-- -- Indexes ------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_orders_client_id
    ON pc1.orders USING btree (client_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_orders_facility_id
    ON pc1.orders USING btree (facility_id) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_orders_patient_id
    ON pc1.orders USING btree (patient_id) TABLESPACE pg_default;

-- Supports the orders_v join: orders.procedure_description = pe.procedure_desc.
CREATE INDEX IF NOT EXISTS idx_orders_procedure_description
    ON pc1.orders USING btree (procedure_description) TABLESPACE pg_default;

CREATE INDEX IF NOT EXISTS idx_orders_active
    ON pc1.orders USING btree (is_active) TABLESPACE pg_default
    WHERE is_active = true;

-- -- Trigger ------------------------------------------------------------------

DROP TRIGGER IF EXISTS trg_orders_consistency_check ON pc1.orders;

CREATE TRIGGER trg_orders_consistency_check
    BEFORE INSERT OR UPDATE OF client_id, facility_id, patient_id
    ON pc1.orders
    FOR EACH ROW
    EXECUTE FUNCTION pc1.check_orders_consistency();
