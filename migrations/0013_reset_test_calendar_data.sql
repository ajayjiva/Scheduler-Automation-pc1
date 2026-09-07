-- =====================================================================
-- TEST / NON-PROD DATA -- calendar reset + exceptions + booked slots
-- =====================================================================
-- NOT part of the production migration sequence. Supports end-to-end
-- testing of the study-array input path (get_studies.py / main.py
-- --studies-file). Safe to run only against a dev/test database.
--
-- This is a THREE-STEP process. Steps 1 and 3 are plain SQL (below).
-- Step 2 (regenerating the calendar) is a Python CLI invocation, since
-- generate_machineschedule.py owns the slot-generation logic -- do not
-- hand-write pc1.machineschedule rows.
--
-- Defaults below assume client_id = 1, facility_id = 3 (Antioch Medical
-- Imaging) -- adjust if testing a different tenant/facility. Look up real
-- modality_id values first:
--
--   SELECT id, modality_type, modality_machine
--   FROM pc1.modalities
--   WHERE client_id = 1 AND facility_id = 3 AND is_active = true;
--
-- =====================================================================

-- -- STEP 1: wipe future calendar rows for this facility -----------------
-- generate_machineschedule.py upserts with ignore_duplicates=true, so it
-- will NOT overwrite existing rows -- a true reset needs this DELETE first.
-- Only future rows are cleared; past slots are left alone (they're inert
-- for scheduling anyway).
DELETE FROM pc1.machineschedule
WHERE client_id = 1
  AND facility_id = 3
  AND date_and_time_utc >= now();

-- -- STEP 2: regenerate the calendar (run from the shell, not SQL) -------
-- python generate_machineschedule.py --facility "Antioch Medical Imaging" \
--     --start-date <today, YYYY-MM-DD> --days-ahead 30 --client-id 1
--
-- Then fold exceptions into availability (after inserting the exception
-- rows below):
-- python reconcile_exceptions.py --facility "Antioch Medical Imaging" \
--     --start-date <today, YYYY-MM-DD> --days-ahead 30 --client-id 1

-- -- STEP 1b: synthetic schedule exceptions -------------------------------
-- Replace <mr_modality_id> with a real pc1.modalities.id for an MR machine
-- at this facility (see lookup query above). Reused logic:
-- reconcile_exceptions.py is what actually folds these into
-- pc1.machineschedule.availability -- run it (Step 2, second command)
-- AFTER these rows exist.
INSERT INTO pc1.scheduleexceptions
    (client_id, facility_id, modality_id, modality_type, description,
     start_date, start_time, end_date, end_time,
     recurrence, type, repeat_every, weekdays_only, is_active)
VALUES
    -- Hard exception: one MR machine down for a single morning.
    (1, 3, <mr_modality_id>, 'MR', 'TEST: MR machine maintenance',
     CURRENT_DATE + 3, '08:00', CURRENT_DATE + 3, '12:00',
     'None', 'Hard', 1, false, true),
    -- Soft, recurring: all CT machines blocked for lunch every weekday.
    (1, 3, NULL, 'CT', 'TEST: CT lunch block (all CT machines)',
     CURRENT_DATE, '12:00', CURRENT_DATE + 90, '12:30',
     'Daily', 'Soft', 1, true, true);

-- -- STEP 3: hand-mark specific slots as already booked -------------------
-- Run this AFTER step 2's regeneration + reconcile. Replace <modality_id>
-- and the timestamps with real values from pc1.machineschedule for this
-- facility (pick a couple of in-window, in-hours slots to simulate
-- existing appointments).
--
-- UPDATE pc1.machineschedule
-- SET availability = 0, updated_at = now()
-- WHERE client_id = 1 AND facility_id = 3 AND modality_id = <modality_id>
--   AND date_and_time_utc IN (
--       '<YYYY-MM-DDTHH:MM:SS+00>',
--       '<YYYY-MM-DDTHH:MM:SS+00>'
--   );
