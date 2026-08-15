-- pc1.machineschedule_v: Phase 3.6 engine compatibility-layer view.
--
-- WHY THIS VIEW EXISTS
-- --------------------
-- pc1.machineschedule deliberately dropped the legacy text columns
-- modality_type / modality_machine / facility and the un-suffixed
-- date_and_time (see migrations 0004 lines 40-44 and 0005). It keeps only
-- the modality_id / facility_id FKs and date_and_time_utc. The scheduling
-- engine ported in Phase 3.6, however, reads modality_type and
-- modality_machine on every slot row (modality grouping + the patient-facing
-- "On Machine" line). This view rejoins that machine metadata from
-- pc1.modalities so the engine can read legacy-shaped rows without a
-- per-row Python lookup -- the same compatibility-layer approach used for
-- pc1.orders_v (migration 0009).
--
-- SHAPE
-- -----
--   * One row per machineschedule slot (straight INNER JOIN on modality_id;
--     the FK guarantees exactly one modalities match, so the view neither
--     adds nor drops rows).
--   * modality_type / modality_machine come from pc1.modalities.
--   * date_and_time_utc is exposed under its real name -- the engine port
--     reads `date_and_time_utc` (not the legacy `date_and_time`).
--   * modality_id is exposed so the per-machine resolver can key candidate
--     machines by the FK int (matching orders_v.modality_id, the override
--     pin) rather than the machine-name text. modality_machine remains for
--     display only.
--
-- SCOPING
-- -------
-- No is_active filter on pc1.modalities: the generator only creates slots
-- for active modalities, and slot availability (governed by the generator +
-- reconciler) is the real bookability signal. A slot present here was
-- schedulable when generated. Tenant scoping is the caller's job (the engine
-- filters client_id + facility_id), matching how it queries the base table.
--
-- READ-ONLY: booking writes (order_id, availability) still target the base
-- table in Phase 4. This view is for the engine's read path only.
--
-- Idempotent: CREATE OR REPLACE VIEW.

CREATE OR REPLACE VIEW pc1.machineschedule_v AS
SELECT
    ms.id                 AS id,
    ms.client_id          AS client_id,
    ms.facility_id        AS facility_id,
    ms.modality_id        AS modality_id,          -- per-machine key (matches orders_v.modality_id)
    m.modality_type       AS modality_type,        -- from pc1.modalities (was a column on legacy machineschedule)
    m.modality_machine    AS modality_machine,     -- display name (was a column on legacy machineschedule)
    ms.seq                AS seq,                   -- facility-local YYYYMMDDHHMMSS (engine str(seq)[:8])
    ms.slot_seq           AS slot_seq,
    ms.date_and_time_utc  AS date_and_time_utc,     -- true UTC; engine converts via facility tz
    ms.start_time         AS start_time,            -- facility-local TIME (business-hours filter)
    ms.end_time           AS end_time,              -- facility-local TIME
    ms.availability       AS availability,
    ms.capacity           AS capacity,
    ms.scheduled          AS scheduled,
    ms.order_id           AS order_id
FROM pc1.machineschedule ms
JOIN pc1.modalities m
    ON m.id = ms.modality_id;
