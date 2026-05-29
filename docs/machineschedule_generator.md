# `generate_machineschedule.py`

Generates blank slot rows in `pc1.machineschedule` for one or more
facilities over a rolling window. This is the **Phase 2 writer** for
the slot calendar table — populates the empty rows that the Phase 3
reconciler later overlays with `pc1.scheduleexceptions` rules, and
that the Phase 4 scheduling engine reads when assembling patient
appointment options.

Unlike the other scripts in this repo, this one is **not a scraper**
— there is no RIS source for slots. The generator computes them
locally from `pc1.facilities.slot_size` and the active
`pc1.modalities` rows for each facility.

For the table schema (column meanings, business key, invariants,
trigger), see [`docs/machineschedule.md`](./machineschedule.md).

---

## What it writes

For each `(active modality, facility-local date in the window, slot
start in a 24-hour day)`, one row is INSERTed with:

| Column          | Value at generation time |
|-----------------|---------------------------|
| `client_id`     | resolved tenant (`--client-id`, env, or default) |
| `facility_id`   | `pc1.facilities.id` for this facility |
| `modality_id`   | `pc1.modalities.id` for an active machine |
| `seq`           | **facility-local** `YYYYMMDDHHMMSS` bigint of the slot start (e.g. `20260528083000` for 08:30 PT on 2026-05-28). Engine extracts the local date via `str(seq)[:8]` — see [`docs/machineschedule.md` → The `seq` engine contract](./machineschedule.md#the-seq-engine-contract) |
| `slot_seq`      | 1-based ordinal of the slot within its facility-local day |
| `date_and_time_utc` | UTC instant (facility-local wall clock → UTC via `zoneinfo`) |
| `start_time`    | facility-local time of slot start (`08:00:00`, `08:15:00`, ...) |
| `end_time`      | `start_time + slot_size`; wraps to `00:00:00` for the last slot |
| `capacity`      | `1` |
| `availability`  | `1` if `opening_time <= start_time < closing_time` (in business hours), else `0`. See [Initial availability and business hours](#initial-availability-and-business-hours). |
| `exceptions`    | `'{}'` |
| `exception_ids` | `'{}'` |
| `created_at`    | now (UTC) |
| `updated_at`    | now (UTC) |

Left `NULL` at generation time (other writers populate later):
`scheduled` (future booking bookkeeping), `order_id` (Phase 4 —
booking), `created_by` / `updated_by` (automated write).

---

## Window resolution

| Setting              | Source (facility row override > client row) |
|----------------------|---------------------------------------------|
| `slot_size`          | `pc1.facilities.slot_size` → `pc1.clients.slot_size` |
| `advance_booking_days` (used as default `--days-ahead`) | `pc1.facilities.advance_booking_days` → `pc1.clients.advance_booking_days` |
| `opening_time`       | `pc1.facilities.opening_time` → `pc1.clients.opening_time` |
| `closing_time`       | `pc1.facilities.closing_time` → `pc1.clients.closing_time` |
| `timezone`           | `pc1.facilities.timezone` → `pc1.clients.timezone` |

Today every relevant column is `NOT NULL` on both tables (with
`DEFAULT`s), so the facility row always carries a value and the
fallback to client is effectively dead code. It exists for forward
compatibility per the design rule documented in
[`docs/machineschedule.md`](./machineschedule.md#slot-window-parameter-resolution).

`opening_time` and `closing_time` drive the **initial `availability`
value** of each slot — see the next section. The generator still
emits a full 24-hour grid; the business-hours rule decides whether
each slot starts life as bookable (`availability=1`) or blocked
(`availability=0`).

---

## Initial availability and business hours

The generator emits slots for the full 24-hour day so the calendar
grid is always complete (no "we need to extend hours so let me
regenerate" pain). Each slot's initial `availability` reflects the
facility's business hours:

```
availability = 1 if  opening_time <= start_time < closing_time
               else 0
```

The interval is **half-open** (end-exclusive) — same as the legacy
engine's DB-level filter (`start_time >= opening AND start_time <
closing`). Worked examples for `opening_time=08:00`,
`closing_time=17:00`, `slot_size=15`:

| `start_time` | `end_time` | `availability` | Why |
|---|---|---|---|
| `07:45` | `08:00` | `0` | Starts before opening |
| `08:00` | `08:15` | `1` | First in-hours slot |
| `16:45` | `17:00` | `1` | Last in-hours slot — ends at closing, which is fine |
| `17:00` | `17:15` | `0` | Starts at closing → out-of-hours |
| `23:45` | `00:00` | `0` | Overnight |

The Phase 4 scheduling engine filters its slot pool by
`availability = 1`, so out-of-hours slots are never offered to
patients. The grid is still present in the DB for future use
(after-hours scheduling, manual adjustments, reconciler bookkeeping).

### Reconciler note (Phase 3)

The Phase 3 reconciler will further reduce `availability` to 0 for
slots covered by Hard `pc1.scheduleexceptions` rules. It MUST NOT
flip out-of-hours slots back to `availability=1` when no Hard
exception covers them — the "blocked by business hours" state is
intrinsic to the slot, not exception-derived. The reconciler will
re-check the in-hours-ness from the slot's `start_time` and the
facility's `opening_time` / `closing_time` before any flip.

### Per-facility log line

The per-facility output line shows the split:

```
[Inview-Fremont] slot_size=15min  tz=America/Los_Angeles  hours=[08:00:00, 17:00:00)
  window=2026-05-29 -> 2026-08-27 (91 days)  modalities=8
  -> 69,888 candidate slots (26,208 availability=1 / 43,680 availability=0)
```

For a facility with `slot_size=15` and an 8-hour business day
(08:00–17:00 = 9 hours wall-clock), 36 of 96 daily slots are
in-hours → roughly 38% `availability=1`, 62% `availability=0`.

---

## Modality filter

Generates slots only for `pc1.modalities` rows where:

```
is_active = true  AND  status IN ('Active', NULL)
```

`status = 'Inactive'` and `status = 'UNKNOWN'` rows are NovaRIS
placeholder junk (see [`docs/modalities.md`](./modalities.md)) and
never get blank calendar entries. `status IS NULL` covers
non-NovaRIS tenants where the column is not populated; those are
treated the same as `Active`.

---

## Facility scope

Default: iterates every row in `pc1.facilities` for the active
tenant where `is_client = true` (same convention as the NovaRIS
scrapers).

Pass `--facility=NAME` to limit to a single facility. The flag
bypasses the `is_client` check so it can also be used to bootstrap a
newly-contracted facility before flipping its `is_client` flag.

---

## CLI flags

| Flag | Default | What it does |
|---|---|---|
| `--client-id=NNNN`        | from env / default | Override active tenant for this run |
| `--facility=NAME`         | iterate is_client=true | Process only this facility, by `pc1.facilities.facility_name` |
| `--start-date=YYYY-MM-DD` | today, facility-local | Inclusive window start |
| `--days-ahead=N`          | facility's resolved `advance_booking_days` | Inclusive window length in days |
| `--limit=N`               | unlimited | Cap rows per facility (smoke testing) |
| `--dry-run`               | off | Compute rows; do not write to Supabase |
| `--quiet`                 | off | Suppress per-facility progress chatter |

---

## Idempotency

The generator uses
`INSERT ... ON CONFLICT (client_id, facility_id, modality_id, date_and_time_utc) DO NOTHING`
on the table's business-key UNIQUE constraint. Re-running over an
already-generated window is a safe no-op — every conflicting row is
silently skipped server-side, no UPDATE issued, no audit-column churn.

This means the typical operational pattern is just "run it whenever":

```powershell
# Daily / weekly maintenance — extends the rolling horizon by one day
python generate_machineschedule.py

# After bumping Fremont's slot_size — regenerate Fremont's future slots
python generate_machineschedule.py --facility=Inview-Fremont
```

The second command does **not** rewrite existing slots — it only adds
the new slot positions implied by the new `slot_size` for days that
don't have them yet. **If you change `slot_size` mid-stream you must
manually wipe the affected future window first**; see the next
section.

### Wiping slots before regenerating

When you genuinely need a clean slate (after a `slot_size` change,
business-hours rework, or accidental seed with wrong data), run a
DELETE in the Supabase SQL editor:

```sql
-- Clean future slots for one facility
DELETE FROM pc1.machineschedule
 WHERE client_id     = <CLIENT_ID>
   AND facility_id   = (SELECT id FROM pc1.facilities
                         WHERE client_id    = <CLIENT_ID>
                           AND facility_name = '<FACILITY_NAME>')
   AND date_and_time_utc >= '<YYYY-MM-DD>'::timestamptz;
```

Then re-run the generator. The DELETE is intentionally NOT exposed as
a CLI flag — destructive ops on shared data belong in SQL where
they're visible to anyone reviewing the operational log.

---

## DST behavior (24-hour generation)

The generator emits `(24 * 60) / slot_size` slot-starts per day in
facility-local wall clock, then converts each to UTC via `zoneinfo`.
On the two days of the year DST shifts in IANA-aware timezones, this
produces a small known artifact:

| Day | What happens |
|---|---|
| **Spring-forward** (~March, 02:00 PT → 03:00 PT in US/Pacific) | The 02:00 PT slot's UTC equivalent collides with the 03:00 PT slot's UTC. `ON CONFLICT DO NOTHING` silently drops the duplicate. That day has 23 unique UTC slots for the affected hour — which matches reality (the day is genuinely 23 hours long). |
| **Fall-back** (~November, 01:00 PT happens twice) | Only the first fold of the doubled hour is emitted, so the day produces 24 unique UTC slots instead of the theoretical 25. The "missing" second-fold hour is between 01:00 and 02:00 — well outside business hours; no operational impact. |

These transitions occur at 02:00 local — well outside business hours
for every contracted facility today — so the artifacts are invisible
to actual scheduling.

---

## Output

A single-facility run looks like:

```
client_id=1 (Inview Imaging)  facilities=1  WRITE
  [Inview-Fremont] slot_size=15min  tz=America/Los_Angeles  window=2026-05-29 -> 2026-08-27 (91 days)  modalities=5  -> 43,680 candidate slots
  [Inview-Fremont] attempted=43,680  inserted=43,680  skipped_existing=0

Done. Total slots attempted: 43,680  Elapsed: 0m 24s  Rate: 1,820 rows/sec
```

`inserted` counts rows that landed; `skipped_existing` counts rows
the ON CONFLICT branch silently dropped. The split is computed by
counting rows in the UTC window before and after the insert pass —
which means a concurrent writer could skew the numbers (very unlikely
in practice; the generator is operator-driven, not on a cron).

Smoke-test runs (`--dry-run` or `--limit=N`) print expected counts
without contacting Postgres for the row-count diff.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean — every facility produced its expected slot count (or was a clean no-op on re-run) |
| `1` | Resolution failure — no matching facility row, missing client row, or other "can't even start" condition |
| `3` | Partial — at least one facility was skipped (no active modalities, bad timezone, invalid `--start-date`, etc.) while others succeeded |

Exit codes are designed to be cron-friendly even though the
generator is operator-driven today.

---

## Performance

Measured on the live Inview Imaging tenant (`client_id = 1`) against
Supabase, against the May 2026 calendar window (31 days,
`slot_size = 15`):

| Facility | Modalities | Days | Slot size | Slots inserted | Wall time | Rate |
|---|---|---|---|---|---|---|
| Inview-Fremont          | 8  | 31 | 15 min | 23,808 | 20 s | 1,145 rows/sec |
| Antioch Medical Imaging | 10 | 31 | 15 min | 29,760 | ~23 s | ~1,290 rows/sec |
| **Full tenant** (both, fresh-Antioch + idempotent-Fremont) | 18 | 31 | 15 min | 29,760 new + 23,808 skipped | 40 s | 1,328 rows/sec (aggregate) |

Idempotent re-run on a fully-generated Fremont window: 23,808 rows
attempted, **0 inserted**, 23,808 silently skipped server-side — 17 s
wall time, which is just the cost of streaming the duplicate rows
through PostgREST so Postgres can reject them. No UPDATE issued, no
audit column churn, no row-count drift.

Throughput is consistent with the 1,000–2,000 rows/sec heuristic for
batched PostgREST inserts of small records. Scales linearly with
total slot count — a 90-day full-tenant horizon would be ~3× the
above (≈ 2 min wall time end-to-end).

---

## Operational pattern

### First-time bootstrap for a new facility

1. Confirm `pc1.facilities` has a row for the facility, `is_client = true`,
   `slot_size` / `timezone` / `advance_booking_days` set as desired.
2. Confirm `pc1.modalities` has the facility's active machines (run
   `novaRIS_modalities_scraper.py --facility=NAME --initial-load`).
3. Run the generator:
   ```powershell
   python generate_machineschedule.py --facility=NAME
   ```

### Rolling-window maintenance

Just run the generator on whatever cadence makes sense for you (daily,
weekly, monthly). Each run extends the rolling horizon by whatever
days have passed since the last run. Re-runs without time passing are
no-ops.

```powershell
# Cover every contracted facility — extends each one's horizon
python generate_machineschedule.py
```

### Reacting to a config change

| Change | Action |
|---|---|
| New machine added (`pc1.modalities` row inserted) | Run the generator — it'll insert slots for the new machine, leave existing slots untouched |
| Machine deactivated (`is_active=false` or `status='Inactive'`) | Nothing to do — generator stops emitting; existing slots stay (may still be booked) |
| `slot_size` changed | Wipe future slots via the SQL above, then re-run the generator |
| `timezone` changed | Wipe **all** slots for that facility (past + future), then re-run — past UTC values were computed with the wrong timezone |
| `advance_booking_days` changed | Run the generator — the new value drives the next horizon-extension |

---

## Verification (post-run sanity SQL)

Run these in the Supabase SQL editor after a generator run to confirm
the shape of what landed:

```sql
-- Per-facility / per-day slot counts: should be (24 * 60 / slot_size)
-- * (number of active modalities) for every facility-local day in the
-- window. Adjust the time zone in the date_trunc to your facility's.
SELECT date_trunc('day',
                   date_and_time_utc AT TIME ZONE 'America/Los_Angeles')::date
         AS facility_local_day,
       count(*) AS slot_count
  FROM pc1.machineschedule
 WHERE client_id   = 1
   AND facility_id = (SELECT id FROM pc1.facilities
                       WHERE client_id    = 1
                         AND facility_name = 'Inview-Fremont')
   AND date_and_time_utc >= now()
   AND date_and_time_utc <  now() + interval '7 days'
 GROUP BY 1
 ORDER BY 1;

-- Confirm every slot is initialized to availability=1 with empty arrays
SELECT count(*) FROM pc1.machineschedule
 WHERE availability  <> 1
    OR cardinality(exceptions)    > 0
    OR cardinality(exception_ids) > 0;
-- Expected: 0 (after a fresh generator run; reconciler hasn't touched
-- anything yet)

-- All distinct slot_seq values for one day (should equal 1..slots_per_day)
SELECT distinct slot_seq
  FROM pc1.machineschedule
 WHERE client_id   = 1
   AND facility_id = (SELECT id FROM pc1.facilities
                       WHERE client_id    = 1
                         AND facility_name = 'Inview-Fremont')
   AND date_and_time_utc >= (current_date)::timestamptz
   AND date_and_time_utc <  ((current_date + 1))::timestamptz
 ORDER BY slot_seq;
```

---

## Related

- [`pc1.machineschedule` schema doc](./machineschedule.md) — the
  authoritative reference for what each column means and the
  reconciler's invariants
- [`pc1.modalities` doc](./modalities.md) — the source of the
  `modality_id` rows the generator iterates
- [`pc1.facilities` doc](./facilities.md) — the source of
  `slot_size` / `timezone` / `advance_booking_days` / `is_client`
- Exception reconciler doc — coming with Phase 3
