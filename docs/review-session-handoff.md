# Review-Session Handoff — pc1 Scheduler Automation

> **Purpose of the next session:** a guided **review** with the user — walk each
> object, its functionality, the logic built, and *opportunities to improve*.
> This is a discussion-driven session, not an implementation sprint. This doc
> exists so the reviewing session can (a) understand context fast, (b) locate
> every object's code + doc, and (c) open with a concrete improvement agenda.
>
> **This is a companion to [`session-handoff.md`](./session-handoff.md)** — that
> file is the full project context (roadmap §6, snapshot §7, design §8, and the
> Phase 3.6 validation record §8.9). Read it first; this doc is the review lens
> on top of it.

---

## 0. How to get oriented (read order for the next session)

1. **`docs/session-handoff.md`** — full context. Especially §7 (what exists) and §8.9 (engine-port result).
2. **This file** — the review map (object → code → doc) + improvement candidates.
3. **Per-object docs** in `docs/` — open each as its topic comes up in discussion.
4. Current state: everything is on `main` (squash commit `d641cb9`, PR #11). Phases 1–3, 3.5, 3.6 all merged and validated. Phase 4 (real orders schema) not started.

Sync + smoke-test before the session:
```powershell
git checkout main; git pull origin main
python main.py --client-id=1 --patient-id=5 --debug --max-options=5   # override → 60 min
python main.py --client-id=1 --patient-id=3 --debug --max-options=5   # multi-modality chain
```
Test patients: ids **1–8** (`ris_account_no LIKE 'TEST-P%'`). Seed prerequisites (already applied): migrations `0006`–`0011`, `generate_machineschedule.py` for Inview-Fremont + Antioch.

---

## 1. Complete object inventory (object → purpose → code → doc)

### Tables (`pc1` schema)
| Object | Purpose | Code / migration | Doc |
|---|---|---|---|
| `pc1.clients` | Tenant identity + global param defaults (`slot_size`, hours, `advance_booking_days`, `timezone`) | pre-pc1 foundation | — (described in `facilities.md`) |
| `pc1.facilities` | Facility list, `is_client` gate, per-facility param overrides | `0001` | `docs/facilities.md` |
| `pc1.modalities` | Per-facility machine inventory | `0001` | `docs/modalities.md` |
| `pc1.proceduresestimate` | Procedure catalog, 4-shape override design | `0002`, `0007` (index) | `docs/proceduresestimate.md` |
| `pc1.scheduleexceptions` | Exception rules, recurrence masks | `0003` | `docs/scheduleexceptions.md` |
| `pc1.machineschedule` | Slot calendar (UTC + facility-local `seq`) | `0004`, `0005` (rename) | `docs/machineschedule.md` |
| `pc1.orders` | Patient orders (RIS-shaped + plain internal cols) | `0008` (ALTER) | `docs/orders.md` |
| `pc1.patients` | Patients (RIS-shaped + plain cols) | `0006` (ALTER) | — (no dedicated doc yet) |

### Views (compatibility layer)
| Object | Purpose | Code | Doc |
|---|---|---|---|
| `pc1.orders_v` | Multi-row order↔catalog join the engine reads | `0009` | `docs/orders_v.md` |
| `pc1.machineschedule_v` | `machineschedule` JOIN `modalities` re-exposing `modality_type`/`modality_machine` | `0011` | `docs/machineschedule_v.md` |

### Data ingestion (NovaRIS scrapers)
| Object | Purpose | Code | Doc |
|---|---|---|---|
| Modalities scraper | Refresh machine inventory | `novaRIS_modalities_scraper.py` | `docs/novaris_modalities_scraper.md` |
| Procedure scraper | Refresh catalog (grid→wizard) | `novaRIS_standardprocedure_scraper.py` | `docs/novaris_standardprocedure_scraper.md` |
| Exception scraper | Refresh exception rules | `novaRIS_exception_scraper.py` | `docs/novaris_exception_scraper.md` |
| NovaRIS protocol helpers | login, form-state, session | `novaRIS_common.py` | — |

### Calendar writers
| Object | Purpose | Code | Doc |
|---|---|---|---|
| Slot generator | Blank 24h slot grid, business-hours availability | `generate_machineschedule.py` | `docs/machineschedule_generator.md` |
| Exception reconciler | Overlay exceptions → `availability`/`exceptions[]` | `reconcile_exceptions.py` | `docs/reconcile_exceptions.md` |

### Scheduling engine (Phase 3.6, read-only options generator)
| Object | Purpose | Code |
|---|---|---|
| Orchestrator (CLI) | orders → resolve → load slots → build/prune → print | `main.py` |
| Orders reader | `orders_v` + 4-tier `summary_list` | `get_orders.py` |
| Per-machine resolver | 4-tier precedence + combination enumeration (keyed on `modality_id`) | `per_machine_resolver.py` |
| First-modality blocks | Eligible anchor blocks (cumulative-slot ≥ required) | `first_modalitytype_scheduler.py` |
| Chain builder | Adjacent multi-modality chains, wait slots | `all_modality_scheduler.py` |
| Cumulative slots | Consecutive `availability=1` runs per machine-day | `cumulative_open_slots.py` |
| Option filters | Dedupe, Pareto-prune, time-of-day/day/month | `option_filters.py` |
| TZ helpers | UTC↔facility-local (`date_and_time_utc`) | `tz_helpers.py` |
| Facility settings | pc1 params reader (`facilities`←`clients`) | `facility_settings.py` |
| Resource scheduler | Technician-calendar path (inert; Inview off) | `resource_scheduler.py` |

### Shared infra
| Object | Purpose | Code |
|---|---|---|
| Supabase factory | `get_supabase()` | `supabase_client.py` |
| Client context | `resolve_client_id()`, CLI flag | `client_context.py` |

---

## 2. Logic summary (the "what we built")

**Data flow, end to end:**
NovaRIS → scrapers → (`modalities`, `proceduresestimate`, `scheduleexceptions`) →
`generate_machineschedule.py` builds blank slots → `reconcile_exceptions.py`
overlays exceptions onto `availability` → **engine** reads `orders_v` +
`machineschedule_v` and produces patient appointment options.

**Key logic + invariants to review:**
- **`ris_*` (raw) vs plain (internal) columns** on `orders`/`patients` — plain is a cleaned copy; scraper refresh must keep plain in sync.
- **Catalog join by exact text**: `orders.procedure_description = proceduresestimate.procedure_desc`. `cpt_codes` eliminated. Spelling drift silently drops an order from `orders_v`.
- **4-shape override precedence** (`proceduresestimate`): `(facility,machine)` → `(facility,NULL)` → `(NULL,machine)` → `(NULL,NULL)`. Resolved in Python (`get_orders._row_tier`, `per_machine_resolver`).
- **`seq` engine contract**: `machineschedule.seq` is facility-local `YYYYMMDDHHMMSS`; engine extracts the local date via `str(seq)[:8]`.
- **UTC vs local**: `date_and_time_utc` = UTC instant (query filtering); `start_time`/`end_time` = facility-local clock (display). All wall-clock reasoning goes through `tz_helpers`.
- **Availability rule**: `availability=0` when out-of-hours (generator) OR in-hours + Hard exception OR in-hours + `order_id IS NOT NULL` (reconciler). Phase 4 booking writes `(order_id, availability=0)` atomically.
- **Engine pipeline**: resolve per-machine slot totals → find eligible blocks (cumulative open slots ≥ required, business-hours-safe) → build adjacent chains (with `wait_threshold`) → dedupe + Pareto-prune → sort (soonest/shortest) → cap.
- **Two paths in `main.py`**: per-machine enumeration (when `has_per_machine_variation`) vs legacy single-total. Currently only the legacy path runs (see gaps).

---

## 3. Improvement opportunities (pre-seeded agenda)

Grounded candidates I've already noticed — starting points for the discussion, not conclusions.

### Engine / logic
1. **Option explosion.** A single-modality 30-day search yields ~1,100+ options (capped at 50). Worth discussing product intent: representative-per-day, coarser time buckets, shorter default horizon, or richer default filters.
2. **Per-machine combination path is never exercised.** Each modality has only one active machine per facility, so `has_per_machine_variation` is always False and the enumeration path (`_build_options_per_machine`, `MAX_COMBINATIONS`) is dead in practice. Decide: is multi-machine-per-modality real? If yes, it needs test data + validation; if no, consider simplifying.
3. **Deferred order attributes never used.** `stat_order` / `machine_skill` / `contrast_skill` are plumbed through the schedulers but never affect logic (softened to `.get()`, absent from `orders_v`). Decide whether they'll ever drive behavior; if not, remove the plumbing.
4. **`requesting_date` start-floor override** is designed (`orders.requesting_date`) but **not implemented** in `main.py`. Opportunity to wire it into the start-floor if the product wants "only show options on/after the requested date."
5. **Technician-calendar path is untested.** `resource_scheduler.py` + `technicianschedule_v` only run when `use_technician_calendar=true`; Inview is false and `technicianschedule_v` doesn't exist in pc1. Future-tenant concern.
6. **Performance / recompute.** The engine pulls tens of thousands of `machineschedule_v` rows into Python and recomputes cumulative open slots + blocks every run. Deliberate (stale-column footgun avoided), but at scale worth revisiting (push-down to SQL, caching, or a narrower default window).

### Schema / data
7. **Catalog hygiene.** `proceduresestimate` has junk rows (`DELETE`×48, `ZZZ`, `DO NOT USE`) and **duplicate global rows** (same `procedure_desc`, two `(NULL,NULL)` shapes → multi-row `orders_v` output, as P04 showed). Opportunity: clean/dedupe the catalog.
8. **Fragile exact-text join.** `procedure_description` ↔ `procedure_desc` drift silently excludes orders. Opportunity: a validation/monitoring query, or a normalized join key.
9. **`order_type` `P`/`S` vocabulary unconfirmed** — no CHECK constraint on `order_status`/`order_type`. Confirm meaning, then constrain.
10. **No booking/write path.** Engine is read-only; the Phase 4 atomic-booking workflow (`order_id` + `availability=0`, plus the deferred `machineschedule.order_id → orders` FK) isn't built.

### Config / cleanup
11. **`client_context.DEFAULT_CLIENT_ID = 1001`** but pc1 data is `client_id = 1`; also the legacy `get_client_parameters`/`get_param` (clientparameters table) remain in the file and are dead/misleading for pc1. Opportunity: fix the default and delete the dead functions.

### Docs / tests
12. **Missing `docs/data-model.md`** — referenced by `proceduresestimate.md` (and the resolver's design note) but doesn't exist. Either create it or fix the references.
13. **No `docs/patients.md`** — the only pc1 table without a dedicated doc.
14. **No automated tests.** The pure functions are ideal unit-test targets (`per_machine_resolver` 4-tier, `option_filters` dedupe/Pareto, `cumulative_open_slots`, `tz_helpers` DST). Legacy referenced tests (e.g. `test_equal_windows_both_survive`) that weren't ported. Opportunity: add a `tests/` suite to harden before Phase 4.

---

## 4. Open questions for the user (to resolve during review)
- **Option volume**: what should a "good" patient-facing result set look like (count, grouping, default horizon)?
- **Multi-machine facilities**: real, or is one-machine-per-modality the norm? (decides the per-machine path's fate)
- **Deferred attributes**: will `stat_order`/`machine_skill`/`contrast_skill`/`requesting_date` ever drive scheduling?
- **Phase 4 orders schema**: timeline + expected shape (drives the `orders`/`orders_v` migration).
- **`pc1.user_profiles` lifecycle**: how does a row get created? (every audit FK points at it.)
- **Booking workflow**: is Phase 4 booking in scope next, and who owns the atomic-write design?

---

## 5. Quick reference — verification snippets
```sql
-- orders_v row counts per seed patient (expect 1,1,2,2,3,1,1,2 = 13)
SELECT p.patient_full_name, count(*)
FROM pc1.orders_v v JOIN pc1.patients p ON p.id = v.patient_id
WHERE p.ris_account_no LIKE 'TEST-P%'
GROUP BY p.patient_full_name ORDER BY p.patient_full_name;

-- P05 override tiers (expect required_slots 2 / 3 / 4)
SELECT facility_id, pe_facility_id, modality_id, required_slots
FROM pc1.orders_v
WHERE order_id = (SELECT id FROM pc1.orders WHERE ris_order_id = 900006)
ORDER BY pe_facility_id NULLS FIRST, modality_id NULLS FIRST;
```
Seed cleanup (when desired) — recipe in `migrations/0010_seed_test_orders.sql` header.
