# `pc1.machineschedule_v`

Phase 3.6 engine compatibility-layer view over
[`pc1.machineschedule`](./machineschedule.md). Created by
`migrations/0011_create_pc1_machineschedule_v.sql`.

## Why it exists

`pc1.machineschedule` dropped the legacy text columns `modality_type`,
`modality_machine`, and `facility` in favor of the `modality_id` / `facility_id`
FKs (migration `0004`), and renamed `date_and_time` → `date_and_time_utc`
(`0005`). The scheduling engine (`main.py` + helpers) reads `modality_type` and
`modality_machine` on every slot — for modality grouping and the patient-facing
"On Machine" line. This view rejoins that machine metadata from
`pc1.modalities`, so the engine reads legacy-shaped rows without a per-row
Python lookup. Same compatibility-layer pattern as
[`pc1.orders_v`](./orders_v.md).

## Shape

Straight `INNER JOIN pc1.modalities ON id = machineschedule.modality_id` — the
FK guarantees exactly one match, so the view neither adds nor drops rows.

| Column | Source | Notes |
|---|---|---|
| `id`, `client_id`, `facility_id` | machineschedule | |
| `modality_id` | machineschedule | **per-machine key** — matches `orders_v.modality_id` (the override pin). The engine's per-machine resolver keys candidate machines on this int FK. |
| `modality_type` | modalities | was a column on legacy machineschedule |
| `modality_machine` | modalities | display name only (never a key) |
| `seq` | machineschedule | facility-local `YYYYMMDDHHMMSS`; engine takes `str(seq)[:8]` for the local date |
| `slot_seq` | machineschedule | 1-based ordinal within the facility-local day |
| `date_and_time_utc` | machineschedule | true UTC; engine converts via facility tz |
| `start_time` / `end_time` | machineschedule | facility-local `TIME` (business-hours filter) |
| `availability`, `capacity`, `scheduled`, `order_id` | machineschedule | |

## Scoping & usage

- **No `is_active` filter** on `pc1.modalities`: the generator only emits slots
  for active machines, and `availability` is the real bookability signal.
- Tenant scoping is the caller's job — the engine filters `client_id` +
  `facility_id`, exactly as it would against the base table.
- **Read-only for the engine.** Booking writes (`order_id`, `availability`)
  still target the base `pc1.machineschedule` table in Phase 4.

## Related

- [`pc1.machineschedule`](./machineschedule.md) — the base slot calendar
- [`pc1.orders_v`](./orders_v.md) — the sibling compatibility view for orders
- `main.py` + `per_machine_resolver.py` + `first_modalitytype_scheduler.py` — the engine consumers
