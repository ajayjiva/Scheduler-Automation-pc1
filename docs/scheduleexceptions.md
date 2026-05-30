# `pc1.scheduleexceptions`

The scheduling-exception rule set. One row per rule — LUNCH, holiday,
machine downtime, technician out-of-office, etc. — that blocks or
annotates one or more slots on the appointment calendar. Sourced from
the tenant's RIS (currently NovaRIS, `ModalityScheduling.aspx`) by the
`novaRIS_exception_scraper.py` on-demand sync job.

Each row carries the rule window (`start_date`, `start_time`,
`end_date`, `end_time`), the recurrence (`recurrence` plus the flat
day-of-week / day-of-month masks), and the rule's effect on
availability (`type = 'Hard'` blocks; `'Soft'` is display-only). The
downstream reconciler ([`reconcile_exceptions.py`](./reconcile_exceptions.md))
iterates active rules against `pc1.machineschedule` to drive per-slot
`availability` and `exceptions[]` updates.

Rows are **never hard-deleted** under normal operation. Removals are
represented by `is_active = false`; the scraper can reactivate them
automatically if the rule reappears in the RIS.

---

## Identity

| Concept           | Field(s)                                                |
|-------------------|---------------------------------------------------------|
| Surrogate PK      | `id`                                                    |
| **Business key**  | `(client_id, facility_id, source_record_key)`           |

The business key is three columns: tenant, facility, and the RIS-side
identifier (NovaRIS `frequencyId`). Unlike `pc1.proceduresestimate`,
there's no override pattern here — exception rules are operational
events, not catalog entries with per-machine timing variants. The
uniqueness is enforced via a partial unique index:

```sql
CREATE UNIQUE INDEX scheduleexceptions_source_unique_idx
    ON pc1.scheduleexceptions (
        client_id,
        facility_id,
        source_record_key
    )
    WHERE source_record_key IS NOT NULL;
```

The partial `WHERE source_record_key IS NOT NULL` clause lets
manually-inserted, app-side rows (with no upstream RIS key) coexist
without colliding with each other.

The surrogate `id` is **unstable across hard-delete + re-scrape
cycles** — if you DELETE a row and the next scraper run inserts the
rule again, it comes back with a new `id`. Use `is_active`, not
DELETE, to take a rule out of rotation.

---

## Columns

### Foreign keys

| Column        | FK target                        | Null? | Notes |
|---------------|----------------------------------|-------|-------|
| `client_id`   | `pc1.clients(id)`                | NO    | Tenant scope. No DEFAULT — must be set explicitly. |
| `facility_id` | `pc1.facilities(id)`             | YES   | Always set by the scraper (NovaRIS rules are facility-scoped). NULL allowed for forward compatibility with RIS systems that may permit cross-facility rules. |
| `modality_id` | `pc1.modalities(id)`             | YES   | Always set by the scraper (NovaRIS rules are machine-scoped). NULL allowed for forward compatibility with future machine-agnostic rules. |
| `created_by`  | `pc1.user_profiles(id) ON DELETE SET NULL` | YES | NULL when written by automation. |
| `updated_by`  | `pc1.user_profiles(id) ON DELETE SET NULL` | YES | NULL when written by automation. |

### Business fields

| Column            | Type                  | Null? | Notes |
|-------------------|-----------------------|-------|-------|
| `modality_type`   | `varchar(20)`         | YES   | E.g. `'MRI'`, `'CT'`, `'US'`. Free-text snapshot from the RIS for reporting without a JOIN to `pc1.modalities`. |
| `description`     | `text`                | YES   | Free-text label (e.g. `'LUNCH'`, `'STAFF MEETING'`, `'HOLIDAY'`). |
| `start_date`      | `date`                | YES   | First date the rule is active (facility-local). |
| `start_time`      | `time`                | YES   | Daily start time within the rule window (facility-local). |
| `end_date`        | `date`                | YES   | Last date the rule is active. NULL = open-ended. |
| `end_time`        | `time`                | YES   | Daily end time within the rule window. |
| `recurrence`      | `text`                | YES   | CHECK constrained to `'None'`, `'Daily'`, `'Weekly'`, `'Monthly'`. See [Recurrence semantics](#recurrence-semantics). |
| `type`            | `text`                | YES   | CHECK constrained to `'Hard'` or `'Soft'`. `'Hard'` reduces availability to 0; `'Soft'` is display-only. |
| `repeat_every`    | `integer`             | NO    | DEFAULT `1`. The "every N" interval for Weekly/Monthly. No current tenant uses values other than 1, but NovaRIS exposes the field and future tenants may. |
| `weekdays_only`   | `boolean`             | NO    | DEFAULT `false`. For `Daily` recurrence: Mon-Fri only when true. |

### Recurrence masks

| Column                                | Type      | Null? | Default | Notes |
|---------------------------------------|-----------|-------|---------|-------|
| `is_sunday` … `is_saturday`           | `boolean` | NO    | `false` | For `Weekly` recurrence: which days of the week the rule applies. All-false on non-Weekly rules. |
| `day_1` … `day_31`                    | `boolean` | NO    | `false` | For `Monthly` recurrence: which days of the month the rule applies. All-false on non-Monthly rules. Days that don't exist in a given month (e.g. `day_31` in February) are silently skipped by the reconciler. |

### Soft-delete

| Column      | Type      | Null? | Default | Notes |
|-------------|-----------|-------|---------|-------|
| `is_active` | `boolean` | NO    | `true`  | `false` only when the rule is no longer present in the RIS. See [Lifecycle](#lifecycle). |

### Source-system tracking

| Column              | Type            | Null? | Default       | Notes |
|---------------------|-----------------|-------|---------------|-------|
| `source_record_key` | `varchar(255)`  | YES   | —             | NovaRIS `frequencyId`. Part of the business key. NULL allowed so manually-inserted rules coexist with scraper rows. |
| `content_hash`      | `varchar(255)`  | YES   | —             | SHA-256 of business fields, for delta-sync skip-if-unchanged. See [Content hash](#content-hash). |
| `ris_system`        | `varchar(50)`   | YES   | `'konica_exa'`| Which RIS produced this row. |
| `ris_sync_status`   | `varchar(20)`   | YES   | `'synced'`    | Free-text sync state. |
| `ris_last_synced_at`| `timestamptz`   | YES   | —             | When the RIS-side data was last pulled successfully. |
| `ris_metadata`      | `jsonb`         | YES   | —             | Flexible source-system trace (writer name, NovaRIS modality dropdown ID, etc.). |
| `synced_at`         | `timestamptz`   | YES   | —             | When PC1 last wrote this row from a sync. |

### Audit

| Column        | Type            | Null? | Default | Notes |
|---------------|-----------------|-------|---------|-------|
| `created_at`  | `timestamptz`   | NO    | `now()` | Set once on INSERT. Preserved on UPDATE. |
| `updated_at`  | `timestamptz`   | NO    | `now()` | Writers must set explicitly on every UPDATE — the DEFAULT only fires on INSERT. |
| `created_by`  | `bigint`        | YES   | —       | FK to `pc1.user_profiles`. NULL for scraper writes. |
| `updated_by`  | `bigint`        | YES   | —       | Same. |

---

## Recurrence semantics

| `recurrence` value | Meaning | Relevant columns | Reconciler behavior ([`reconcile_exceptions.py`](./reconcile_exceptions.md)) |
|---|---|---|---|
| `None`     | One-off rule active only on `start_date`. | `start_date`, `start_time`, `end_time` (and optionally `end_date` if multi-day one-off) | Block slots on that single date in `[start_time, end_time)`. |
| `Daily`    | Repeats every day in `[start_date, end_date]`. | `weekdays_only` | If `weekdays_only=true`, skip Sat/Sun; else every day. |
| `Weekly`   | Repeats on the chosen day(s) of week in `[start_date, end_date]`. | `is_sunday`..`is_saturday`, `repeat_every` | Block on each chosen weekday. `repeat_every>1` means "every N weeks." |
| `Monthly`  | Repeats on the chosen day(s) of month in `[start_date, end_date]`. | `day_1`..`day_31`, `repeat_every` | Block on each chosen day-of-month. `repeat_every>1` means "every N months." Days that don't exist in a month (Feb 31) are silently skipped. |

The CHECK constraint on `recurrence` pins values to the four-element
vocabulary above. The scraper and any other writer will fail loudly
at INSERT time if they ever produce a typo or new value.

---

## Cross-tenant safety trigger

The `BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id`
trigger `trg_scheduleexceptions_consistency_check` fires the function
`pc1.check_scheduleexceptions_consistency()` to validate that all three
columns belong to the same tenant. Same shape as the
`pc1.proceduresestimate` trigger.

The trigger checks:

1. If `facility_id IS NOT NULL`:
   - Look up `pc1.facilities(id = NEW.facility_id).client_id`.
   - Row doesn't exist → raise.
   - Row exists but `client_id` differs from `NEW.client_id` → raise.
2. If `modality_id IS NOT NULL`:
   - Same two checks against `pc1.modalities`.

Error messages are explicit:

```
ERROR: scheduleexceptions.client_id=1 does not match
       pc1.facilities(id=42).client_id=2

ERROR: scheduleexceptions.modality_id=99999 references a row that
       does not exist in pc1.modalities
```

UPDATEs that touch only non-FK columns (e.g. `content_hash`,
`description`, `is_active`) skip the trigger entirely via the
`OF client_id, facility_id, modality_id` clause — so the hot path
(delta-sync content updates) costs zero trigger work.

---

## Content hash

```python
content_hash = SHA-256(canonical_json({
    facility_id,
    modality_id,
    modality_type,
    description,
    start_date,
    start_time,
    end_date,
    end_time,
    recurrence,
    type,
    repeat_every,
    weekdays_only,
    is_sunday, is_monday, is_tuesday, is_wednesday,
    is_thursday, is_friday, is_saturday,
    day_1, day_2, ..., day_31,
}))
```

Things deliberately **excluded** from the hash:

- `client_id` — re-keying a tenant shouldn't churn every hash.
- Audit timestamps (`updated_at`, `synced_at`, `ris_last_synced_at`)
  — they change every run by definition.
- `is_active` — its transitions are explicit state, not "content."
- `created_by` / `updated_by` — orthogonal to the content.

A scraped row whose computed hash matches the DB row's stored hash
short-circuits in the delta path with **zero writes**. The
`unchanged=N` counter in the scraper log is the count of these
short-circuits.

---

## Lifecycle

Per scraper delta run, every scraped row falls into exactly one
bucket:

| Scenario in DB                              | Outcome                                          | Counter           |
|---------------------------------------------|--------------------------------------------------|-------------------|
| Not present                                 | INSERT new row                                   | `insert`          |
| Present, `is_active = true`, hash matches   | No write                                         | `unchanged`       |
| Present, `is_active = true`, hash differs   | UPDATE in place, bump `updated_at`               | `update`          |
| Present, `is_active = false`                | UPDATE + flip `is_active = true`, preserve `created_at` | `reactivated` (also counted in `update`) |

After iterating the scraped rows, the scraper computes the set of
`source_record_key` values present in the DB for the `(client_id,
facility_id)` scope but **missing** from the scrape, and bulk-updates
them to `is_active = false` (counter: `deactivate`).

### Initial-load mode

`--initial-load` short-circuits all of the above:

1. `DELETE FROM pc1.scheduleexceptions WHERE client_id = ? AND
   facility_id = ? AND source_record_key IS NOT NULL` — wipe only
   scraper-managed rows for this (client, facility), preserving any
   manually-inserted rows (where `source_record_key IS NULL`).
2. Bulk insert every scraped row fresh.

This **does** burn `id`s — use it only when seeding a new tenant for
the first time, or after intentional manual cleanup.

---

## Operational guidance

- **Don't `DELETE` rows manually in production.** Use
  `UPDATE … SET is_active = false` if you need to take a rule out of
  rotation; the scraper will leave it alone (unless it reappears in
  the RIS, in which case it'll be reactivated).
- A row's `created_at` is the moment the row was first inserted, full
  stop. Reactivations preserve it. Hard-delete + re-scrape resets it.
- A row's `updated_at` advances on every UPDATE the scraper performs,
  including reactivations. `unchanged` rows do NOT bump it.
- **Past-dated rules are preserved.** The scraper writes every rule
  NovaRIS returns regardless of `start_date` / `end_date`. The
  downstream reconciler (Phase 3) is what decides which rules apply
  to current-or-future calendar slots.

---

## Common queries

```sql
-- All active exception rules for a tenant + facility
SELECT id, modality_id, modality_type, description, type, recurrence,
       start_date, end_date, start_time, end_time
  FROM pc1.scheduleexceptions
 WHERE client_id = 1
   AND facility_id = 42
   AND is_active = true
 ORDER BY start_date, start_time;

-- All Hard-type rules currently in effect for a tenant
SELECT se.id, f.facility_name, m.modality_machine,
       se.description, se.recurrence,
       se.start_date, se.end_date, se.start_time, se.end_time
  FROM pc1.scheduleexceptions se
  JOIN pc1.facilities f ON f.id = se.facility_id
  LEFT JOIN pc1.modalities m ON m.id = se.modality_id
 WHERE se.client_id = 1
   AND se.is_active = true
   AND se.type = 'Hard'
   AND se.start_date <= current_date
   AND (se.end_date IS NULL OR se.end_date >= current_date)
 ORDER BY f.facility_name, m.modality_machine, se.start_date;

-- All recurring rules by recurrence type
SELECT recurrence, count(*) AS n
  FROM pc1.scheduleexceptions
 WHERE client_id = 1
   AND is_active = true
 GROUP BY recurrence
 ORDER BY recurrence;

-- Weekly Monday-Friday lunch rules for a facility
SELECT id, modality_id, description, start_time, end_time
  FROM pc1.scheduleexceptions
 WHERE client_id = 1
   AND facility_id = 42
   AND is_active = true
   AND recurrence = 'Weekly'
   AND is_monday AND is_tuesday AND is_wednesday
   AND is_thursday AND is_friday
   AND NOT is_saturday AND NOT is_sunday;

-- Rules the scraper has soft-deactivated (i.e. NovaRIS no longer lists them)
SELECT id, description, source_record_key, updated_at
  FROM pc1.scheduleexceptions
 WHERE client_id = 1
   AND is_active = false
 ORDER BY updated_at DESC
 LIMIT 50;

-- Find rules that haven't been synced in over a day — a smell that the
-- scraper isn't covering them anymore
SELECT id, description, ris_last_synced_at
  FROM pc1.scheduleexceptions
 WHERE client_id = 1
   AND is_active = true
   AND source_record_key IS NOT NULL
   AND ris_last_synced_at < now() - interval '1 day'
 ORDER BY ris_last_synced_at;
```

---

## Verification queries (post-migration)

Run these in the Supabase SQL editor after applying
`migrations/0003_create_pc1_scheduleexceptions.sql` to confirm the
shape landed correctly:

```sql
-- Table exists and is empty
SELECT count(*) FROM pc1.scheduleexceptions;

-- Columns + types + nullability + defaults
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = 'pc1' AND table_name = 'scheduleexceptions'
 ORDER BY ordinal_position;

-- All FKs
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = 'pc1.scheduleexceptions'::regclass
   AND contype = 'f';

-- CHECK constraints
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conrelid = 'pc1.scheduleexceptions'::regclass
   AND contype = 'c';

-- All indexes
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE schemaname = 'pc1' AND tablename = 'scheduleexceptions'
 ORDER BY indexname;

-- Trigger present
SELECT tgname, pg_get_triggerdef(oid)
  FROM pg_trigger
 WHERE tgrelid = 'pc1.scheduleexceptions'::regclass
   AND NOT tgisinternal;
```

---

## Related

- [`pc1.facilities`](./facilities.md) — supplies `facility_id` and
  gates scraper scope via `is_client`.
- [`pc1.modalities`](./modalities.md) — supplies `modality_id`.
- [`pc1.proceduresestimate`](./proceduresestimate.md) — sibling
  catalog table; same audit / source-tracking column conventions.
- [`pc1.machineschedule`](./machineschedule.md) — the slot calendar
  table whose `exception_ids` arrays carry this table's
  `source_record_key` values. The exception reconciler (Phase 3,
  deferred) is the bridge: it expands active rules here into
  per-slot overlays there.
- NovaRIS exception scraper doc — coming with Phase 2 of this
  feature branch.
