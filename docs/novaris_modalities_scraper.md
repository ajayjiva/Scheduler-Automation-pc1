# `novaRIS_modalities_scraper.py`

Scrapes the per-facility modality-machine grid from NovaRIS
(`ViewModalities.aspx`) and syncs each row into Supabase
`pc1.modalities`.

This is the **nightly maintenance job** that keeps PC1's idea of "what
imaging machines exist at each facility" aligned with what NovaRIS
shows.

For the data model the scraper writes to, see
[`pc1.modalities`](./modalities.md) and
[`pc1.facilities`](./facilities.md).

---

## Quick reference

```powershell
# Normal nightly run — delta sync, every is_client=true facility
python novaRIS_modalities_scraper.py

# Same, but don't write anything (preview)
python novaRIS_modalities_scraper.py --dry-run

# One facility only (bypasses the is_client filter)
python novaRIS_modalities_scraper.py --facility="Inview-Fremont"

# First-time seed for a newly-onboarded facility
python novaRIS_modalities_scraper.py --initial-load --facility="Inview-Concord"

# Cross-tenant one-off (overrides .env CLIENT_ID for this run)
python novaRIS_modalities_scraper.py --client-id=1002

# Debug: save the first parsed grid HTML to disk
python novaRIS_modalities_scraper.py --facility="Inview-Fremont" --save-grid=fremont.html
```

---

## Prerequisites

### `.env`

The scraper requires these variables (loaded via `python-dotenv`):

| Variable             | Used for                                  |
|----------------------|-------------------------------------------|
| `NOVARISURL`         | Base URL for NovaRIS (no trailing slash). |
| `NOVARISUSER`        | NovaRIS login user.                       |
| `NOVARISPASSWORD`    | NovaRIS login password.                   |
| `SUPABASE_URL`       | Supabase project URL.                     |
| `SUPABASE_KEY`       | Supabase service role / write key.        |
| `CLIENT_ID`          | Optional. Default tenant (falls back to `1001` if unset). |

### Database

Migration `0001_pc1_modalities_novaris_compat.sql` must be applied.
It:

- Relaxes `pc1.modalities.ris_modality_id` to allow `NULL` (NovaRIS
  doesn't expose an internal modality id).
- Drops the legacy `(client_id, ris_modality_id)` uniqueness so the
  scraper can use `(facility_id, source_record_key)` instead.
- Adds `UNIQUE (client_id, facility_name)` on `pc1.facilities` so the
  facility-id lookup is well-defined.

### Facility seeding

For every facility the scraper will touch, `pc1.facilities` must
contain a row with:

- `client_id` = the active tenant
- `facility_name` = exactly the key used in
  `novaRIS_common.FACILITY_MAP` (e.g. `'Inview-Fremont'`)
- `is_client = true` (unless you're invoking with `--facility=NAME`,
  which bypasses the flag)

If the row is missing, the scraper hard-fails with a clear
"seed the facility before running" message — it does NOT auto-create
facilities.

---

## Modes

### Default — delta sync

This is what the nightly cron runs. For each in-scope facility:

1. GET `ViewModalities.aspx`, select the facility in the dropdown,
   POST the Search form.
2. Parse the grid into in-memory records.
3. Fetch `(id, source_record_key, content_hash, is_active, created_at)`
   for every existing row in `pc1.modalities` for this
   `(client_id, facility_id)`.
4. For each scraped row, decide:
   - **No existing row** → INSERT (counter: `insert`).
   - **Existing row, hash matches, `is_active = true`** → no-op
     (counter: `unchanged`).
   - **Existing row, hash differs, `is_active = true`** → UPDATE
     (counter: `update`).
   - **Existing row, `is_active = false`** → UPDATE + reactivate,
     preserve `created_at` (counter: `reactivated`; also rolled into
     `update`).
5. Compute the set of `source_record_key` values present in DB but
   missing from the scrape → bulk-UPDATE them to `is_active = false`
   (counter: `deactivate`).
6. Per-facility log line:

```
[delta] <facility>: <N> scraped records  insert=<i>  update=<u> (reactivated=<r>)  unchanged=<x>  deactivate=<d> -> applied.
```

(or `(dry run, not applied).` under `--dry-run`).

Writes are batched in chunks of 200–500 rows and committed via a
Supabase upsert with `on_conflict="facility_id,source_record_key"`.

### `--initial-load` — wipe + seed

For each in-scope facility:

1. `DELETE FROM pc1.modalities WHERE client_id=? AND facility_id=?`
2. Bulk INSERT every scraped row fresh.

Use this **once** when onboarding a facility for the first time, or
after intentional manual cleanup. It burns surrogate `id`s, so don't
use it on a facility that's already in production. The nightly job
should never run with this flag.

Combine with `--facility=NAME` to scope the wipe to a single facility
(strongly recommended — `--initial-load` alone wipes every active
facility's data and re-seeds them all).

---

## Flags

| Flag                  | Effect |
|-----------------------|--------|
| `--initial-load`      | Switch from delta mode to wipe + bulk insert per facility. |
| `--dry-run`           | Parse + compute everything; skip every Supabase write. The per-facility log line says `(dry run, not applied)`. |
| `--facility=NAME`     | Process only this one facility (must match a key in `FACILITY_MAP`). **Bypasses the `is_client` filter** — useful for debug runs against a non-client facility. |
| `--client-id=NNNN`    | Override the active tenant for this run. Beats the `CLIENT_ID` env var and the default. |
| `--quiet`             | Suppress per-facility progress chatter (the `selecting facility … posting Search` lines). The summary line per facility still prints. |
| `--save-grid=FILE`    | Save the HTML of the first parsed grid to `FILE`. Use this when parsing produces zero rows so the HTML can be inspected and the parser adjusted. |

---

## Facility scope (the `is_client` filter)

When `--facility` is **not** passed, the scraper iterates only the
facilities present in `FACILITY_MAP` **and** flagged
`is_client = true` in `pc1.facilities` for the active tenant.
Facilities in `FACILITY_MAP` whose row is `is_client = false` are
**skipped silently** — no log line, no NovaRIS HTTP call, no DB read
or write.

This:

- Saves ~3–4 seconds per skipped facility per run (one NovaRIS GET +
  one Search POST avoided).
- Prevents accidentally writing modality rows for facilities the
  tenant doesn't contract for.

The trade-off is documented in
[`pc1.facilities` § "frozen state" caveat](./facilities.md#caveat--frozen-state-while-is_client--false):
while a facility is flagged off, its existing rows in `pc1.modalities`
sit frozen. Reads and writes resume only when `is_client` flips back
to `true`, and the first run after that may produce a burst of
catch-up writes.

If you need to run against an `is_client = false` facility for any
reason (debugging, one-off audit), pass `--facility=NAME` — it
bypasses the filter entirely.

---

## Log format reference

Per facility (delta mode):

```
[delta] Inview-Fremont: 27 scraped records  insert=0  update=1 (reactivated=1)  unchanged=26  deactivate=0 -> applied.
```

| Field            | Meaning |
|------------------|---------|
| `scraped records`| Rows the parser produced from the NovaRIS grid. |
| `insert`         | Rows the scraper saw for the first time. |
| `update`         | Rows whose `content_hash` changed OR that were reactivated. **Includes** the `reactivated` count. |
| `reactivated`    | Rows that were `is_active=false` in DB and are now flipped back to `true`. **Subset of `update`.** |
| `unchanged`      | Rows whose `content_hash` matched DB exactly. Zero writes. |
| `deactivate`     | Rows present in DB but **missing** from this scrape — flipped to `is_active=false`. |

The final line summarises across all facilities:

```
Done. Total records processed: 42  Elapsed: 0m 3s  Rate: 11.0 records/sec
```

`Total records processed` is the sum of `scraped records` across
facilities, not the number of writes.

---

## Run recipes

### Daily nightly job

```powershell
python novaRIS_modalities_scraper.py
```

Schedule via Windows Task Scheduler (or `cron` on a Linux host).
Recommended: 2–3am local time, after the RIS quiet window. Redirect
both stdout and stderr to a rotating log file so you can grep when
something looks wrong:

```powershell
python novaRIS_modalities_scraper.py *> "C:\Logs\novaris_modalities_$(Get-Date -f yyyyMMdd).log"
```

### Onboarding a new facility

1. INSERT row into `pc1.facilities`
   (`client_id`, `facility_name`, `is_client = true`).
2. Confirm `facility_name` matches a key in
   `novaRIS_common.FACILITY_MAP`. Add the mapping there if it's new.
3. Seed once:

   ```powershell
   python novaRIS_modalities_scraper.py --initial-load --facility="Inview-Concord"
   ```

4. Drop back to the nightly delta cron from then on.

### Offboarding a facility

```sql
UPDATE pc1.facilities
   SET is_client = false
 WHERE client_id = 1 AND facility_name = 'Some-Facility';
```

Next nightly run skips it silently. Re-onboarding is just flipping
`is_client` back; no `--initial-load` needed unless data drift while
the facility was offline is unacceptable.

### Forcing a re-sync of one facility

```powershell
python novaRIS_modalities_scraper.py --facility="Inview-Fremont"
```

Bypasses the `is_client` filter, processes only Fremont, normal delta
semantics.

### Previewing what would change

```powershell
python novaRIS_modalities_scraper.py --dry-run
```

Same scrape + diff computation as a real run; the counter line prints
`(dry run, not applied)` instead of `-> applied`.

### Cross-tenant invocation

```powershell
python novaRIS_modalities_scraper.py --client-id=1002
```

The `--client-id` flag wins over `.env`'s `CLIENT_ID`. Useful for
running ad-hoc against a tenant other than the one configured in the
environment.

---

## Troubleshooting

### "No pc1.facilities row found for client_id=…, facility_name=…"

The facility isn't seeded for this tenant. Either INSERT the row in
`pc1.facilities` (see [Onboarding](#onboarding-a-new-facility)) or
drop the `FACILITY_MAP` key that doesn't apply to this tenant.

### "No is_client=true facilities found in pc1.facilities for client_id=…"

You invoked without `--facility` and every entry in `FACILITY_MAP`
either has no matching `pc1.facilities` row or has `is_client = false`
for this tenant. Either flip a facility on, or pass `--facility=NAME`
explicitly.

### Parser produces zero rows but the NovaRIS UI shows data

The grid HTML or the form's hidden field names probably changed. Run:

```powershell
python novaRIS_modalities_scraper.py --facility="<some_facility>" --save-grid=debug.html
```

…and inspect `debug.html`. The parser lives in
`novaRIS_modalities_scraper.py` and the form-field discovery in
`novaRIS_common.py`.

### Login fails

`NOVARISUSER` / `NOVARISPASSWORD` in `.env` are wrong, the account is
locked, or NovaRIS is down. The scraper prints `Login failed.` and
exits non-zero.

### Supabase writes fail mid-run

The scraper does not currently checkpoint. If a delta run dies after
some facilities are written and others aren't, the safe recovery is
just to re-run it — delta mode is idempotent and the unchanged rows
short-circuit. (`--initial-load` is **not** idempotent; don't blindly
re-run that one on partial failures.)

---

## Internals (one-paragraph summary)

`scrape()` resolves the tenant, intersects `FACILITY_MAP` with
`pc1.facilities` (skipping the intersect when `--facility` is given),
logs in to NovaRIS, then for each facility GETs the modalities page,
posts the facility-scoped Search, parses the resulting grid into dict
records, resolves `facility_id` from `pc1.facilities` (cached
per-process), and hands the records to `write_delta()` or
`write_initial_load()`. The delta path computes content hashes,
diffs against `_fetch_all_existing()`, and issues batched upserts +
soft-deletes via the Supabase REST client. See the module docstring
in `novaRIS_modalities_scraper.py` for the exact field-by-field
schema mapping.

---

## Related

- [`pc1.facilities`](./facilities.md) — gates iteration scope via
  `is_client`.
- [`pc1.modalities`](./modalities.md) — the table this scraper
  maintains.
