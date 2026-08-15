# `pc1.orders`

The patient-order table. One row per ordered procedure for a patient at a
facility. Orders are the top-level input to the scheduling engine: the engine
pulls a patient's orders, expands each against the procedure catalog
(`pc1.proceduresestimate`) through the [`pc1.orders_v`](./orders_v.md) view, and
computes appointment options.

`pc1.orders` is a **RIS-shaped table** (like `pc1.patients`): most business
fields arrive raw from the source RIS under a `ris_*` prefix
(`ris_order_id`, `ris_order_status`, `ris_order_type`, `ris_requesting_date`,
`ris_billing_type`, insurance/accident/location fields, …) plus the standard
`ris_system` / `ris_sync_status` / `ris_last_synced_at` / `ris_metadata` sync
quintet and audit columns.

> **History.** The table pre-existed the Phase 3.5 work (the old session-handoff
> snapshot that said "`pc1.orders`: not yet created" was stale). Phase 3.5 does
> **not** create it — it ADDs the plain internal columns the view needs
> (`migrations/0008_alter_pc1_orders_internal_columns.sql`).

---

## Design notes (the "why")

### `ris_*` vs plain columns
The convention used across pc1 (`pc1.patients`, and here):

| Prefix | Holds | Written by |
|---|---|---|
| `ris_*` | RAW value, exactly as it came from the source RIS | the RIS scraper |
| plain (un-prefixed) | the value used by INTERNAL logic (engine, views) | starts as a copy of the `ris_*` value; later may carry cleanup/normalization |

The split lets us normalize (status canonicalization, language-code mapping, …)
into the plain column **without mutating the source-of-truth `ris_*` value**.
Phase 3.5 adds these plain columns and backfills them from their raw sources:

| Plain column | Raw source |
|---|---|
| `order_status` | `ris_order_status` |
| `order_type` | `ris_order_type` |
| `requesting_date` | `ris_requesting_date` |
| `procedure_description` | `ris_procedure_description` (also added — see below) |
| `preferred_language` | *(internal-only; no RIS source)* |
| `is_active` | *(internal soft-delete; no RIS source)* |

> **Staleness dependency.** When the orders RIS scraper refreshes the `ris_*`
> columns, it (or a trigger) must also refresh these plain columns, or they go
> stale. Same caveat as the plain columns on `pc1.patients`.

### `cpt_codes` is eliminated; no procedure column existed
The legacy system carried a `cpt_codes` lookup table. **pc1 drops it**, and the
`studies` table is **not used** for scheduling. Orders link straight to the
catalog by description:

```
orders.procedure_description  =  proceduresestimate.procedure_desc   (exact text)
```

`pc1.orders` carried **no procedure column at all**, so Phase 3.5 adds both
`ris_procedure_description` (raw) and `procedure_description` (the plain join
key). `procedure_code` (the CPT array) is sourced **from the catalog** via the
view, never stored on the order.

> **Exact-match contract.** `procedure_description` must match
> `proceduresestimate.procedure_desc` character-for-character. Drift (a typo, a
> trailing space, a renamed procedure) means the join finds nothing and the
> order silently drops out of `orders_v` — the same naming-exactness discipline
> as `pc1.facilities` ↔ NovaRIS facility names.

### `requesting_date` — facility-local date, future engine use
`requesting_date` is a facility-**local** `date` (not a timestamp — time-of-day
isn't meaningful). It's not consumed by the current logic, but the Phase 3.6
engine will use it as an override: when an order carries a `requesting_date`
(e.g. "the requesting date is ~15 days out"), the scheduler pins its search
**start-floor to that local date** instead of starting from "now", so options
are produced only for that day forward rather than the whole rolling horizon.

### Open question — `order_type` 'P' / 'S'
NovaRIS appears to emit an order type of `P` or `S`. **The meaning is
unconfirmed**, so `order_type` (and `order_status`, whose vocabulary is also
TBD) carry **no CHECK constraint**. Add one once the vocabulary is confirmed.

---

## Identity

| Concept       | Field(s)            |
|---------------|---------------------|
| Surrogate PK  | `id`                |
| Tenant scope  | `client_id`         |
| RIS-side id   | `ris_order_id` (NOT NULL) |
| FK targets    | `facility_id` → `pc1.facilities` (nullable), `patient_id` → `pc1.patients` |

## Columns added by Phase 3.5

The full RIS column set is documented at the source; these are the columns
Phase 3.5 adds (all NULL-able except `is_active`):

| Column | Type | Notes |
|---|---|---|
| `ris_procedure_description` | `text` | Raw procedure text from the RIS |
| `procedure_description` | `text` | Plain join key → `proceduresestimate.procedure_desc` |
| `preferred_language` | `varchar(50)` | Internal-only |
| `order_status` | `varchar(50)` | Plain ← `ris_order_status`. Vocabulary TBD — no CHECK |
| `order_type` | `varchar(20)` | Plain ← `ris_order_type`. 'P'/'S' TBD — no CHECK |
| `requesting_date` | `date` | Plain ← `ris_requesting_date`. Facility-local; future start-floor override |
| `is_active` | `boolean NOT NULL DEFAULT true` | Soft-delete; the view filters on it |

## Cross-tenant safety trigger

Phase 3.5 also adds `pc1.check_orders_consistency()` (absent before), firing
`BEFORE INSERT OR UPDATE OF client_id, facility_id, patient_id`. It validates
that `client_id` matches both the FK'd **facility's** and the FK'd **patient's**
`client_id`, turning a cross-tenant mis-link into a loud INSERT failure. Matches
the trigger pattern on `pc1.proceduresestimate` / `pc1.scheduleexceptions`.
`facility_id` is nullable, so the facility check short-circuits when it's NULL.

## Verification queries (post-migration)

```sql
-- the new plain columns exist
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'pc1' AND table_name = 'orders'
  AND column_name IN ('ris_procedure_description','procedure_description',
                      'preferred_language','order_status','order_type',
                      'requesting_date','is_active')
ORDER BY column_name;

-- plain columns backfilled from their ris_* sources (expect 0 mismatched rows)
SELECT count(*) AS mismatched
FROM pc1.orders
WHERE order_status    IS DISTINCT FROM ris_order_status
   OR order_type      IS DISTINCT FROM ris_order_type
   OR requesting_date IS DISTINCT FROM ris_requesting_date;

-- trigger present
SELECT tgname FROM pg_trigger
WHERE tgrelid = 'pc1.orders'::regclass AND NOT tgisinternal;
```

## Related

- [`pc1.orders_v`](./orders_v.md) — the compatibility-layer view over this table
- [`pc1.proceduresestimate`](./proceduresestimate.md) — the catalog the view joins against
- `pc1.patients` — patient source (no doc yet); plain internal columns added in `migrations/0006_add_patients_internal_columns.sql`
- `docs/session-handoff.md` §8 — full Phase 3.5 design context
