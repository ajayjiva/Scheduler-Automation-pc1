# `pc1.proceduresestimate`

The standard-procedure catalog. One row per **procedure shape** —
i.e. "this is how the system should size and route an order for this
procedure." Sourced from the tenant's RIS (currently NovaRIS) by the
`novaRIS_standardprocedure_scraper.py` on-demand sync job.

Each row carries the timing (`required_time`, `required_slots`) and
routing (`modality_type`, `procedure_code` array) needed by the
scheduler to fit the procedure into a patient's appointment, plus
reserved-for-future columns that capture intake metadata the RIS
already exposes (`anatomical_area`, `exam_prep_instructions`,
`exam_prep_requires_prompt`).

Rows are **never hard-deleted** under normal operation. Removals are
represented by `is_active = false`; the scraper can reactivate them
automatically if the procedure reappears in the RIS.

---

## Identity

| Concept           | Field(s)                                                     |
|-------------------|--------------------------------------------------------------|
| Surrogate PK      | `id`                                                         |
| **Business key**  | `(client_id, COALESCE(facility_id, 0), COALESCE(modality_id, 0), source_record_key)` |

The business key is **four columns**, not two, because the same
`source_record_key` can legitimately appear up to four times for one
client — once per override-shape. See [Override
design](#override-design-four-row-shapes) below for the worked
example. The composite is enforced via a unique partial index:

```sql
CREATE UNIQUE INDEX proceduresestimate_source_unique_idx
    ON pc1.proceduresestimate (
        client_id,
        COALESCE(facility_id, 0::bigint),
        COALESCE(modality_id, 0::bigint),
        source_record_key
    )
    WHERE source_record_key IS NOT NULL;
```

Notes on the COALESCE sentinel:

- `bigserial` PKs start at 1, so no real `facilities.id` or
  `modalities.id` will ever be `0`. Sentinel `0` is safe as a NULL
  stand-in for index purposes.
- The partial `WHERE source_record_key IS NOT NULL` clause lets
  manually-inserted, app-side rows (with no upstream key) coexist
  without colliding with each other.

Like `pc1.modalities`, the **surrogate `id` is unstable across
hard-delete + re-scrape cycles** — if you DELETE a row and the next
scraper run inserts the procedure again, it comes back with a new
`id`. Any external FK pointing at the old `id` will dangle. Use the
`is_active` flag, not DELETE, to take a row out of rotation.

---

## Columns

### Foreign keys

| Column        | FK target                        | Null? | Notes |
|---------------|----------------------------------|-------|-------|
| `client_id`   | `pc1.clients(id)`                | NO    | Tenant scope. Defaults removed — must be set explicitly. |
| `facility_id` | `pc1.facilities(id)`             | YES   | NULL = applies to any facility (global default). Non-NULL = facility-specific override. |
| `modality_id` | `pc1.modalities(id)`             | YES   | NULL = applies to any machine. Non-NULL = pinned to a specific machine (per-machine override). |
| `created_by`  | `pc1.user_profiles(id)` ON DELETE SET NULL | YES | NULL when written by automation. |
| `updated_by`  | `pc1.user_profiles(id)` ON DELETE SET NULL | YES | NULL when written by automation. |

### Business fields

| Column                    | Type                  | Null? | Notes |
|---------------------------|-----------------------|-------|-------|
| `modality_type`           | `varchar(20)`         | YES   | E.g. `'CT'`, `'MR'`, `'US'`. Free-text from the RIS for now; not FK'd. |
| `procedure_code`          | `text[]`              | NO    | Array of CPT (or HCPCS) codes. One procedure can carry multiple codes — e.g. `{'70544','70545'}` for a multi-region MRA. Indexed via GIN for `procedure_code @> ARRAY[...]` containment lookups. |
| `procedure_desc`          | `text`                | NO    | Human-readable description from the RIS (e.g. `'MR ANGIO HEAD (70544,70545)'`). |
| `required_time`           | `integer`             | YES   | Minutes from the RIS wizard. May be NULL if the upstream doesn't expose it. |
| `required_slots`          | `integer`             | NO    | `ceil(required_time / slot_size)` — the count the scheduler uses to size a modality block. The writer computes this; readers do not recompute. |
| `anatomical_area`         | `text`                | YES   | Reserved-for-future. Captured from the RIS but not currently consumed by the scheduler. |
| `exam_prep_instructions`  | `text`                | YES   | Same — reserved-for-future. |
| `exam_prep_requires_prompt` | `boolean`           | NO    | Same. Defaults `false`. |

### Soft-delete

| Column      | Type      | Null? | Default | Notes |
|-------------|-----------|-------|---------|-------|
| `is_active` | `boolean` | NO    | `true`  | `false` only when the row is no longer present in the RIS. See [Lifecycle](#lifecycle). |

### Source-system tracking

| Column              | Type            | Null? | Default       | Notes |
|---------------------|-----------------|-------|---------------|-------|
| `source_record_key` | `varchar(255)`  | YES   | —             | Stable RIS-side identifier (NovaRIS `standardProcedureID`). Half of the business key. NULL allowed so manually-inserted rows can coexist with scraper rows. |
| `content_hash`      | `varchar(255)`  | YES   | —             | SHA-256 of business fields, for delta-sync skip-if-unchanged. See [Content hash](#content-hash). |
| `ris_system`        | `varchar(50)`   | YES   | `'konica_exa'`| Which RIS produced this row. Lets a future non-NovaRIS scraper share the table. |
| `ris_sync_status`   | `varchar(20)`   | YES   | `'synced'`    | Free-text sync state. Convention: `'synced'` = last sync succeeded; other values reserved for partial-failure flagging. |
| `ris_last_synced_at`| `timestamptz`   | YES   | —             | When the RIS-side data was last pulled successfully. |
| `ris_metadata`      | `jsonb`         | YES   | —             | Flexible source-system trace (writer name, raw payload fragments, parser version, etc.). |
| `synced_at`         | `timestamptz`   | YES   | —             | When PC1 last wrote this row from a sync. Distinct from `ris_last_synced_at`: the former is "our clock," the latter is "the RIS's clock." |

### Audit

| Column        | Type            | Null? | Default | Notes |
|---------------|-----------------|-------|---------|-------|
| `created_at`  | `timestamptz`   | NO    | `now()` | Set once on INSERT. Preserved on UPDATE. |
| `updated_at`  | `timestamptz`   | NO    | `now()` | Writers must set explicitly on every UPDATE — the DEFAULT only fires on INSERT. |
| `created_by`  | `bigint`        | YES   | —       | FK to `pc1.user_profiles`. NULL for scraper writes. |
| `updated_by`  | `bigint`        | YES   | —       | Same. |

---

## Override design — four row shapes

The same `source_record_key` can legitimately appear up to four times
under one client, one shape per `(facility_id, modality_id)` pair:

| `facility_id` | `modality_id` | Meaning                                |
|---------------|---------------|----------------------------------------|
| `NULL`        | `NULL`        | **Global default.** Applies anywhere this client doesn't have a more specific row. |
| `X`           | `NULL`        | **Facility-level override.** Applies to any machine at facility `X`. Beats global for orders at `X`. |
| `NULL`        | `K`           | **Per-machine global.** Pinned to a specific machine regardless of facility. Rare; intended for cross-facility identical machines. |
| `X`           | `K`           | **Facility + machine override.** Most specific. Used when one machine at one facility behaves differently from siblings. |

The unique partial index allows all four to coexist for the same
`source_record_key`. Override **resolution** (which row wins for a
given order) is the scheduler's job, not the schema's — the schema
just guarantees the four shapes never collide.

### Why this exists

Different machines at the same facility — and the same procedure at
different facilities — sometimes need different scan times. A newer
MRI may finish a procedure in 30 minutes that takes an older MRI 45.
A facility in a high-volume region may run shorter slot windows than
its sibling. Carrying one row per shape lets the scheduler resolve to
the **most specific applicable timing** at query time without forking
the procedure into separate `source_record_key`s.

### Worked example

For procedure `70544` (CPT MR Angio Head) at client 1, with overrides
at facility `42` (Fremont) for machines `5` (MR1) and `6` (MR2):

| `id` | `facility_id` | `modality_id` | `procedure_code` | `required_slots` | Meaning |
|------|---------------|---------------|------------------|------------------|---------|
| 100  | `NULL`        | `NULL`        | `{'70544'}`      | 3                | Default everywhere |
| 101  | 42            | `NULL`        | `{'70544'}`      | 3                | Fremont default (same as global here) |
| 102  | 42            | 5             | `{'70544'}`      | 3                | Fremont MR1 — falls back to default |
| 103  | 42            | 6             | `{'70544'}`      | 2                | Fremont MR2 — faster |

A patient with `70544` ordered for Fremont gets:
- 3 slots on MR1 (row 102 wins by `(facility_id, modality_id)`
  specificity)
- **2 slots** on MR2 (row 103 wins — the faster scanner)

A patient with `70544` at a different facility falls back to row 100
at 3 slots.

The "one machine per modality per visit" constraint (a patient never
hops between MR1 and MR2 within a single appointment) is what makes
this resolution tractable — each `(modality, machine)` pair gets one
slot count, and the scheduler enumerates machine assignments at the
appointment level.

---

## Cross-tenant safety trigger

The `BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id`
trigger `trg_proceduresestimate_consistency_check` fires the function
`pc1.check_proceduresestimate_consistency()` to validate that all three
columns belong to the same tenant. Without it, a buggy writer could
silently insert a row tying client A's procedure to client B's
facility — a data-integrity bug that wouldn't surface until a query
joined them and got nonsense.

The trigger checks:

1. If `facility_id IS NOT NULL`:
   - Look up `pc1.facilities(id = NEW.facility_id).client_id`.
   - Row doesn't exist → raise.
   - Row exists but `client_id` differs from `NEW.client_id` → raise.
2. If `modality_id IS NOT NULL`:
   - Same two checks against `pc1.modalities`.

Error messages are explicit about which column mismatched and what
the conflicting IDs were:

```
ERROR: proceduresestimate.client_id=1 does not match
       pc1.facilities(id=42).client_id=2

ERROR: proceduresestimate.facility_id=99999 references a row that
       does not exist in pc1.facilities
```

UPDATEs that touch only other columns (e.g. `content_hash`,
`required_time`, `is_active`) skip the trigger entirely via the
`OF client_id, facility_id, modality_id` clause — so the hot path
(delta-sync content updates) costs zero trigger work.

Per-row trigger cost is two indexed PK lookups (~50-100µs total),
invisible against any scraper or app workload.

---

## Content hash

```python
content_hash = SHA-256(canonical_json({
    facility_id,
    modality_id,
    modality_type,
    procedure_code,
    procedure_desc,
    required_time,
    required_slots,
    anatomical_area,
    exam_prep_instructions,
    exam_prep_requires_prompt,
}))
```

Things deliberately **excluded** from the hash:

- `client_id` — re-keying a tenant shouldn't churn every hash.
- Audit timestamps (`updated_at`, `synced_at`, `ris_last_synced_at`)
  — they change every run by definition.
- `is_active` — its transitions are explicit state, not "content."
- `created_by` / `updated_by` — orthogonal to the content.

A scraped row whose computed hash matches the DB row's stored hash
short-circuits in the delta path with **zero writes** — no UPDATE
statement, no `updated_at` bump. The `unchanged=N` counter in the
scraper log is the count of these short-circuits.

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
`source_record_key` values present in the DB for this `(client_id,
facility_id, modality_id)` scope but **missing** from the scrape, and
bulk-updates them to `is_active = false` (counter: `deactivate`).

### Initial-load mode

`--initial-load` short-circuits all of the above:

1. `DELETE FROM pc1.proceduresestimate WHERE client_id = ? AND source_record_key IS NOT NULL`
   — wipe only scraper-managed rows for this client, preserving any
   manually-inserted app-side rows (where `source_record_key IS NULL`).
2. Bulk insert every scraped row fresh.

This **does** burn `id`s — use it only when seeding a new tenant for
the first time, or after intentional manual cleanup. It is not the
routine on-demand pattern.

---

## Operational guidance

- **Don't `DELETE` rows manually in production.** Use
  `UPDATE … SET is_active = false` if you need to take a row out of
  rotation; the scraper will leave it alone (unless it reappears in
  the RIS, in which case it'll be reactivated).
- A row's `created_at` is the moment the row was first inserted, full
  stop. Reactivations preserve it. Hard-delete + re-scrape resets it.
- A row's `updated_at` advances on every UPDATE the scraper performs,
  including reactivations. `unchanged` rows do NOT bump it.
- **Override rows are forever** — the scraper writes only global
  rows (`facility_id IS NULL`, `modality_id IS NULL`). Facility-level
  and per-machine override rows are inserted manually (or by a future
  override-management tool) and the scraper leaves them alone. A
  human-curated override won't be clobbered by the next sync.

---

## Common queries

```sql
-- All active procedures globally for a client
SELECT id, modality_type, procedure_code, procedure_desc, required_slots
  FROM pc1.proceduresestimate
 WHERE client_id = 1
   AND facility_id IS NULL
   AND modality_id IS NULL
   AND is_active = true
 ORDER BY modality_type, procedure_desc;

-- Find a procedure by CPT code (uses the GIN index on procedure_code)
SELECT id, facility_id, modality_id, procedure_desc, required_slots
  FROM pc1.proceduresestimate
 WHERE client_id = 1
   AND procedure_code @> ARRAY['70544']::text[]
   AND is_active = true
 ORDER BY facility_id NULLS LAST, modality_id NULLS LAST;
-- Returns up to 4 rows per procedure (one per override shape).

-- All facility-level overrides for a tenant
SELECT pe.id, f.facility_name, pe.modality_type,
       pe.procedure_desc, pe.required_slots
  FROM pc1.proceduresestimate pe
  JOIN pc1.facilities f ON f.id = pe.facility_id
 WHERE pe.client_id = 1
   AND pe.facility_id IS NOT NULL
   AND pe.modality_id IS NULL
   AND pe.is_active = true
 ORDER BY f.facility_name, pe.procedure_desc;

-- All per-machine overrides for a tenant
SELECT pe.id, f.facility_name, m.modality_machine,
       pe.procedure_desc, pe.required_slots
  FROM pc1.proceduresestimate pe
  JOIN pc1.modalities  m ON m.id = pe.modality_id
  JOIN pc1.facilities  f ON f.id = m.facility_id
 WHERE pe.client_id = 1
   AND pe.modality_id IS NOT NULL
   AND pe.is_active = true
 ORDER BY f.facility_name, m.modality_machine, pe.procedure_desc;

-- Rows the scraper has soft-deactivated (i.e. the RIS no longer lists them)
SELECT id, procedure_desc, source_record_key, updated_at
  FROM pc1.proceduresestimate
 WHERE client_id = 1
   AND is_active = false
 ORDER BY updated_at DESC;

-- Find procedures that haven't been synced in over a week —
-- a smell that the scraper isn't covering them
SELECT id, procedure_desc, ris_last_synced_at
  FROM pc1.proceduresestimate
 WHERE client_id = 1
   AND is_active = true
   AND source_record_key IS NOT NULL
   AND ris_last_synced_at < now() - interval '7 days'
 ORDER BY ris_last_synced_at;
```

---

## Related

- [`pc1.facilities` doc](./facilities.md) — supplies `facility_id`
  and gates scraper scope via `is_client`.
- [`pc1.modalities` doc](./modalities.md) — supplies `modality_id`
  for per-machine override rows.
- NovaRIS standard-procedures scraper doc — coming with Phase 2 of
  this feature branch.
