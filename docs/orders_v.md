# `pc1.orders_v` (compatibility-layer view — Phase 3.5)

A **regular (non-materialized) view** that joins [`pc1.orders`](./orders.md),
`pc1.patients`, and [`pc1.proceduresestimate`](./proceduresestimate.md) into the
row shape the scheduling engine consumes. It is the **stable contract** between
the engine (Phase 3.6) and the underlying `pc1.orders` table. As the team
evolves the orders schema (Phase 4), the view definition absorbs the change so
the engine keeps reading the same columns either way.

> **Why a view, not inline joins.** Postgres rewrites a regular view at query
> time, so performance equals hand-written joins **as long as** we avoid
> `DISTINCT ON` / aggregation and keep the join columns indexed. Both hold here.
> Do **not** convert to `MATERIALIZED VIEW` or a function-based view without
> re-discussing — the no-overhead guarantee is intentional. See
> `docs/session-handoff.md` §8.1.

---

## Row shape: multi-row per order

The view emits **one row per matching `proceduresestimate` shape** for an order
— there is **no `DISTINCT ON`**. A single order can therefore produce several
rows: the catalog's global default, a facility-level override, a per-machine
override, etc. (the 4-shape override design on `proceduresestimate`).

`per_machine_resolver.py` consumes these candidate rows and resolves the 4-tier
precedence in Python:

```
tier 1: pe_facility_id == order's facility_id  AND  modality_id == candidate machine
tier 2: pe_facility_id == order's facility_id  AND  modality_id IS NULL
tier 3: pe_facility_id IS NULL                 AND  modality_id == candidate machine
tier 4: pe_facility_id IS NULL                 AND  modality_id IS NULL
```

This is why `pe_facility_id` (the **procedure's** facility) is exposed
separately from `facility_id` (the **order's** facility): the resolver compares
the two. Confirmed against `per_machine_resolver._row_tier()` in the legacy
engine bundle.

## Columns

| Column | Source | Engine use |
|---|---|---|
| `order_id` | `orders.id` | dedup / conflict reporting in the resolver |
| `client_id` | `orders.client_id` | tenant scope |
| `facility_id` | `orders.facility_id` | the **order's** facility; resolver per-facility tier |
| `patient_id` | `orders.patient_id` | top-level engine input filter |
| `patient_full_name` | `patients.patient_full_name` (plain) | display |
| `patient_dob` | `patients.patient_dob` (plain) | display |
| `patient_language` | `patients.language` (plain) | display / routing |
| `preferred_language` | `orders.preferred_language` | display / routing |
| `order_status` | `orders.order_status` | internal (vocabulary TBD) |
| `order_type` | `orders.order_type` | internal ('P'/'S' TBD) |
| `requesting_date` | `orders.requesting_date` | facility-local date; 3.6 engine pins the search start-floor to it when set |
| `procedure_description` | `orders.procedure_description` | display + the catalog join key |
| `procedure_code` | `proceduresestimate.procedure_code` (text[]) | display / safety-net (not consumed by current logic) |
| `pe_facility_id` | `proceduresestimate.facility_id` | the **procedure's** facility; resolver per-facility tier |
| `modality_id` | `proceduresestimate.modality_id` | the procedure's machine; resolver per-machine tier |
| `modality_type` | `proceduresestimate.modality_type` | engine modality grouping |
| `required_slots` | `proceduresestimate.required_slots` | scheduler block sizing |
| `required_time` | `proceduresestimate.required_time` | informational (not consumed by current logic) |

**Deferred (decision #5):** `stat_order`, `machine_skill`, `contrast_skill`.
The legacy engine references these keys but only **debug-prints** them — they're
inert in the scheduling logic. They are left off the view to avoid paying for
unused contract surface; add them if/when real logic consumes them. (The 3.6
port softens the legacy `order_row["stat_order"]` subscripts to `.get()` so the
missing keys don't `KeyError`.)

## Join scoping

`tenant + active` on all three sources:

```sql
orders o
  JOIN patients p            ON p.id = o.patient_id
                            AND p.client_id = o.client_id
                            AND p.ris_is_active = true
  JOIN proceduresestimate pe ON pe.procedure_desc = o.procedure_description
                            AND pe.client_id = o.client_id
                            AND pe.is_active = true
WHERE o.is_active = true
```

There is **no facility-tier filter in SQL** — the Python resolver owns
facility/machine precedence, so every matching catalog shape (global +
facility + per-machine) must reach it. `client_id` matching prevents
cross-tenant bleed when a `procedure_desc` exists for more than one tenant.

> `patients` uses `ris_is_active` as its active flag (there is no plain
> `is_active` on that table).

## 3.6 engine-port rename map

The legacy engine reads legacy key names; pc1 exposes FK names. When porting:

| Legacy `orders_v` key | pc1 view column |
|---|---|
| `facility` (order's) | `facility_id` |
| `module_type` | `modality_type` |
| `module` (pe's machine) | `modality_id` |
| `pe_facility` | `pe_facility_id` |

In pc1 the resolver's tier comparisons become **ID == ID** (cleaner than the
legacy text == text). The machine inventory built by
`derive_machines_from_machineschedule` must key on `modality_id` to match.

## Verification queries (post-migration)

```sql
-- view exists with the expected columns / order
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'pc1' AND table_name = 'orders_v'
ORDER BY ordinal_position;

-- confirms it is a plain (non-materialized) view
SELECT table_name FROM information_schema.views
WHERE table_schema = 'pc1' AND table_name = 'orders_v';

-- multi-row shape: an order on a procedure that has >1 catalog shape should
-- return >1 row (no DISTINCT ON collapse)
SELECT order_id, count(*) AS catalog_shapes
FROM pc1.orders_v
GROUP BY order_id
ORDER BY catalog_shapes DESC;

-- spot-check one patient's expanded orders
SELECT order_id, facility_id, pe_facility_id, modality_id, modality_type,
       procedure_description, required_slots
FROM pc1.orders_v
WHERE patient_id = <PATIENT_ID>
ORDER BY order_id, pe_facility_id NULLS LAST, modality_id NULLS LAST;
```

Expected per-patient row counts get filled in from the seed data once it's
inserted (see the seed migration).

## Related

- [`pc1.orders`](./orders.md) — the underlying orders table
- [`pc1.proceduresestimate`](./proceduresestimate.md) — catalog (4-shape override design)
- [`pc1.machineschedule`](./machineschedule.md) — slot calendar the engine schedules into
- `docs/session-handoff.md` §8 — full Phase 3.5 design context + decisions
