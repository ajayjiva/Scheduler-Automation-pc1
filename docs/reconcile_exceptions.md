# `reconcile_exceptions.py`

Reads active rules from `pc1.scheduleexceptions` and applies them to
`pc1.machineschedule`. Each in-range slot's `exceptions`,
`exception_ids`, and `availability` columns are computed from the
full set of currently-active exceptions and surgically updated only
when the row's state differs from the computed target.

This is the **Phase 3 writer** for the slot calendar — the bridge
between the rule-set table (Phase 2 of `pc1.scheduleexceptions`) and
the slot grid (Phase 2 of `pc1.machineschedule`). The Phase 4
scheduling engine reads the resulting `availability=1` slots when
assembling patient appointment options.

For the slot-calendar schema, see
[`docs/machineschedule.md`](./machineschedule.md). For the
exception-rule schema, see
[`docs/scheduleexceptions.md`](./scheduleexceptions.md).

---

## What it writes

The reconciler UPDATEs **only these columns** on `pc1.machineschedule`:

| Column          | Target value |
|-----------------|--------------|
| `exceptions`    | `['<desc> (H/S)', ...]` for every exception covering the slot, sorted by `source_record_key` |
| `exception_ids` | `['<source_record_key>', ...]` paired positionally with `exceptions` |
| `availability`  | Per the rule in [Availability rule](#availability-rule) below |
| `updated_at`    | `now()` (UTC) |

Untouched on every UPDATE: `created_at`, `seq`, `slot_seq`,
`date_and_time_utc`, `start_time`, `end_time`, `modality_id`,
`facility_id`, `client_id`, `order_id`, `capacity`, `scheduled`,
`created_by`, `updated_by`.

The `BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id`
cross-tenant trigger on `pc1.machineschedule` is **not** invoked by
this reconciler's UPDATEs (the trigger's `OF` clause filters by
column), so the hot path costs zero trigger work.

---

## Availability rule

```
if  slot.start_time is OUTSIDE [opening_time, closing_time):
    # Out of business hours -- the generator owns this invariant.
    # Reconciler leaves availability untouched (whatever's there
    # stays).
else:
    # In business hours -- the reconciler owns this invariant.
    availability = 0 if any Hard exception covers the slot, else 1
```

The two halves of this rule are **deliberately split between
writers**:

| Slot category | Owner | Logic |
|---|---|---|
| Out-of-hours (`start_time < opening_time` or `>= closing_time`) | Generator | Set to `0` at generation; never modified by reconciler |
| In-hours, no Hard exception | Reconciler | Set to `1` |
| In-hours, ≥1 Hard exception | Reconciler | Set to `0` |

### Why split this way

Each writer stays in its lane:
- The generator owns "is this slot physically schedulable?" (i.e.,
  is the door open?).
- The reconciler owns "is this in-hours slot currently held by a
  Hard exception?".

If the reconciler tried to flip out-of-hours slots when no Hard
covered them, two things could go wrong:
1. A manual SQL UPDATE that intentionally set an out-of-hours slot
   to `availability=1` (for, say, a special after-hours appointment)
   would silently get reverted on the next reconcile run.
2. The reconciler would carry implicit knowledge of "what does
   in-hours mean for this facility" that's better expressed once,
   in the generator and the doc.

### `exceptions[]` / `exception_ids[]` are always populated

Even on out-of-hours slots. If a `LUNCH (H)` exception covers the
22:00 row of a 24-hour-grid calendar, the row gets
`exceptions = ['LUNCH (H)']` written even though its `availability`
stays at `0` from generator time. The arrays are pure "what rules
apply here?" annotations; the `availability` column is the
"is it bookable?" decision derived from them.

### NULL `availability` (anomaly handling)

The generator always writes `0` or `1`. The reconciler always writes
`0` or `1` on the in-hours path. So `availability IS NULL` should
never appear in normal operation.

If it ever does (manual SQL UPDATE that forgot the column, or a
future bug), the reconciler **leaves it as NULL** — it doesn't
silently coerce to `0`. That preserves the anomaly so the
verification SQL can surface it.

Worked example:

```
Slot A -- facility-local 22:00, generated correctly:
    availability = 0       <- generator wrote this
    Reconciler runs        <- "out-of-hours, don't touch" -> still 0  ✓

Slot B -- same shape, but someone ran:
    UPDATE pc1.machineschedule SET availability = NULL WHERE id = 12345;
    Reconciler runs        <- "out-of-hours, don't touch" -> still NULL
    Verification SQL flags <- "1 row with NULL availability"
    Operator investigates  <- finds the manual update, fixes the data
```

If you ever want NULL to be impossible at the schema level, that's a
future one-line migration adding `NOT NULL` to the column with a
backfill of any NULLs to `0`. Not part of Phase 3's scope.

---

## Paired-array invariant

The reconciler is the **sole maintainer** of `exceptions` and
`exception_ids`. Its contract:

1. `len(exceptions) == len(exception_ids)` at all times.
2. Both arrays are sorted ascending by `exception_ids` element
   (which is a `pc1.scheduleexceptions.source_record_key`).
3. `exceptions` element format is `'<description> (<marker>)'`
   where `<marker>` is `H` for Hard or `S` for Soft.
4. `availability = 0` when any element has marker `H` AND the slot
   is in business hours. See [Availability rule](#availability-rule).

The sort gives deterministic output, which makes idempotent re-runs
free: a row whose target arrays already match (with the same sort)
is skipped — no UPDATE issued, no `updated_at` churn.

---

## Recurrence semantics (ported from legacy)

| `recurrence` value | Dates the exception covers in `[range_start, range_end]` |
|---|---|
| `None`    | Only `start_date`, if it falls in the range |
| `Daily`   | Every day in `[start_date, end_date]` — Mon-Fri only when `weekdays_only=true`, else all 7 days |
| `Weekly`  | Days in `[start_date, end_date]` whose `weekday()` matches any `is_<dow>=true` flag |
| `Monthly` | Days in `[start_date, end_date]` whose `day` (1-31) matches any `day_N=true` flag. Days that don't exist (Feb 31) are skipped |

`repeat_every` is not currently consumed — no live tenant uses
values other than `1`. Adding support for it would extend the
`_exception_dates` helper.

Daily slot iteration steps by `slot_size` minutes inside the
`[start_time, end_time)` interval. Exceptions whose
`[start_time, end_time)` doesn't align with the calendar's slot
grid emit partial coverage and may leave the trailing partial slot
untouched. In practice the facility's exception editor uses the
same slot grid as the calendar, so this is theoretical.

---

## Multi-facility behavior

Same convention as the generator and the scrapers:

- **Default** (no flag): iterates every `pc1.facilities` row for the
  tenant where `is_client = true`.
- **`--facility=NAME`**: limits to one facility. Bypasses `is_client`
  so a newly-contracted facility can be reconciled before flipping
  its `is_client` flag.

---

## Settings resolution

Same merge as the Phase 2 generator — facility row override > client
row default:

| Parameter         | Source |
|---|---|
| `slot_size`       | `pc1.facilities.slot_size` → `pc1.clients.slot_size` |
| `opening_time`    | `pc1.facilities.opening_time` → `pc1.clients.opening_time` |
| `closing_time`    | `pc1.facilities.closing_time` → `pc1.clients.closing_time` |
| `advance_booking_days` | `pc1.facilities.advance_booking_days` → `pc1.clients.advance_booking_days` |
| `timezone`        | `pc1.facilities.timezone` → `pc1.clients.timezone` |

`slot_size` **must match** what the generator used when the calendar
was created. If they diverge, the reconciler's per-slot
time-of-day grid won't align with the actual `machineschedule` rows
and most exceptions will silently no-op. The settings merge above
ensures consistency — both writers use the same resolution helper.

---

## Modality identity — FK, not text

`pc1.scheduleexceptions.modality_id` (bigint FK) → `pc1.machineschedule.modality_id`
(bigint FK). This is a clean win over the legacy public-schema
text join (`scheduleexceptions.name` → `machineschedule.modality_machine`):

- Faster join
- FK enforces existence
- No text-spelling drift surface

### `modality_id IS NULL` handling

`pc1.scheduleexceptions.modality_id` is nullable for forward
compatibility (a future RIS may produce facility-wide exception
rules with no specific machine). The NovaRIS scraper today always
sets it non-NULL.

The reconciler **skips** any active exception row whose `modality_id`
is NULL and emits a per-facility warning naming the affected
`source_record_key`s. Implementing the "applies to all machines"
fan-out is deferred — adds complexity and risks false-positive
blocks if a stray NULL ever sneaks in from a future scraper change.

To **manually test** this path during Phase 3 verification (the
reminder from the design conversation):

```sql
-- Pick any active exception for a facility, copy it with
-- modality_id NULL'd out:
INSERT INTO pc1.scheduleexceptions (
    client_id, facility_id, modality_id, modality_type,
    description, type, recurrence, start_date, end_date,
    start_time, end_time, is_active, source_record_key
)
SELECT client_id, facility_id, NULL, modality_type,
       'TEST: cross-machine rule', 'Hard', 'None', current_date,
       current_date, '10:00:00', '10:15:00', true,
       'TEST-NULL-MODALITY'
  FROM pc1.scheduleexceptions
 WHERE source_record_key IS NOT NULL
 LIMIT 1;

-- Run the reconciler (warning should fire):
-- python reconcile_exceptions.py --facility=Inview-Fremont --dry-run

-- Clean up after testing:
DELETE FROM pc1.scheduleexceptions
 WHERE source_record_key = 'TEST-NULL-MODALITY';
```

---

## Booked-slot Hard-exception conflict

When the reconciler computes a target state with at least one Hard
marker for a slot whose `order_id IS NOT NULL`:

1. The update is **still written** normally — arrays + availability
   per the rules above. The exception IS active; the operator just
   needs to know.
2. A conflict record is collected:
   `(slot_id, order_id, modality_id, modality_machine, date_and_time_utc, start_time_local, hard_exceptions)`.
3. At the end of the run, a `CONFLICTS` section is printed listing
   each booked-but-now-exception-covered slot.
4. The process exits with code **4** (distinct from `0`=clean,
   `1`=fatal, `3`=facility-skipped-due-to-bad-config).

This code path activates only once Phase 4's orders work starts
populating `order_id`. To **manually test** in Phase 3:

```sql
-- After a successful reconciler run, find a slot that ended up
-- with a Hard exception and pretend it was booked:
UPDATE pc1.machineschedule
   SET order_id = 9999
 WHERE id = <some_id_with_hard_in_exceptions>;

-- Then re-run the reconciler:
-- python reconcile_exceptions.py --facility=<NAME>
-- Expect: "CONFLICTS" section listing this slot; exit code 4.

-- Clean up after testing:
UPDATE pc1.machineschedule SET order_id = NULL WHERE id = <some_id>;
```

After running the cleanup, re-running the reconciler should produce
zero updates (idempotent) and exit code 0.

---

## CLI flags

| Flag | Default | What it does |
|---|---|---|
| `--client-id=NNNN`        | from env / default | Override active tenant |
| `--facility=NAME`         | iterate is_client=true | Process only this facility |
| `--start-date=YYYY-MM-DD` | today (facility-local) | Inclusive window start; **clamped to today** regardless of value |
| `--days-ahead=N`          | facility's `advance_booking_days` | Inclusive window length in days |
| `--workers=N`             | `6` | Parallel UPDATE workers |
| `--dry-run`               | off | Compute updates; do not write. Prints per-exception expansion summary. |
| `--quiet`                 | off | Suppress per-facility progress chatter |

---

## Past-date clamping

The window `range_start` is **clamped to today** even if
`--start-date` specifies an earlier date. This guarantees the
reconciler never modifies past slots. If you run with
`--start-date=2025-01-01` on 2026-06-01, the actual window starts at
2026-06-01.

`today` is computed in the **facility's** local timezone, so on a
multi-facility multi-timezone tenant each facility's clamp is
correct for its own clock. (Currently moot — the single live tenant
is single-timezone PT — but documented for the future.)

---

## Idempotency

Re-running over a calendar whose state already matches the active
exceptions is a no-op: every row's target state matches its current
state, so no UPDATE statement is issued. The `updated_at` of
unchanged rows stays frozen at whatever the last meaningful
reconciler write set it to. Operators can identify reconciler runs
that did real work by clustering on `updated_at`.

The `unchanged` count is implicit (`total fetched - total updated`)
since PostgREST doesn't expose a per-row "was this row updated?"
signal for batch UPDATEs.

---

## Output

A typical multi-facility run looks like:

```
client_id=1 (Inview Imaging)  facilities=2  WRITE  workers=6
  [Antioch Medical Imaging] tz=America/Los_Angeles slot_size=15min  hours=[08:00:00, 18:00:00)  range=2026-05-29 -> 2026-08-27 (91 days)  (WRITE)
  [Antioch Medical Imaging]   active exceptions=8073 (skipped_null_modality=0)  desired-state entries=12345 across 9876 unique slots
  [Antioch Medical Imaging]   machineschedule rows in range: 29760
  [Antioch Medical Imaging]   updates -- add/change=1234  removal-only=45
    applying 78 batched updates (across 12 distinct payloads, 6 parallel workers) ...
    applying 3 batched updates (across 2 distinct payloads, 3 parallel workers) ...
  [Antioch Medical Imaging] DONE -- 1279 slots updated.
  [Inview-Fremont]          ...
  [Inview-Fremont] DONE -- 891 slots updated.

Done. Total slots updated: 2,170  Elapsed: 0m 35s
```

Dry-run mode adds a per-exception expansion summary before the
machineschedule scan:

```
  [Inview-Fremont]   DRY-RUN exception expansion:
    LUNCH (H) on FRE-MRI                                       ->    372 slots affected
    STAFF MEETING (H) on FRE-MRI                               ->    248 slots affected
    LISA OOO (S) on FRE-MRI                                    ->     96 slots affected
    ...
```

---

## Verification (post-run sanity SQL)

Run these in the Supabase SQL editor after a reconciler run.

```sql
-- 1) Paired-array invariant (should be 0)
SELECT count(*)
  FROM pc1.machineschedule
 WHERE coalesce(array_length(exceptions, 1), 0)
    <> coalesce(array_length(exception_ids, 1), 0);

-- 2) Availability-vs-Hard invariant for IN-HOURS slots (should be 0)
-- Any in-hours slot with a Hard marker must have availability=0;
-- any in-hours slot with no Hard marker must have availability=1.
SELECT ms.id, ms.start_time, ms.availability, ms.exceptions,
       f.opening_time, f.closing_time
  FROM pc1.machineschedule ms
  JOIN pc1.facilities      f ON f.id = ms.facility_id
 WHERE ms.start_time >= f.opening_time
   AND ms.start_time <  f.closing_time
   AND (
       (ms.availability = 1
        AND EXISTS (SELECT 1 FROM unnest(ms.exceptions) AS e
                     WHERE e LIKE '% (H)' OR e LIKE '%(H)'))
    OR (ms.availability = 0
        AND NOT EXISTS (SELECT 1 FROM unnest(ms.exceptions) AS e
                         WHERE e LIKE '% (H)' OR e LIKE '%(H)'))
   );

-- 3) Availability-vs-business-hours invariant (generator-level;
-- should be 0). The reconciler doesn't manage this, but it's still
-- worth re-running after the reconciler to confirm nothing else
-- silently broke it.
SELECT count(*)
  FROM pc1.machineschedule ms
  JOIN pc1.facilities      f ON f.id = ms.facility_id
 WHERE (ms.start_time <  f.opening_time
        OR ms.start_time >= f.closing_time)
   AND ms.availability = 1;

-- 4) NULL availability anomaly (should be 0 in normal operation)
SELECT count(*) FROM pc1.machineschedule WHERE availability IS NULL;

-- 5) Hard-exception coverage spot-check -- which active rules
-- actually drove an availability change?
SELECT exception_ids, availability, count(*)
  FROM pc1.machineschedule
 WHERE cardinality(exception_ids) > 0
 GROUP BY exception_ids, availability
 ORDER BY count(*) DESC
 LIMIT 20;
```

---

## Operational pattern

### Routine maintenance

Run after any change to `pc1.scheduleexceptions`:

```powershell
# All facilities, default forward window
python reconcile_exceptions.py

# Single facility after a hand-edit to its exceptions
python reconcile_exceptions.py --facility=Inview-Fremont
```

The reconciler is idempotent — running it twice in a row produces
zero updates on the second run.

### After regenerating the calendar

If you wiped + regenerated `pc1.machineschedule` (e.g. after a
business-hours change), re-run the reconciler to re-apply the
exception overlay on the fresh rows. The generator writes empty
`exceptions[]` / `exception_ids[]` to every row; the reconciler
populates them.

### Cadence

Manual / on-demand. Same philosophy as the scrapers and the
generator: re-run when something upstream has changed. No cron.

---

## Performance

Measured on the live Inview Imaging tenant (`client_id = 1`) against
Supabase, against the May–June 2026 calendar window (31 days,
`slot_size = 15`, 10-hour business day, 475 active exceptions, 8
modalities at Inview-Fremont):

| Stage | Slots in window | Exceptions | Updates issued | Wall time | Notes |
|---|---|---|---|---|---|
| `--dry-run` | 23,040 | 475 | 0 (computed 6,312 would-update) | **~15 s** | Reads only; per-exception expansion summary printed. Wall time scales with the cost of the SELECT pagination + Python-side desired-state build. |
| First real write | 23,808 | 475 | 6,565 across 388 batched UPDATEs (387 distinct payloads, 6 workers) | **~1 m 26 s** | Updates `exceptions[]` / `exception_ids[]` / `availability` / `updated_at`. Payload-grouping collapsed 6,565 row UPDATEs into 388 actual PostgREST round-trips. Throughput ≈ 76 row-updates/sec across all workers. |
| Idempotent re-run | 23,808 | 475 | **0** | **~14 s** | No DB writes. Pure read + Python compare. Confirms determinism — every row's target state already matches current state. |

The dry-run vs. real-write timing difference (~15 s vs ~86 s) is
almost entirely the cost of the UPDATE round-trips themselves —
PostgREST UPDATEs against an indexed business key are ~2× slower
than INSERTs of the same row count, and the 388 sequential batches
(despite 6 parallel workers) are the dominant cost.

The idempotent-re-run time (14 s) is the **floor** for any
reconciler invocation — it's the cost of:
1. Reading the active exceptions (~475 rows).
2. Reading the in-range machineschedule rows (~23,808 rows across
   24 PostgREST pages of 1000).
3. Building the desired-state dict in Python.
4. Comparing each row's current vs. desired state.

So a routine "nothing changed since last run" reconciler invocation
costs ~15 s per facility — cheap enough to run after any exception
edit. The 1 m 26 s cost is what you pay when there are real
changes to apply.

### Scaling

Linear in slots-in-window (DB read cost) and slots-needing-update
(write cost). Doubling the window from 31 → 60 days roughly
doubles the dry-run time and the real-write time; doubling the
number of exceptions roughly doubles the desired-state build cost
but barely affects the write cost (it depends on slot coverage, not
on exception count).

A 91-day full-tenant horizon across both Antioch (10 modalities) +
Inview-Fremont (8 modalities) is expected to run in ~5 min real-write
or ~45 s idempotent re-run.

---

## Related

- [`pc1.machineschedule`](./machineschedule.md) — the table being
  reconciled; defines the paired-array invariant and the
  cross-tenant trigger
- [`pc1.scheduleexceptions`](./scheduleexceptions.md) — the source
  of truth for active rules; defines `recurrence` / `type` vocabulary
- [`generate_machineschedule.py`](./machineschedule_generator.md) —
  the Phase 2 writer that creates the blank rows the reconciler
  overlays
