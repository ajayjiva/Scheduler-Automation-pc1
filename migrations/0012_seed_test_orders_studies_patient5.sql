-- =====================================================================
-- TEST / NON-PROD SEED DATA -- orders + studies for patient_id = 5
-- =====================================================================
-- NOT part of the production migration sequence. Adds 3 pc1.orders rows
-- and 4 pc1.studies rows for the existing TEST-P05 patient (Emma Rose
-- Patel, client_id 1, facility_id 3 -- Antioch Medical Imaging), seeded
-- by migrations/0010_seed_test_orders.sql. Safe to run only against a
-- dev/test database, and only after 0010 has been applied.
--
-- All seed rows are tagged for clean removal:
--   * orders  -> ris_order_id BETWEEN 1001 AND 1003 (client_id = 1)
--   * studies -> cascade-deleted automatically via studies_order_id_fkey
--                (ON DELETE CASCADE) when the orders above are removed
--
-- NOTE on ris_study_id: the source data handed to us listed 0 for every
-- study row, but pc1.studies has UNIQUE (client_id, ris_study_id), so
-- four rows of ris_study_id = 0 under client_id = 1 would collide. This
-- script substitutes distinct placeholder values (800001-800004) instead
-- -- flag/replace these if real RIS study ids are available.
--
-- NOTE on ris_study_status: pc1.studies.ris_study_status is varchar(10).
-- 'Unscheduled' (11 chars) overflows it, so this script uses 'Unsched'
-- instead -- swap in the real RIS vocabulary once it's confirmed.
--
-- This script is IDEMPOTENT: it deletes any prior seed rows (by the
-- ris_order_id tag) before re-inserting, so it can be replayed.
--
-- To remove all seed data from this script:
--   DELETE FROM pc1.orders WHERE client_id = 1 AND ris_order_id BETWEEN 1001 AND 1003;
-- =====================================================================

-- -- 0. Idempotent cleanup (studies cascade-delete via order_id FK) --------
DELETE FROM pc1.orders WHERE client_id = 1 AND ris_order_id BETWEEN 1001 AND 1003;

-- -- 1. Orders + studies, chained via RETURNING/CTE so studies.order_id ---
-- --    always points at the surrogate id actually assigned above,
-- --    rather than a hardcoded guess.
WITH ins_orders AS (
    INSERT INTO pc1.orders
        (client_id, ris_order_id, patient_id, facility_id,
         ris_order_status, ris_order_type,
         order_status, order_type,
         created_at, updated_at, is_active)
    VALUES
        (1, 1001, 5, 3, 'Ordered', 'P', 'Ordered', 'P',
         '2026-06-06 19:30:14.802489+00', '2026-06-06 19:30:14.802489+00', true),
        (1, 1002, 5, 3, 'Ordered', 'P', 'Ordered', 'P',
         '2026-06-06 19:30:14.802489+00', '2026-06-06 19:30:14.802489+00', true),
        (1, 1003, 5, 3, 'Ordered', 'P', 'Ordered', 'P',
         '2026-06-06 19:30:14.802489+00', '2026-06-06 19:30:14.802489+00', true)
    RETURNING id, ris_order_id
)
INSERT INTO pc1.studies
    (client_id, ris_study_id, order_id, facility_id,
     ris_study_status, ris_study_description, ris_cpt_code, ris_modality, ris_duration)
SELECT
    1, v.ris_study_id, io.id, 3,
    'Unsched', v.ris_study_description, v.ris_cpt_code, v.ris_modality, 30
FROM (VALUES
    -- (ris_order_id, ris_study_id, description, cpt, modality)
    (1001::bigint, 800001::bigint, 'MRI - LSPINE (72148)', '{72148}', 'MR'),
    (1002::bigint, 800002::bigint, 'MRI - TSPINE (72146)', '{72146}', 'MR'),
    (1003::bigint, 800003::bigint, 'CT - CSPINE W/ CONTRAST (72126)', '{72126}', 'CT'),
    (1003::bigint, 800004::bigint, 'US - ABDOMINAL DOPPLER LIMITED (93976)', '{93976}', 'US')
) AS v(ris_order_id, ris_study_id, ris_study_description, ris_cpt_code, ris_modality)
JOIN ins_orders io ON io.ris_order_id = v.ris_order_id;
