# `pc1.modalities`

One row per **physical imaging machine** at a facility, sourced from
the tenant's RIS (currently NovaRIS). This is the table the
`novaRIS_modalities_scraper.py` on-demand sync job maintains.

Rows in `pc1.modalities` are **never hard-deleted** under normal
operation. Removals are represented by `is_active = false` and the
scraper can reactivate them automatically if the machine reappears in
the RIS.

---

## Identity

| Concept           | Field(s)                              |
|-------------------|---------------------------------------|
| Surrogate PK      | `id`                                  |
| **Business key**  | `(facility_id, source_record_key)`    |

The scraper upserts on the business key, **not** the surrogate `id`.
That means:

- `id` is stable as long as the row keeps existing.
- A row that is **soft-deactivated** keeps its `id` (and is reactivated
  in place).
- A row that is **hard-deleted** (manually or otherwise) and then
  re-scraped comes back with a **new** `id`. Any external FK pointing
  at the old `id` will dangle. Don't hard-delete from this table in
  production — use the `is_active` flag.

Migration `0001` enforces the business key via:

```sql
-- effectively (after the migration relaxes the legacy NovaRIS shape)
UNIQUE (facility_id, source_record_key)
```

…which is what the scraper passes to Supabase's
`on_conflict="facility_id,source_record_key"`.

---

## Columns the scraper writes

| Column                 | Source                                  | Notes |
|------------------------|-----------------------------------------|-------|
| `client_id`            | `--client-id` / env / default           | Tenant. |
| `facility_id`          | `pc1.facilities` lookup by `(client_id, facility_name)` | Resolved once per facility per run, cached in process. |
| `ris_system`           | hardcoded `'NovaRIS'`                   | Tag for which RIS produced the row. |
| `ris_modality_id`      | `NULL`                                  | NovaRIS grid has no internal modality id. |
| `ris_modality_code`    | parsed `Modality Type` cell             | e.g. `'US'`, `'CT'`, `'MR'`. |
| `ris_modality_name`    | `NULL`                                  | Not exposed by NovaRIS. |
| `modality_type`        | parsed `Modality Type` cell             | Same as `ris_modality_code` today. |
| `modality_machine`     | parsed `Name` cell                      | e.g. `'US1-F'`, `'FRE-MRI'`. |
| `source_record_key`    | parsed `Name` cell                      | Half of the business key. |
| `room`                 | parsed `Room` cell                      | May be NULL. |
| `status`               | parsed `Status` cell                    | `'Active'`, `'Inactive'`, `'UNKNOWN'`. |
| `ae_title`             | parsed `AE Title` cell                  | May be NULL. |
| `worklist_enabled`     | parsed `Worklist` checkbox              | Boolean. |
| `is_active`            | `true` on insert / reactivation         | See [Lifecycle](#lifecycle). |
| `content_hash`         | SHA-256 of source-side fields           | See [Content hash](#content-hash). |
| `ris_metadata`         | `{writer, facility_label}`              | Trace info — which scraper wrote the row, which facility label it parsed under. |
| `updated_at`           | `now()` on every write                  | |
| `synced_at`            | `now()` on every write                  | |
| `ris_last_synced_at`   | `now()` on every write                  | RIS-specific sync clock. |
| `created_at`           | set on insert; preserved on update      | Reactivations preserve the original `created_at`. |
| `created_by` / `updated_by` | `NULL` — automated writes          | |

Migration `0001` also drops the legacy NovaRIS-shape constraints
(`ris_modality_id NOT NULL`, the old `(client_id, ris_modality_id)`
unique key, the `(modality_code, client_id)` index) so the
`source_record_key`-based identity above can take over.

---

## Content hash

```python
content_hash = SHA-256(canonical_json({
    modality_machine,
    facility_label,
    room,
    status,
    modality_type,
    ae_title,
    worklist_enabled,
}))
```

Things deliberately **excluded** from the hash:

- `client_id`, `facility_id` — re-keying a facility (e.g. moving it
  between tenants) shouldn't churn every row's hash.
- Timestamps (`updated_at`, `synced_at`, …) — they change every run by
  definition.
- `is_active` — its transitions are explicit state, not "content".

This means a scraped row whose content matches the DB row exactly
short-circuits in the delta path with **zero writes** — no UPDATE
statement, no `updated_at` bump. The `unchanged=N` counter in the log
is the count of these short-circuits.

---

## Lifecycle

Per facility, per delta run, every scraped row falls into exactly one
of these buckets:

| Scenario in DB                              | Outcome              | Counter           |
|---------------------------------------------|----------------------|-------------------|
| Not present                                 | INSERT new row       | `insert`          |
| Present, `is_active = true`, hash matches   | No write             | `unchanged`       |
| Present, `is_active = true`, hash differs   | UPDATE in place      | `update`          |
| Present, `is_active = false`                | UPDATE + flip `is_active = true`, preserve `created_at` | `reactivated` (also counted in `update`) |

Plus, after iterating the scraped rows, the scraper computes the set
of `source_record_key` values present in DB for this facility but
**missing** from the scrape, and bulk-updates them to
`is_active = false` (counter: `deactivate`).

### Reactivate vs update — the double-count nuance

A reactivation IS an UPDATE under the hood (it writes the row with
`is_active = true` and a fresh `updated_at`), so the log shows it in
both columns:

```
[delta] Inview-Fremont: 27 scraped records  insert=0  update=1 (reactivated=1)  unchanged=26  deactivate=0 -> applied.
```

`update=1` and `reactivated=1` describe the **same one write**. The
parenthetical `reactivated=N` is the more specific number — it tells
you how many of the updates were status flips vs. true content
changes. If you ever see `update=5 (reactivated=1)`, that means 4
rows changed content + 1 row came back from the dead.

### Initial-load mode

`--initial-load` short-circuits all of the above and does, per facility:

1. `DELETE FROM pc1.modalities WHERE client_id=? AND facility_id=?`
2. Bulk insert every scraped row fresh.

This **does** burn `id`s — use it only when seeding a new facility for
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
- The `(facility_id, source_record_key)` business key is the join
  point for everything downstream (orders, schedules, etc.). Joining
  on `modality_machine` text or on `id` directly will eventually
  bite you.

---

## Common queries

```sql
-- All active modalities for a facility
SELECT source_record_key, modality_type, status, ae_title
  FROM pc1.modalities
 WHERE facility_id = (SELECT id FROM pc1.facilities
                       WHERE client_id = 1
                         AND facility_name = 'Inview-Fremont')
   AND is_active = true
 ORDER BY source_record_key;

-- Rows the scraper has soft-deactivated (i.e. the RIS no longer lists them)
SELECT source_record_key, updated_at
  FROM pc1.modalities
 WHERE client_id = 1
   AND is_active = false
 ORDER BY updated_at DESC;

-- Find rows that haven't been touched by a sync in a week — a smell
-- that the scraper isn't covering them
SELECT facility_id, source_record_key, ris_last_synced_at
  FROM pc1.modalities
 WHERE client_id = 1
   AND ris_last_synced_at < now() - interval '7 days'
 ORDER BY ris_last_synced_at;
```

---

## Related

- [`pc1.facilities` doc](./facilities.md) — supplies the
  `facility_id` and gates the scraper's iteration via `is_client`.
- [NovaRIS modalities scraper](./novaris_modalities_scraper.md) — the
  writer.
