-- =====================================================================
-- TEST / NON-PROD SEED DATA -- Phase 3.5 (pc1.orders_v verification)
-- =====================================================================
-- NOT part of the production migration sequence. Hand-crafted patients,
-- orders, and a couple of synthesized proceduresestimate override rows to
-- exercise pc1.orders_v across all the shapes the scheduling engine cares
-- about. Safe to run only against a dev/test database.
--
-- All seed rows are tagged for clean removal:
--   * patients          -> ris_account_no LIKE 'TEST-P%'
--   * orders            -> ris_order_id BETWEEN 900001 AND 900099
--   * override pe rows   -> ris_metadata->>'seed' = '0010_seed_test_orders'
--
-- This script is IDEMPOTENT: it deletes any prior seed rows (by those tags)
-- before re-inserting, so it can be replayed.
--
-- Tenant: client_id = 1 (Inview Imaging). Facilities: 2 = Inview-Fremont,
-- 3 = Antioch Medical Imaging. Real CT machine at Antioch: CT-AMI (modality_id 2).
--
-- To remove all seed data:
--   DELETE FROM pc1.orders   WHERE ris_order_id BETWEEN 900001 AND 900099;
--   DELETE FROM pc1.patients WHERE ris_account_no LIKE 'TEST-P%';
--   DELETE FROM pc1.proceduresestimate
--          WHERE ris_metadata->>'seed' = '0010_seed_test_orders';
-- =====================================================================

-- -- 0. Idempotent cleanup (orders first: FK -> patients) ------------------
DELETE FROM pc1.orders   WHERE ris_order_id BETWEEN 900001 AND 900099;
DELETE FROM pc1.patients WHERE ris_account_no LIKE 'TEST-P%';
DELETE FROM pc1.proceduresestimate
       WHERE ris_metadata->>'seed' = '0010_seed_test_orders';

-- -- 1. Synthesized override catalog rows ----------------------------------
-- Two extra shapes for the global procedure 'CT - ABDOMEN (74150)' (global
-- required_slots=2), each with a DIFFERENT required_slots so per_machine_resolver
-- (Phase 3.6) has real tier variation to resolve. source_record_key NULL keeps
-- them out of the scraper's unique index and protected from scraper wipes
-- (initial-load only deletes source_record_key IS NOT NULL rows).
INSERT INTO pc1.proceduresestimate
    (client_id, facility_id, modality_id, modality_type, procedure_code,
     procedure_desc, required_time, required_slots, is_active,
     source_record_key, content_hash, ris_metadata)
VALUES
    -- facility-level override (Antioch, any machine)
    (1, 3, NULL, 'CT', ARRAY['74150']::text[],
     'CT - ABDOMEN (74150)', NULL, 3, true,
     NULL, NULL, '{"seed":"0010_seed_test_orders","tier":"facility"}'::jsonb),
    -- per-(facility, machine) override (Antioch, CT-AMI = modality_id 2)
    (1, 3, 2, 'CT', ARRAY['74150']::text[],
     'CT - ABDOMEN (74150)', NULL, 4, true,
     NULL, NULL, '{"seed":"0010_seed_test_orders","tier":"facility_machine"}'::jsonb);

-- -- 2. Test patients ------------------------------------------------------
-- ris_* are the raw source values; the plain columns (added in 0006) are the
-- straight copy used by internal logic. Required NOT NULL ris_* fields:
-- ris_first_name, ris_last_name, ris_birth_date, ris_gender.
INSERT INTO pc1.patients
    (client_id, ris_account_no, facility_id,
     ris_first_name, ris_middle_name, ris_last_name, ris_full_name,
     ris_birth_date, ris_gender, ris_language,
     first_name, middle_name, last_name, patient_full_name,
     patient_dob, language, ris_metadata)
VALUES
    (1, 'TEST-P01', 2, 'Alice', 'Marie', 'Nguyen', 'Alice Marie Nguyen',
     '1980-03-15', 'F', 'English',
     'Alice', 'Marie', 'Nguyen', 'Alice Marie Nguyen',
     '1980-03-15', 'English', '{"seed":"0010_seed_test_orders"}'::jsonb),
    (1, 'TEST-P02', 2, 'Bob', 'A', 'Carter', 'Bob A Carter',
     '1975-07-22', 'M', 'English',
     'Bob', 'A', 'Carter', 'Bob A Carter',
     '1975-07-22', 'English', '{"seed":"0010_seed_test_orders"}'::jsonb),
    (1, 'TEST-P03', 3, 'Carla', 'Sofia', 'Ruiz', 'Carla Sofia Ruiz',
     '1990-11-02', 'F', 'Spanish',
     'Carla', 'Sofia', 'Ruiz', 'Carla Sofia Ruiz',
     '1990-11-02', 'Spanish', '{"seed":"0010_seed_test_orders"}'::jsonb),
    (1, 'TEST-P04', 2, 'David', NULL, 'Lee', 'David Lee',
     '1968-01-30', 'M', 'English',
     'David', NULL, 'Lee', 'David Lee',
     '1968-01-30', 'English', '{"seed":"0010_seed_test_orders"}'::jsonb),
    (1, 'TEST-P05', 3, 'Emma', 'Rose', 'Patel', 'Emma Rose Patel',
     '1985-05-12', 'F', 'English',
     'Emma', 'Rose', 'Patel', 'Emma Rose Patel',
     '1985-05-12', 'English', '{"seed":"0010_seed_test_orders"}'::jsonb),
    (1, 'TEST-P06', 3, 'Frank', 'Owen', 'Diaz', 'Frank Owen Diaz',
     '1972-09-09', 'M', 'Spanish',
     'Frank', 'Owen', 'Diaz', 'Frank Owen Diaz',
     '1972-09-09', 'Spanish', '{"seed":"0010_seed_test_orders"}'::jsonb),
    (1, 'TEST-P07', 2, 'Grace', NULL, 'Lin', 'Grace Lin',
     '1995-12-25', 'F', 'English',
     'Grace', NULL, 'Lin', 'Grace Lin',
     '1995-12-25', 'English', '{"seed":"0010_seed_test_orders"}'::jsonb),
    (1, 'TEST-P08', 2, 'Henry', 'James', 'Cole', 'Henry James Cole',
     '1960-04-18', 'M', 'English',
     'Henry', 'James', 'Cole', 'Henry James Cole',
     '1960-04-18', 'English', '{"seed":"0010_seed_test_orders"}'::jsonb);

-- -- 3. Test orders --------------------------------------------------------
-- patient_id resolved by joining the VALUES list to the seeded patients on
-- ris_account_no. For each order the plain and ris_* business columns are set
-- equal (raw == cleaned for synthetic data) so the 0008 backfill-consistency
-- check stays clean.
INSERT INTO pc1.orders
    (client_id, ris_order_id, patient_id, facility_id,
     procedure_description, ris_procedure_description,
     order_status, ris_order_status, order_type, ris_order_type,
     preferred_language, requesting_date, ris_requesting_date, is_active)
SELECT
    v.client_id, v.ris_order_id, p.id, v.facility_id,
    v.procedure_description, v.procedure_description,
    v.order_status, v.order_status, v.order_type, v.order_type,
    v.preferred_language, v.requesting_date, v.requesting_date, true
FROM (VALUES
    -- (client, ris_order_id, acct, facility, procedure_description, status, type, pref_lang, requesting_date)
    -- P01: single-modality, single-CPT, global
    (1::bigint, 900001::bigint, 'TEST-P01', 2::bigint,
     'CT - ABDOMEN/PELVIS (74176)', 'Ordered', 'P', NULL::varchar, NULL::date),
    -- P02: multi-CPT (procedure_code array length 2), global
    (1, 900002, 'TEST-P02', 2,
     'XR - ABDOMEN/KUB/PELVIS COMBO (74018,72170)', 'Ordered', 'P', NULL, NULL),
    -- P03: multi-modality (CT + CR), global
    (1, 900003, 'TEST-P03', 3,
     'CT - ABDOMEN/PELVIS (74176)', 'Ordered', 'P', 'Spanish', NULL),
    (1, 900004, 'TEST-P03', 3,
     'XR - ABDOMEN/KUB/PELVIS COMBO (74018,72170)', 'Ordered', 'S', 'Spanish', NULL),
    -- P04: duplicate-global -> view returns 2 rows (proves no DISTINCT ON)
    (1, 900005, 'TEST-P04', 2,
     'XR - FINGER(s), 2+ VIEWS, LEFT (73140)', 'Ordered', 'P', NULL, NULL),
    -- P05: per-facility + per-machine override base (global + 2 synthesized = 3 rows)
    (1, 900006, 'TEST-P05', 3,
     'CT - ABDOMEN (74150)', 'Ordered', 'P', NULL, NULL),
    -- P06: single-modality + a future requesting_date (start-floor override demo)
    (1, 900007, 'TEST-P06', 3,
     'CT - ABDOMEN W/ CONTRAST (74160)', 'Ordered', 'S', 'Spanish', '2026-06-21'),
    -- P07: single-modality, global (Fremont)
    (1, 900008, 'TEST-P07', 2,
     'CT - CALCIUM SCORING (75571)', 'Ordered', 'P', NULL, NULL),
    -- P08: multi-modality (CT + multi-CPT CR), global (Fremont)
    (1, 900009, 'TEST-P08', 2,
     'CT - ABDOMEN/PELVIS (74176)', 'Ordered', 'P', NULL, NULL),
    (1, 900010, 'TEST-P08', 2,
     'XR - AC JNTS  W W/O WTS BIL (73050, 73050)', 'Ordered', 'S', NULL, NULL)
) AS v(client_id, ris_order_id, acct, facility_id,
       procedure_description, order_status, order_type, preferred_language, requesting_date)
JOIN pc1.patients p
  ON p.ris_account_no = v.acct
 AND p.client_id      = v.client_id;
