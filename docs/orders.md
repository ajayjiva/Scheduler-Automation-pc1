# `pc1.orders` (STUB — Phase 3.5)

The patient-order table. One row per ordered procedure for a patient at a
facility. Orders are the top-level input to the scheduling engine: the engine
pulls a patient's orders, expands each against the procedure catalog
(`pc1.proceduresestimate`) through the [`pc1.orders_v`](./orders_v.md) view, and
computes appointment options.

> **This is a deliberate stub.** The team's real orders schema is still in
> design (Phase 4). To unblock the scheduling-engine port (Phase 3.6) we ship a
> minimal table carrying only the columns `pc1.orders_v` needs, plus
> hand-crafted test data. When the real schema lands it **replaces** this table;
> `pc1.orders_v`'s column contract stays stable so the engine code is
> unaffected. See `docs/session-handoff.md` §8.

---

## Design notes (the "why")

### `cpt_codes` is eliminated
The legacy system carried a `cpt_codes` lookup table mapping order text to CPT
codes. **pc1 drops it.** Orders link straight to the procedure catalog by
description:

```
orders.procedure_description  =  proceduresestimate.procedure_desc   (exact text)
```

`procedure_code` (the CPT array) is then sourced **from the catalog** via the
view, not stored on the order. The `studies` table is likewise **not used** for
scheduling.

> **Exact-match contract.** `procedure_description` must match
> `proceduresestimate.procedure_desc` character-for-character. Drift (a typo, a
> trailing space, a renamed procedure) means the join finds nothing and the
> order silently drops out of `orders_v`. This is the same naming-exactness
> discipline as `pc1.facilities` ↔ NovaRIS facility names. Keep order text in
> lockstep with the catalog.

### `ris_*` vs plain columns
A convention used across pc1 (introduced on `pc1.patients`, applied here):

| Prefix | Holds | Written by |
|---|---|---|
| `ris_*` | RAW value, exactly as it came from the source RIS | the RIS scraper |
| plain (un-prefixed) | the value used by INTERNAL logic (engine, views) | starts as a copy of the `ris_*` value; later may carry cleanup/normalization |

The split lets us normalize (name casing, language-code mapping, status
canonicalization, …) into the plain column **without mutating the
source-of-truth `ris_*` value**. The stub only creates the plain business
columns the view reads; the real Phase 4 schema will add the `ris_*`
counterparts (`ris_order_status`, `ris_order_type`, `ris_requesting_date`).

> **Staleness dependency.** Once a real orders scraper writes `ris_*`, it (or a
> trigger) must also refresh the plain columns, or they go stale. Same caveat
> applies to the new plain columns on `pc1.patients` (see
> `migrations/0006_add_patients_internal_columns.sql`).

### Open question — `order_type` 'P' / 'S'
NovaRIS appears to emit an order type of `P` or `S`. **The meaning is
unconfirmed.** Until it's pinned, `order_type` has **no CHECK constraint**. Add
one (and document the vocabulary) once confirmed. `order_status` is likewise
TBD / un-constrained.

---

## Identity

| Concept       | Field(s)            |
|---------------|---------------------|
| Surrogate PK  | `id`                |
| Tenant scope  | `client_id`         |
| FK targets    | `facility_id` → `pc1.facilities`, `patient_id` → `pc1.patients` |

No `source_record_key` / `content_hash` / `ris_*` sync quintet on the stub —
orders aren't scraped yet. Those arrive with the Phase 4 schema.

## Columns

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `client_id` | `bigint NOT NULL` | FK `pc1.clients`. No DEFAULT (multi-tenant footgun) |
| `facility_id` | `bigint NOT NULL` | FK `pc1.facilities`. The order's facility |
| `patient_id` | `bigint NOT NULL` | FK `pc1.patients` |
| `procedure_description` | `text NOT NULL` | Exact-match join key to `proceduresestimate.procedure_desc` |
| `preferred_language` | `varchar(50) NULL` | Plain/internal |
| `order_status` | `varchar(50) NULL` | Plain/internal. Vocabulary TBD — no CHECK |
| `order_type` | `varchar(20) NULL` | Plain/internal. NovaRIS 'P'/'S', meaning TBD — no CHECK |
| `requesting_date` | `date NULL` | Date the order was requested. Widen to `timestamptz` if the RIS carries a time |
| `is_active` | `boolean NOT NULL DEFAULT true` | Soft-delete |
| `created_at` / `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Audit |
| `created_by` / `updated_by` | `bigint NULL` | FK `pc1.user_profiles ON DELETE SET NULL` |

## Cross-tenant safety trigger

`pc1.check_orders_consistency()` fires `BEFORE INSERT OR UPDATE OF client_id,
facility_id, patient_id`. It validates that `client_id` matches both the FK'd
**facility's** and the FK'd **patient's** `client_id`, turning a cross-tenant
mis-link into a loud INSERT failure. Matches the trigger pattern on
`pc1.proceduresestimate` / `pc1.scheduleexceptions`.

## Verification queries (post-migration)

```sql
-- table exists with the expected columns
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'pc1' AND table_name = 'orders'
ORDER BY ordinal_position;

-- trigger present
SELECT tgname FROM pg_trigger
WHERE tgrelid = 'pc1.orders'::regclass AND NOT tgisinternal;

-- cross-tenant guard works (should RAISE):
--   INSERT INTO pc1.orders (client_id, facility_id, patient_id, procedure_description)
--   VALUES (1, <facility_of_other_client>, <patient_of_client_1>, 'X');
```

## Related

- [`pc1.orders_v`](./orders_v.md) — the compatibility-layer view over this table
- [`pc1.proceduresestimate`](./proceduresestimate.md) — the catalog the view joins against
- `pc1.patients` — patient source (no doc yet); plain internal columns added in `migrations/0006_add_patients_internal_columns.sql`
- `docs/session-handoff.md` §8 — full Phase 3.5 design context
