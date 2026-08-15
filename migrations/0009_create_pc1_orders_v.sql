-- pc1.orders_v: Phase 3.5 compatibility-layer view.
--
-- A REGULAR (non-materialized) view. Postgres rewrites it inline at query time,
-- so there is no performance penalty vs writing the joins by hand, provided we
-- avoid DISTINCT ON / aggregation and keep the join columns indexed (see
-- session-handoff.md 8.1). Do NOT convert to MATERIALIZED / function-based
-- without re-discussing.
--
-- Shape (decisions locked in session-handoff.md 8.2):
--   * MULTI-ROW per order: one row per matching proceduresestimate shape. NO
--     DISTINCT ON. per_machine_resolver.py resolves the 4-tier precedence in
--     Python from these candidate rows.
--   * FK columns (facility_id / modality_id / pe_facility_id), not legacy text
--     names. pe_facility_id is the procedure's facility -- CONFIRMED used by
--     per_machine_resolver._row_tier() (compares pe_facility to the order's
--     facility for the per-facility override tier).
--   * procedure_description kept as text (display + the catalog join key).
--   * stat_order / machine_skill / contrast_skill DEFERRED (decision #5). The
--     legacy engine references these keys but only debug-prints them; the 3.6
--     port softens those subscripts to .get(). Add the columns to this view if
--     and when real scheduling logic consumes them.
--
-- Join scoping = "tenant + active" (user's choice): match client_id and require
-- is_active on all three sources. No facility-tier filter in SQL -- the Python
-- resolver owns facility/machine precedence, so every matching pe shape (global
-- + facility + per-machine) must reach it.
--
-- cpt_codes is ELIMINATED: orders link to the catalog via
-- procedure_description = procedure_desc (exact text). studies is not used.
--
-- ris_* vs plain: the view reads the PLAIN patient columns (patient_full_name /
-- patient_dob / language), which hold internal/cleaned values; the raw ris_*
-- columns remain the source of truth on pc1.patients.
--
-- 3.6 engine-port rename map (legacy orders_v key -> pc1 view column):
--   facility    -> facility_id     | module_type -> modality_type
--   module      -> modality_id     | pe_facility -> pe_facility_id
-- In pc1 the resolver's tier comparisons become ID==ID (cleaner than the legacy
-- text==text); the machine inventory (derive_machines_from_machineschedule)
-- must key on modality_id to match.
--
-- Idempotent: CREATE OR REPLACE VIEW.

CREATE OR REPLACE VIEW pc1.orders_v AS
SELECT
    o.id                    AS order_id,
    o.client_id             AS client_id,
    o.facility_id           AS facility_id,
    o.patient_id            AS patient_id,
    p.patient_full_name     AS patient_full_name,   -- plain (cleaned) name
    p.patient_dob           AS patient_dob,         -- plain (cleaned) DOB
    p.language              AS patient_language,     -- plain (cleaned) language
    o.preferred_language    AS preferred_language,
    o.order_status          AS order_status,
    o.order_type            AS order_type,
    o.requesting_date       AS requesting_date,
    o.procedure_description AS procedure_description,
    pe.procedure_code       AS procedure_code,       -- text[] from the catalog
    pe.facility_id          AS pe_facility_id,       -- procedure's facility: per-facility override tier
    pe.modality_id          AS modality_id,          -- procedure's machine: per-machine override tier
    pe.modality_type        AS modality_type,
    pe.required_slots       AS required_slots,
    pe.required_time        AS required_time
FROM pc1.orders o
JOIN pc1.patients p
    ON  p.id            = o.patient_id
    AND p.client_id     = o.client_id        -- tenant scope
    AND p.ris_is_active = true                -- active (source flag; no plain is_active on patients)
JOIN pc1.proceduresestimate pe
    ON  pe.procedure_desc = o.procedure_description
    AND pe.client_id      = o.client_id       -- tenant scope
    AND pe.is_active      = true              -- active
WHERE o.is_active = true;
