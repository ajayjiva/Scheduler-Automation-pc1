# `pc1.machineschedule`

The slot calendar. One row per
`(client_id, facility_id, modality_id, date_and_time_utc)` representing a
bookable time slot on a single machine. This is the table the
blank-calendar generator (Phase 2, deferred) populates and the
exception reconciler (Phase 3, deferred) mutates, and the scheduling
engine (Phase 4, deferred) reads when assembling patient appointment
options.

Unlike the other tenant-scoped tables in pc1, `machineschedule` is
**not RIS-sourced** — slots are computed locally from
`pc1.facilities.opening_time`/`closing_time` and `slot_size` over a
rolling window. There is no `content_hash` / `source_record_key` /
`ris_*` / `is_active` machinery, because there is nothing upstream to
diff against.

---

## Identity

| Concept           | Field(s)                                                          |
|-------------------|-------------------------------------------------------------------|
| Surrogate PK      | `id`                                                              |
| **Business key**  | `(client_id, facility_id, modality_id, date_and_time_utc)`            |

The business key is enforced via a regular `UNIQUE` constraint (not a
partial index): every slot has all four columns set. There is no
"global slot" tier — every row belongs to a specific machine at a
specific facility for a specific tenant at a specific UTC instant.

The generator uses this constraint as the conflict target for
`INSERT ... ON CONFLICT DO NOTHING`, which gives it free idempotency
on re-runs.

---

## Columns

### Foreign keys

| Column        | FK target                                  | Null? | Notes |
|---------------|--------------------------------------------|-------|-------|
| `client_id`   | `pc1.clients(id)`                          | NO    | Tenant scope. No DEFAULT — must be set explicitly. |
| `facility_id` | `pc1.facilities(id)`                       | NO    | Every slot belongs to a real facility. |
| `modality_id` | `pc1.modalities(id)`                       | NO    | Every slot belongs to a real machine. |
| `order_id`    | `pc1.orders(id)` — **FK deferred**         | YES   | Populated when a booking is recorded against the slot. The schema does NOT enforce the FK yet because `pc1.orders` does not exist at the time of this migration; Phase 4 (orders work) will `ALTER TABLE ... ADD CONSTRAINT`. Until then, the column is a bare `bigint`. |
| `created_by`  | `pc1.user_profiles(id)` ON DELETE SET NULL | YES   | NULL for generator and reconciler writes. |
| `updated_by`  | `pc1.user_profiles(id)` ON DELETE SET NULL | YES   | Same. |

### Slot identity / ordering

| Column          | Type                       | Null? | Notes |
|-----------------|----------------------------|-------|-------|
| `seq`           | `bigint`                   | YES   | **Facility-local `YYYYMMDDHHMMSS`** of the slot start, encoded as a single integer (e.g. `20260528083000` = `2026-05-28 08:30:00` facility-local). See [The `seq` engine contract](#the-seq-engine-contract) below for why it's local-not-UTC. Populated by the generator. |
| `slot_seq`      | `integer`                  | YES   | 1-based ordinal of the slot within its facility-local day (slot at 08:00 → `1`, next at 08:15 → `2`, etc., when `slot_size = 15`). Useful for ordering within a single date without re-deriving from `date_and_time_utc`. |
| `date_and_time_utc` | `timestamptz`              | NO    | **UTC.** The instant the slot starts. The generator iterates facility-local wall clock and converts at write time via `zoneinfo`. The `_utc` suffix is intentional — `start_time` and `end_time` on the same row carry facility-LOCAL clock values, so the contrasting name prevents misreading. See [Time-zone semantics](#time-zone-semantics). |
| `start_time`    | `time without time zone`   | YES   | Facility-local start time of the slot (e.g. `08:00:00`). Carried for the scheduling engine, which constructs human-readable patient-option output without re-running the UTC↔local conversion. |
| `end_time`      | `time without time zone`   | YES   | Facility-local end time, i.e. `start_time + slot_size`. Same rationale. |

### Slot state

| Column         | Type      | Null? | Default | Notes |
|----------------|-----------|-------|---------|-------|
| `capacity`     | `integer` | YES   | —       | Generator writes `1`. Reserved for future multi-patient slot support; nothing currently consumes a non-1 value. |
| `availability` | `integer` | YES   | —       | `0` = blocked, `1` = free. Generator writes `1`; the reconciler drops it to `0` for slots covered by a Hard exception. |
| `scheduled`    | `integer` | YES   | —       | Reserved for future booking-state bookkeeping (e.g. partial counts when `capacity > 1`). Not currently written. |

### Exception overlays

| Column           | Type     | Null? | Default      | Notes |
|------------------|----------|-------|--------------|-------|
| `exceptions`     | `text[]` | NO    | `'{}'`       | Display labels, e.g. `{'LUNCH (H)','STAFF MEETING (S)'}`. Element N is paired positionally with element N of `exception_ids`. |
| `exception_ids`  | `text[]` | NO    | `'{}'`       | `source_record_key` values from `pc1.scheduleexceptions`. Sorted ascending by key for deterministic output. |

The two arrays are maintained as a paired set by the reconciler and
must stay equal-length. Postgres can't enforce paired-length without
a trigger; the discipline lives in the single writer. See [Exception
overlay invariants](#exception-overlay-invariants).

### Audit

| Column        | Type            | Null? | Default | Notes |
|---------------|-----------------|-------|---------|-------|
| `created_at`  | `timestamptz`   | NO    | `now()` | Set once on INSERT. Preserved on UPDATE. |
| `updated_at`  | `timestamptz`   | NO    | `now()` | Writers MUST set explicitly on every UPDATE — the DEFAULT only fires on INSERT. |
| `created_by`  | `bigint`        | YES   | —       | FK to `pc1.user_profiles`. NULL for generator / reconciler writes. |
| `updated_by`  | `bigint`        | YES   | —       | Same. |

---

## What this table deliberately does NOT have

| Column / pattern | Why omitted |
|---|---|
| `is_active` | Slots aren't soft-deleted. There's no upstream RIS to disappear; a slot either exists (because the generator put it there) or doesn't. |
| `source_record_key` | Slots are not RIS rows. The `(client_id, facility_id, modality_id, date_and_time_utc)` business key already uniquely identifies a slot. |
| `content_hash` | Delta-sync diffing doesn't apply — there's nothing to diff against. The generator inserts via `ON CONFLICT DO NOTHING`; the reconciler decides per-row whether the desired state already matches before writing. |
| `ris_system` / `ris_sync_status` / `ris_last_synced_at` / `ris_metadata` / `synced_at` | All four are source-system tracking. Not applicable. |
| `cumm_open_slots_below` / `cumm_open_slots_above` (legacy) | The legacy table cached cumulative open-slot counts at generation time. Stale-data footgun: every reconciler write would have to recompute and rewrite them, or readers would see lies. The scheduling engine recomputes in-process from `availability`. |
| `temp_reservation_id` (legacy) | No documented use; removed. |
| `modality_type` / `modality_machine` / `facility` (legacy text columns) | Replaced by the `facility_id` / `modality_id` FKs. JOIN when display names are needed. |
| `scheduled_by` / `slot_status` (potential future columns) | Deferred until the orders state machine is designed in Phase 4. `order_id IS NULL` vs. `order_id IS NOT NULL` is enough signal until then. |

---

## Slot-window parameter resolution

The generator computes its working window from per-tenant + per-facility
settings, merging facility override over client default:

| Parameter              | Override source                       | Default source           | Used for |
|------------------------|---------------------------------------|--------------------------|----------|
| `slot_size` (minutes)  | `pc1.facilities.slot_size`            | `pc1.clients.slot_size`  | Slot width; drives `slot_seq` and `start_time`/`end_time` arithmetic |
| `opening_time`         | `pc1.facilities.opening_time`         | `pc1.clients.opening_time` | First slot of each facility-local day |
| `closing_time`         | `pc1.facilities.closing_time`         | `pc1.clients.closing_time` | Last slot of each facility-local day (exclusive) |
| `advance_booking_days` | `pc1.facilities.advance_booking_days` | `pc1.clients.advance_booking_days` | Default rolling-window length |
| `timezone`             | `pc1.facilities.timezone` (NOT NULL)  | — (always set on facility) | Facility-local ↔ UTC conversion |

Both `pc1.clients` and `pc1.facilities` carry NOT NULL versions of all
five today, so the merge degenerates to "use the facility value" for
every existing tenant. The override pattern exists so future tenants
can run multiple facilities with divergent hours / slot sizes without
forking the tenant.

---

## Time-zone semantics

`date_and_time_utc` is `timestamptz` storing UTC. The legacy public-schema
table stored `timestamp without time zone` and was later migrated to
UTC behind the scenes; pc1 ships UTC-from-day-one so the type itself
encodes the contract — no future "what timezone is this column in?"
question.

The generator's loop is **facility-local**:

```
for each calendar day d in [today, today + advance_booking_days]:
    for each slot s in [opening_time, closing_time) stepping by slot_size:
        local_dt = datetime(d, s, tzinfo = ZoneInfo(facility.timezone))
        utc_dt   = local_dt.astimezone(timezone.utc)
        INSERT ... date_and_time_utc = utc_dt,
                   start_time   = s,
                   end_time     = s + slot_size,
                   slot_seq     = <ordinal within d>
```

`zoneinfo` handles DST transitions automatically (skipped spring-forward
hour, doubled fall-back hour). A facility in PT will have 23-hour and
25-hour days twice a year; the generator inserts the right number of
slots for each.

Reads that filter by facility-local day boundary (e.g. "all slots for
Inview-Fremont on 2026-06-15") must convert the local boundary to UTC
the same way before issuing the query — see the reconciler pattern in
the legacy `reconcile_exceptions.py:fetch_machineschedule()` for a
worked example.

---

## The `seq` engine contract

`seq` is a compact `YYYYMMDDHHMMSS` integer of the slot's
**facility-local** start instant. For the slot starting at 08:30 PT
on 2026-05-28, `seq = 20260528083000`.

It exists to give the scheduling engine a single-integer
day-and-time key that:

- Sorts chronologically (integer comparison)
- Slices to extract just the day: `str(seq)[:8] = '20260528'`
- Aligns with `start_time` / `end_time` on the same row (all three
  are facility-local)

### Why facility-LOCAL and not UTC

The legacy scheduling engine — and the planned Phase 4 port — uses
`str(seq)[:8]` as the **same-day key** when searching for adjacent
modality blocks within a single visit. For example:

```python
# next_modalitytype_scheduler.py (legacy)
def same_day(seq1, seq2):
    return str(seq1)[:8] == str(seq2)[:8]
```

Consider a slot at **23:00 PT on 2026-05-28** (= 06:00 UTC on
2026-05-29):

| `seq` flavor                            | `seq[:8]`     | Engine sees |
|-----------------------------------------|---------------|---|
| **LOCAL** `20260528230000`              | `'20260528'`  | ✅ Same local day as the 09:00 PT slot of 2026-05-28 — chain can pair an MRI with adjacent CT before/after |
| **UTC** `20260529060000` (legacy-style) | `'20260529'`  | ❌ Different day — engine thinks the slot belongs to 5/29; never pairs it with morning 5/28 slots |

A UTC `seq` would silently break multi-modality scheduling for every
evening slot in a non-UTC time zone. The generator therefore writes
`seq` from `datetime.combine(facility_local_date, slot_start_time)`,
not from `date_and_time_utc`.

### Invariant the generator maintains

For every row:

```
str(seq)[:8] == to_char(date_and_time_utc AT TIME ZONE
                        facilities.timezone, 'YYYYMMDD')
```

This holds across DST transitions because the generator uses
`zoneinfo` to compute both sides (the LOCAL clock for `seq`, and the
UTC instant for `date_and_time_utc`) from the same source datetime.

A verification SQL that flags any rows violating the invariant is
included in [Verification queries](#verification-queries-post-migration)
below.

---

## Exception overlay invariants

`exceptions` and `exception_ids` are **paired positionally**: element
N of each describes the same exception. The reconciler (Phase 3) is
the sole maintainer of both arrays. Its contract:

1. `len(exceptions) == len(exception_ids)` at all times.
2. Both arrays are sorted ascending by `exception_ids` element (which
   is a `pc1.scheduleexceptions.source_record_key`). This makes
   updates deterministic and lets the reconciler skip writes via
   plain Python list comparison.
3. `exceptions` element format is `'<description> (<marker>)'` where
   `<marker>` is `H` for Hard or `S` for Soft. Empty description is
   allowed: `' (H)'`.
4. `availability` is `0` when any element has marker `H`, else `1`.
   The reconciler enforces this invariant on every write.

Postgres can't enforce paired-length or any of the above without a
trigger. We accept the discipline-enforced contract because the single
writer makes it trivially maintainable. Don't write to either array
from anything other than the reconciler.

---

## Cross-tenant safety trigger

The `BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id`
trigger `trg_machineschedule_consistency_check` fires the function
`pc1.check_machineschedule_consistency()` to validate that all three
columns belong to the same tenant. Same shape as the
`pc1.proceduresestimate` and `pc1.scheduleexceptions` triggers.

The trigger checks:

1. If `facility_id IS NOT NULL`: look up
   `pc1.facilities(id = NEW.facility_id).client_id`. Row missing →
   raise. Row exists but `client_id` differs → raise.
2. If `modality_id IS NOT NULL`: same against `pc1.modalities`.

Error messages are explicit:

```
ERROR: machineschedule.client_id=1 does not match
       pc1.facilities(id=42).client_id=2

ERROR: machineschedule.modality_id=99999 references a row that
       does not exist in pc1.modalities
```

UPDATEs that touch only non-FK columns (the reconciler updating
`availability` + arrays + `updated_at`, the engine updating `order_id`)
skip the trigger entirely via the `OF client_id, facility_id, modality_id`
clause — so the hot path costs zero trigger work.

Per-row trigger cost is two indexed PK lookups (~50-100µs total),
invisible against any generator or reconciler workload.

---

## Lifecycle

Unlike the other pc1 tables, this one has no insert-update-deactivate
state machine. The writers are:

| Writer | Phase | Touches | Conflict handling |
|---|---|---|---|
| Generator (Phase 2) | Insert blank rows over a rolling window | All columns at INSERT | `ON CONFLICT (client_id, facility_id, modality_id, date_and_time_utc) DO NOTHING` — re-runs are no-ops on already-existing slots |
| Reconciler (Phase 3) | UPDATE `exceptions`, `exception_ids`, `availability`, `updated_at` for slots whose desired exception state differs from current | Three columns + audit | Per-row pre-check: skip the UPDATE entirely when current state already matches desired (idempotent re-runs cost reads only) |
| Engine / booking (Phase 4) | UPDATE `order_id` (and eventually `scheduled`) when a booking is recorded | `order_id` + audit | TBD — Phase 4 will decide whether to use optimistic locking, advisory locks, or `SELECT FOR UPDATE` |

There is no `DELETE` path. Slots stay in the table forever once
generated; the cost of retention is small and the absence of deletes
keeps `id` references stable. Manual cleanup of very old past-dated
rows (if it ever becomes a concern) is the operator's call.

---

## Common queries

```sql
-- All slots for a facility on a specific facility-local day.
-- IMPORTANT: convert the local day boundary to UTC before filtering.
SELECT ms.id, ms.date_and_time_utc, ms.start_time, ms.modality_id,
       ms.availability, ms.exceptions
  FROM pc1.machineschedule ms
 WHERE ms.client_id   = 1
   AND ms.facility_id = 42
   AND ms.date_and_time_utc >= '2026-06-15 07:00:00+00'  -- 00:00 PT
   AND ms.date_and_time_utc <  '2026-06-16 07:00:00+00'  -- 00:00 PT next day
 ORDER BY ms.modality_id, ms.date_and_time_utc;

-- Find all slots affected by a specific exception (uses GIN index)
SELECT id, modality_id, date_and_time_utc, exceptions
  FROM pc1.machineschedule
 WHERE client_id = 1
   AND exception_ids @> ARRAY['58713'];

-- All free, unbooked slots for a machine over the next 30 days
SELECT date_and_time_utc, start_time, end_time
  FROM pc1.machineschedule
 WHERE client_id    = 1
   AND modality_id  = 12
   AND date_and_time_utc >= now()
   AND date_and_time_utc <  now() + interval '30 days'
   AND availability = 1
   AND order_id IS NULL
 ORDER BY date_and_time_utc;

-- All slots booked against a given order
SELECT id, modality_id, date_and_time_utc, start_time, end_time
  FROM pc1.machineschedule
 WHERE order_id = 12345
 ORDER BY date_and_time_utc;

-- Spot-check the seq engine contract (should return zero rows):
-- str(seq)[:8] must equal the facility-local YYYYMMDD of the slot.
SELECT ms.id,
       ms.seq,
       substring(ms.seq::text, 1, 8) AS seq_day,
       to_char(ms.date_and_time_utc AT TIME ZONE f.timezone,
               'YYYYMMDD')             AS local_day
  FROM pc1.machineschedule ms
  JOIN pc1.facilities      f ON f.id = ms.facility_id
 WHERE substring(ms.seq::text, 1, 8)
    <> to_char(ms.date_and_time_utc AT TIME ZONE f.timezone, 'YYYYMMDD');

-- Spot-check the paired-array invariant (should return zero rows)
SELECT id, array_length(exceptions, 1) AS n_excs,
       array_length(exception_ids, 1) AS n_ids
  FROM pc1.machineschedule
 WHERE coalesce(array_length(exceptions, 1), 0)
     <> coalesce(array_length(exception_ids, 1), 0);

-- Spot-check the availability-vs-Hard invariant (should return zero rows)
SELECT id, availability, exceptions
  FROM pc1.machineschedule
 WHERE availability = 1
   AND EXISTS (
       SELECT 1 FROM unnest(exceptions) AS e
        WHERE e LIKE '% (H)'
   );
```

---

## Verification queries (post-migration)

Run these in the Supabase SQL editor after applying
`migrations/0004_create_pc1_machineschedule.sql` to confirm the shape
landed correctly:

```sql
-- Table exists and is empty
SELECT count(*) FROM pc1.machineschedule;

-- Columns + types + nullability + defaults
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = 'pc1' AND table_name = 'machineschedule'
 ORDER BY ordinal_position;

-- All FKs
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = 'pc1.machineschedule'::regclass
   AND contype = 'f';

-- Business-key UNIQUE
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = 'pc1.machineschedule'::regclass
   AND contype = 'u';

-- All indexes
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE schemaname = 'pc1' AND tablename = 'machineschedule'
 ORDER BY indexname;

-- Trigger present
SELECT tgname, pg_get_triggerdef(oid)
  FROM pg_trigger
 WHERE tgrelid = 'pc1.machineschedule'::regclass
   AND NOT tgisinternal;

-- Cross-tenant trigger sanity check: attempt to insert a row whose
-- client_id mismatches its facility's tenant. Should RAISE.
-- (Replace the IDs with two real client/facility rows whose
-- client_ids differ; expect an ERROR, not an INSERT.)
-- INSERT INTO pc1.machineschedule
--   (client_id, facility_id, modality_id, date_and_time_utc)
-- VALUES (1, <facility_id_for_client_2>, <modality_id_for_client_2>, now());
```

---

## Related

- [`pc1.facilities`](./facilities.md) — supplies `facility_id`, slot-size /
  hours / timezone / advance-booking overrides.
- [`pc1.modalities`](./modalities.md) — supplies `modality_id`.
- [`pc1.scheduleexceptions`](./scheduleexceptions.md) — supplies the
  `source_record_key` values that appear in `exception_ids`, and the
  rule windows the reconciler expands into per-slot overlays.
- Blank-calendar generator doc — coming with Phase 2.
- Exception reconciler doc — coming with Phase 3.
