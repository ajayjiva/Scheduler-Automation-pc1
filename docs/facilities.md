# `pc1.facilities`

The per-tenant list of physical imaging sites. Every other PC1 table
that needs to talk about a facility (modalities, schedules, exceptions,
client parameters, …) joins to this table by `facility_id`, **not** by
free-text facility name.

This table is the single source of truth for:

1. **Which facilities exist** for a given client (`client_id`).
2. **Whether the client currently contracts** for a facility
   (`is_client`).
3. **The stable surrogate key** (`id`) other tables reference.

It is **never hard-deleted from** under normal operation. Facilities
that come and go are managed via flag columns (`is_client`, and
whatever other state flags are added over time).

---

## Columns the scrapers / app rely on

| Column          | Type    | Notes |
|-----------------|---------|-------|
| `id`            | bigint  | Surrogate PK. Stable forever. FK target everywhere else. |
| `client_id`     | bigint  | FK → `clients.id`. Tenant scope. |
| `facility_name` | text    | Human-readable label (e.g. `'Inview-Fremont'`). Must match the keys in `novaRIS_common.FACILITY_MAP` for the scrapers to resolve correctly. |
| `is_client`     | boolean | `true` when the tenant currently contracts for this facility. Drives scraper iteration scope — see [Effect on scrapers](#effect-on-scrapers). |

Other columns may exist (address, time zone, contact info, etc.) but
the items above are the ones the scrapers depend on.

---

## Unique constraint

Migration `0001_pc1_modalities_novaris_compat.sql` adds:

```sql
ALTER TABLE pc1.facilities
    ADD CONSTRAINT facilities_client_id_facility_name_key
    UNIQUE (client_id, facility_name);
```

This makes `(client_id, facility_name)` a well-defined lookup key, so
the scraper can call:

```python
SELECT id FROM pc1.facilities
 WHERE client_id = ? AND facility_name = ?
```

…and get exactly one row (or zero — in which case the scraper hard-
fails with a clear "seed the facility before running" message). Two
facilities with the same name under the same tenant is structurally
impossible.

Seed scripts can also `ON CONFLICT (client_id, facility_name) DO …`
against this constraint.

---

## `is_client` semantics

`is_client` is a tenant-level contract flag, **not** an operational
"is this facility reachable today" flag. Set it to `true` when the
tenant has a live contract that covers this facility's data, `false`
otherwise.

The scraper treats `is_client = false` as "out of scope — leave its
data alone." It does not delete or deactivate anything; it just stops
reading from and writing to the facility's rows.

To onboard a new facility:

1. Insert the row into `pc1.facilities` with `is_client = true`.
2. Add (or confirm) the matching `facility_name → NovaRIS facility ID`
   entry in `novaRIS_common.FACILITY_MAP`.
3. Run the scraper in `--initial-load` mode against that facility once
   to seed `pc1.modalities` — see the
   [scraper doc](./novaris_modalities_scraper.md).
4. Subsequent nightly delta runs pick it up automatically.

To offboard:

1. Set `is_client = false`.

The next nightly run will silently skip it; its existing
`pc1.modalities` rows are left untouched (frozen — see the warning
below). No deletes are issued.

---

## Effect on scrapers

When you run `novaRIS_modalities_scraper.py` **without** the
`--facility` flag, the scraper:

1. Reads `FACILITY_MAP.keys()` from `novaRIS_common.py`.
2. Reads `pc1.facilities.facility_name` for the active `client_id`
   filtered to `is_client = true`.
3. Intersects the two sets.
4. Iterates only that intersection — one NovaRIS round-trip per
   active facility, none for the rest.

So `is_client = false` rows are skipped silently — no log line, no
HTTP call, no DB read or write touching their modality data.

**Bypass:** `python novaRIS_modalities_scraper.py --facility=NAME`
ignores `is_client` and processes the named facility regardless of
flag state. Useful for one-off debug or testing.

### Caveat — "frozen state" while `is_client = false`

Because the scraper fully ignores out-of-contract facilities, none of
the normal lifecycle transitions run for them while they're flagged
off:

- Rows the RIS subsequently **adds** for that facility won't appear in
  `pc1.modalities`.
- Rows the RIS subsequently **removes** for that facility won't be
  soft-deactivated; they'll keep showing `is_active = true` in PC1.
- Rows the RIS subsequently **changes** won't be updated; their
  `content_hash` will stay stale.

This is the intended behavior — out-of-contract facility data
shouldn't churn under us. **But:** if you ever flip `is_client` back
to `true`, the *first* run after that may emit a flurry of `insert`,
`update`, and `deactivate` events as PC1 catches up to whatever drift
accumulated while it was frozen. Plan onboarding/offboarding cycles
with that in mind.

---

## Common queries

```sql
-- All active facilities for the current tenant
SELECT id, facility_name
  FROM pc1.facilities
 WHERE client_id = 1
   AND is_client = true
 ORDER BY facility_name;

-- Resolve a facility_id the same way the scraper does
SELECT id
  FROM pc1.facilities
 WHERE client_id = 1
   AND facility_name = 'Inview-Fremont';

-- Flip a facility offline (offboarding)
UPDATE pc1.facilities
   SET is_client = false
 WHERE client_id = 1
   AND facility_name = 'Inview-Concord';
```

---

## Related

- [`pc1.modalities` doc](./modalities.md) — the table whose lifecycle
  is gated by `is_client`.
- [NovaRIS modalities scraper](./novaris_modalities_scraper.md) — the
  consumer of this table's `is_client` flag.
