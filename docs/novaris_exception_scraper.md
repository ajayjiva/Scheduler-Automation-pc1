# `novaRIS_exception_scraper.py`

Scrapes scheduling-exception rules from NovaRIS
(`ModalityScheduling.aspx` + `ModalitySchedulingPopup.aspx`) and syncs
each rule into Supabase `pc1.scheduleexceptions`.

This is an **on-demand sync job** that keeps PC1's exception table
aligned with NovaRIS. It is run manually — there is no schedule. See
[When to run this](#when-to-run-this) below.

For the data model the scraper writes to, see
[`pc1.scheduleexceptions`](./scheduleexceptions.md).

---

## Quick reference

```powershell
# Normal on-demand run — delta sync across every is_client=true facility
python novaRIS_exception_scraper.py

# Same, but don't write anything (preview)
python novaRIS_exception_scraper.py --dry-run

# One facility only
python novaRIS_exception_scraper.py --facility=Inview-Fremont

# One machine at one facility (for testing the parser)
python novaRIS_exception_scraper.py --facility=Inview-Fremont --modality=FRE-MRI

# First-time seed for a newly-onboarded tenant
python novaRIS_exception_scraper.py --initial-load

# Cross-tenant one-off (overrides .env CLIENT_ID for this run)
python novaRIS_exception_scraper.py --client-id=1002

# Debug: cap to 5 rules per (facility, modality) and save the first grid + popup
python novaRIS_exception_scraper.py --facility=Inview-Fremont --modality=FRE-MRI \
    --limit=5 --save-grid=debug/grid_fre_mri.html \
    --save-popup=debug/popup_fre_mri.html
```

---

## Prerequisites

### `.env`

| Variable          | Used for                                  |
|-------------------|-------------------------------------------|
| `NOVARISURL`      | Base URL for NovaRIS (no trailing slash). |
| `NOVARISUSER`     | NovaRIS login user.                       |
| `NOVARISPASSWORD` | NovaRIS login password.                   |
| `SUPABASE_URL`    | Supabase project URL.                     |
| `SUPABASE_KEY`    | Supabase service role / write key.        |
| `CLIENT_ID`       | Optional. Default tenant (falls back to `1001` if unset). |

### Database

Migration `0003_create_pc1_scheduleexceptions.sql` must be applied.

The scraper also reads from:

- `pc1.facilities` — to drive the per-facility iteration
  (`is_client = true` only) and to resolve `facility_id`.
- `pc1.modalities` — to resolve each NovaRIS machine name to a
  `modality_id`. NovaRIS machines that don't have a matching
  `pc1.modalities.modality_machine` row are **skipped** silently
  except for a debug-line summary at the end of the run.

### Tenant facility + modality seeding

This scraper does **not** auto-discover facilities or machines — it
only writes rules for facilities and machines that already exist in
PC1. The expected onboarding sequence is:

1. Seed `pc1.facilities` for the tenant with `is_client = true`.
2. Run `novaRIS_modalities_scraper.py` to populate `pc1.modalities`.
3. Run **this scraper** to populate `pc1.scheduleexceptions`.

A facility/machine that exists in NovaRIS but is missing from PC1
is reported at the end of the run; the scraper exits non-zero so the
gap is visible from an exit code alone.

---

## Modes

### Default — delta sync

For each `is_client = true` facility in `pc1.facilities` for the
active tenant (or only the one named by `--facility`):

1. Match `pc1.facilities.facility_name` against the live NovaRIS
   `facilitiesDD` dropdown (loud ERROR + non-zero exit if a facility
   was renamed).
2. **Fresh GET** of `ModalityScheduling.aspx` (resets the page's
   `__VIEWSTATE` blob, which NovaRIS otherwise grows on every
   postback).
3. **Pass 1** — POST the form with `facilitiesDD` set to this
   facility and `ModalityDD` left blank. The server renders every
   machine's rules for the facility in one response. Parse the
   grid: each `<tr>` carries `setActionSource('<modID>',
   '<frequencyId>', '<rowIdx>')` plus 10 visible cells.
4. For each parsed row, look up `modality_id` from
   `pc1.modalities.modality_machine` for this facility. Rows whose
   machine name isn't in `pc1.modalities` are dropped with a
   per-facility debug line and rolled into the final NOTE block.
5. **Pass 2** — for each row whose recurrence is `Weekly`,
   `Monthly`, or `Daily`, GET
   `ModalitySchedulingPopup.aspx?frequencyId=<id>&isOccurrence=false`
   in a `ThreadPoolExecutor` (default 6 workers) to read the
   day-of-week / day-of-month mask + `repeat_every` interval +
   any "End On" date override. `recurrence=None` rules skip pass-2.
6. Dedupe by `source_record_key` and run `write_delta` scoped to
   `(client_id, facility_id)`:
   - Fetch every scraper-managed row for this facility
     (`source_record_key IS NOT NULL`).
   - For each scraped row:
     - **No existing row** → INSERT (counter: `insert`)
     - **Existing row, hash matches, `is_active = true`** → no-op
       (counter: `unchanged`)
     - **Existing row, hash differs, `is_active = true`** → UPDATE
       (counter: `update`)
     - **Existing row, `is_active = false`** → UPDATE + reactivate,
       preserve `created_at` (counter: `reactivated`; also rolled
       into `update`)
   - Compute the set of `source_record_key` values present in DB but
     missing from this facility's scrape → bulk-UPDATE them to
     `is_active = false` (counter: `deactivate`).

INSERTs are batched in groups of 200. UPDATEs are issued **one row
at a time** — PostgREST upsert can't target the partial unique index
that backs `(client_id, facility_id, source_record_key)`.

Soft-deletes are batched in groups of 500.

### `--initial-load` — wipe + seed

For each scraped facility:

1. `DELETE FROM pc1.scheduleexceptions WHERE client_id = ? AND
   facility_id = ? AND source_record_key IS NOT NULL`
2. Bulk INSERT every scraped row fresh.

**What survives the wipe:**

- Manually-inserted rows with `source_record_key IS NULL`.

**What gets burned:**

- Every previous scraper-written row's surrogate `id`. Use this once
  when seeding a fresh tenant, or after a deliberate manual cleanup.

---

## Flags

| Flag                    | Effect |
|-------------------------|--------|
| `--initial-load`        | Switch from delta to wipe + bulk insert. Per-facility scope; manually-inserted rows preserved. |
| `--dry-run`             | Parse + compute everything, including the existing-row fetch (so the breakdown is realistic). Skip every Supabase write. |
| `--facility=NAME`       | Limit to one facility (e.g. `Inview-Fremont`). Must match `pc1.facilities.facility_name` AND the NovaRIS dropdown text exactly. |
| `--modality=NAME`       | Filter parsed rows to machines whose name contains NAME (case-insensitive). The per-facility POST is unchanged — this is a post-parse filter applied in-Python, not a server-side filter. |
| `--limit=N`             | Cap rules per facility (applied after the optional `--modality` filter). Combine with `--dry-run` for fast end-to-end testing. |
| `--workers=N`           | Parallel popup fetches in pass 2. Default 6. NovaRIS is single-threaded behind the popup endpoint — pushing past ~8 workers triggers timeouts without speeding up the run. |
| `--client-id=NNNN`      | Override the active tenant for this run. Beats the `CLIENT_ID` env var. |
| `--quiet`               | Suppress per-modality progress chatter. Errors, warnings, the per-facility summary, and the final `Done.` line are still shown. |
| `--save-grid=FILE`      | Save the HTML of the first grid response to `FILE`. Recommended path: `debug/<filename>.html` (gitignored). |
| `--save-popup=FILE`     | Save the HTML of the first popup response to `FILE`. |

---

## Two-pass scrape explained

NovaRIS exposes a rule's data across two pages, and the scraper has
to visit both for recurring rules:

### Pass 1 — Grid (`ModalityScheduling.aspx`)

A grid filtered by Facility (via `facilitiesDD`). The Modality
dropdown is left blank so the server returns every machine's rules
for the facility in a single response.

**Transport**: plain ASP.NET full-page postback. The page does NOT
use MS UpdatePanel AJAX (an older NovaRIS version did; the page has
since been migrated). There is no Search button and no
`AutoPostBack` on the dropdowns — POSTing the form with a new
`facilitiesDD` value triggers a server-side re-render.

Each `<tr>` carries:

| Cell            | Captured as |
|-----------------|-------------|
| `onclick`       | `setActionSource('<novaris_modality_id>', '<frequencyId>', '<rowIdx>')` → `source_record_key`, NovaRIS modality dropdown id |
| Name            | `name` (machine; used for `modality_id` lookup) |
| Modality Type   | `modality_type` (snapshot) |
| Facility        | `facility` (display label; cross-checked) |
| Description     | `description` |
| Start Date / Time | `start_date` / `start_time` (parsed) |
| End Date / Time   | `end_date` / `end_time` (parsed) |
| Recurrence      | `recurrence` (`None`/`Daily`/`Weekly`/`Monthly`) |
| Type            | `type` (`Hard`/`Soft`) |

One HTTP POST per facility (plus one preceding fresh GET for
`__VIEWSTATE` reset).

### Pass 2 — Popup (`ModalitySchedulingPopup.aspx`)

A per-rule detail dialog, accessed via query string:
`?frequencyId=<id>&isOccurrence=false`.

The popup supplies recurrence detail the grid doesn't:

| Popup control                                       | Captured as |
|-----------------------------------------------------|-------------|
| `repeatDD` selected option value                    | Cross-check vs. grid's `recurrence` |
| `repeatEveryWeek` / `repeatEveryMonth` input value  | `repeat_every` |
| `weeklyCheckboxlist_0..6` checked state             | `is_sunday`..`is_saturday` (Sun=0) |
| Telerik RadComboBox `"itemData"` JS init block      | `day_1`..`day_31` (Monthly only) |
| Label-matched "Weekday" checkbox                    | `weekdays_only` (Daily only) |
| `repeatUntilRadiobutton` + `repeatUntilDate`        | `end_date_override` (wins over grid's `end_date`) |

One HTTP GET per `Weekly`/`Monthly`/`Daily` rule. `None`-recurrence
rules skip pass-2 — those don't have a repeat pattern to read.

### Why two passes (and not one)

The grid has the rule's identity, window, recurrence type, and type
(Hard/Soft) — enough for a `None`-recurrence rule. But the day-mask
(which days of the week, which days of the month) only lives in the
popup. Reading both is the only way to faithfully reproduce a
recurring rule.

### When pass-2 fetches fail

If a popup fetch fails all three retries (0.5s / 1s / 2s backoff),
the scraper:

- Logs `WARN popup <freq_id>: <exception>` to stderr.
- Writes the rule to PC1 anyway, using **only the grid-side data**.
  The recurrence-mask columns will all be their defaults (false /
  `repeat_every=1`).
- Exits with status `3` at the end if any popup fetch failed, so an
  operator notices even from the exit code alone.

Re-running the scraper a few minutes later usually clears transient
failures (the delta path's content-hash skip means only the
previously-failed rules pay the popup cost on the retry).

---

## Log format reference

### Per-facility pass-1 + pass-2

```
  [pass1] Inview-Fremont (NovaRIS facility id=2) ... 8072 rows
    [debug] 2 unknown machine name(s) (not in pc1.modalities): 'UNKNOWN'×3, 'ZTest'×5
  [pass2] Inview-Fremont: fetching 4218 popups (6 workers) ...
    progress: 100/4218 (2%)  rate: 8.5/s  ETA: 8m 04s  failed=0
    progress: 200/4218 (4%)  rate: 8.6/s  ETA: 7m 47s  failed=0
    ...
```

### Pass-2 popup warnings (only when failures occur)

```
    WARN popup 12345: HTTPSConnectionPool(host='...', port=443): Read timed out.
```

### Delta summary line (per facility)

```
  [delta] Inview-Fremont: 207 scraped records  insert=3  update=12 (reactivated=1)  unchanged=190  deactivate=2 → applied.
```

| Field                | Meaning |
|----------------------|---------|
| `scraped records`    | Unique rows after per-facility dedupe. |
| `insert`             | New rows for this facility. |
| `update`             | Rows whose `content_hash` changed OR that were reactivated. **Includes** `reactivated`. |
| `reactivated`        | Rows previously `is_active=false` flipped back to `true`. **Subset of `update`.** |
| `unchanged`          | Rows whose `content_hash` matched DB exactly. Zero writes. |
| `deactivate`         | Rows present in DB but missing from this facility's scrape — flipped to `is_active=false`. |

### Initial-load summary line (per facility)

```
  [initial-load] Inview-Fremont: 207 records → wiped + inserted.
```

### Final summary + unknown-machine report

```
Done. Total records processed: 32,847  Elapsed: 8m 22s  Rate: 65.4/s

NOTE: 3 NovaRIS machine name(s) were not found in pc1.modalities — 12 row(s) skipped (no exception rules written for those machines):
  - [Inview-Fremont] 'UNKNOWN'  (3 row(s))
  - [Inview-Oakland] 'ZTest'    (5 row(s))
  - [Inview-Oakland] 'TmpMR'    (4 row(s))
```

The `NOTE` block is informational — these are NovaRIS-side machines
that PC1 doesn't track yet. If the names ought to be tracked, add
them to `pc1.modalities` (typically via the modalities scraper) and
re-run. Per-machine row counts make it easy to see whether an
unknown name represents a meaningful number of rules or just noise.

---

## Measured performance

Real numbers from the end-to-end verification run on the Inview
tenant (`client_id = 1`), recorded at scraper-feature ship time so
future operators have a baseline to compare drift against.

### Per-facility throughput (verification dataset)

| Facility                  | Total rows | One-off rows | Recurring rows (pass-2 popups) | Unknown machines | Initial-load wall-clock |
|---------------------------|-----------:|-------------:|-------------------------------:|-----------------:|-----------------------:|
| Antioch Medical Imaging   |      7,990 |        7,873 |                            117 |                0 | ~1m 50s                |
| Inview-Fremont            |      8,073 |        7,858 |                            215 |                0 | ~2m 05s                |
| **Total (2 facilities)**  | **16,063** |   **15,731** |                        **332** |            **0** | **4m 17s**             |

### Step-by-step verification run

| Step | Command | Rows | Wall-clock | Notes |
|------|---------|-----:|-----------:|-------|
| 1. Smoke test | `--facility=Inview-Fremont --modality=FRE-MRI --limit=5 --dry-run` | 5 | 1m 22s | Cost dominated by HTTP fetches (8 MB initial GET + fresh GET + facility POST); per-row cost is near-zero. |
| 2. Single-facility initial-load | `--facility=Inview-Fremont --initial-load` | 8,073 | 2m 05s | 215 popup fetches (avg ~4 popups/sec at 6 workers); rate **64.4 rows/sec** overall. |
| 3. Single-facility delta (re-run of step 2 with no source changes) | `--facility=Inview-Fremont` | 8,073 | 1m 31s | All-`unchanged`. Faster than initial-load because no DB writes. Proves content-hash determinism. |
| 4. Full-tenant initial-load | `--initial-load` | 16,063 | 4m 17s | Both `is_client=true` facilities; rate **62.4 rows/sec** overall. |
| 5. Full-tenant delta re-run | (no args) | 16,063 | 2m 43s | All-`unchanged` at scale; rate **97.9 rows/sec** for the read-only path. Confirms idempotency. |

### Where the time goes

For a single facility:

- **8.2 MB page transfer** for the initial GET + fresh GET + facility-change POST is the dominant fixed cost — ~30s per round-trip on a typical residential connection, ~60–90s combined.
- **Pass 2 popups** scale linearly with the number of recurring rules. At default `--workers=6`, NovaRIS sustains ~3–4 popups/sec (single-threaded behind the dialog endpoint — pushing past 8 workers triggers timeouts without speedup).
- **Supabase writes** are negligible (batched inserts/updates) — even an 8,073-row initial-load `INSERT` takes a few seconds.

### Scaling expectations

Extrapolating to a hypothetical fully-onboarded tenant with all 8
NovaRIS facilities active:

| Active facilities | Estimated rows | Estimated wall-clock | Notes |
|-------------------|---------------:|---------------------:|-------|
| 2 (current)       |         16,063 |               4m 17s | Measured |
| 4                 |         ~32,000 |              ~9 min | Linear extrapolation |
| 8                 |         ~64,000 |             ~18 min | Linear extrapolation |

Linear scaling holds because the per-facility cost is dominated by
network round-trips that don't share across facilities. The fresh
GET per facility (the VIEWSTATE-bloat mitigation) costs one extra
~30s per facility but is non-negotiable.

### When to be concerned about drift

If a future run takes substantially longer than these baselines —
e.g. a single facility crossing 5 minutes, or all-facility delta
crossing 30 minutes for a similar row count — likely causes are:

- NovaRIS server load (transient — re-run in an off-hour to confirm).
- Popup endpoint regressed to lower throughput → tune `--workers`
  down to 2-4 and watch the per-progress rate.
- Network path change (residential vs. corporate DNS / proxy
  differences can change the round-trip cost of the 8 MB page
  transfer dramatically).
- `pc1.modalities` not refreshed → unknown-machine NOTE block is
  large, with many rules silently dropped (look for the per-facility
  row count diverging from prior runs).

---

## Run recipes

### When to run this

Exception rules change more often than the procedures catalog —
LUNCH overrides, holidays, technician PTO, equipment downtime. Run
the scraper:

- After a new tenant is onboarded — once with `--initial-load`.
- Whenever the scheduling team has indicated rule changes in
  NovaRIS — run a default delta.
- Routinely, on the cadence that matches your tenant's volume of
  exception edits (weekly / daily / as-needed).

The scraper is **idempotent in delta mode** — re-running without
source-side changes produces an all-`unchanged` line and zero DB
writes.

### Routine on-demand run

```powershell
cd C:\Ajay\Scheduler-Automation-pc1
python novaRIS_exception_scraper.py
```

Optionally capture the output to a log file:

```powershell
python novaRIS_exception_scraper.py *> "C:\Logs\novaris_exceptions_$(Get-Date -f yyyy-MM-dd).log"
$LASTEXITCODE   # 0 = clean, 1 = login failed, 2 = facility not found,
                # 3 = partial (missing facility or failed popup)
```

### Fast iteration during development

```powershell
python novaRIS_exception_scraper.py --facility=Inview-Fremont --modality=FRE-MRI --limit=5 --dry-run
```

Caps work to one facility, filters parsed rows to machines whose
name contains `FRE-MRI`, then keeps at most 5 of those. The delta
breakdown still reflects real DB state so the
insert/update/unchanged logic can be validated without waiting for a
full run.

### Cross-tenant invocation

```powershell
python novaRIS_exception_scraper.py --client-id=1002
```

### Re-running after a partial failure

Delta mode is idempotent. If a previous run died partway through
(network blip, NovaRIS reboot, ctrl-C), just re-run — the
content-hash skip on unchanged rows means the second run finishes
faster and converges to a clean state.

`--initial-load` is **not** idempotent. If it dies partway, the
already-wiped facility will be in a partial state. Either re-run
`--initial-load` (per-facility scope means only the partial facility
gets re-wiped) or run a default delta and let it converge through
inserts.

---

## Troubleshooting

### "ERROR: could not find facility dropdown."

The grid page's HTML changed and the facility dropdown id/name
patterns no longer match. Save the page:

```powershell
python -c "from dotenv import load_dotenv; load_dotenv(); from novaRIS_common import login, make_session, BASE_URL; import os; os.makedirs('debug', exist_ok=True); s = make_session(); login(s); open('debug/ms_initial.html','w',encoding='utf-8').write(s.get(f'{BASE_URL}/ModalityScheduling.aspx').text)"
findstr /i "<select" debug\ms_initial.html
```

Update the `find_dropdown_name(soup, key)` call-sites at the top of
`scrape()` to match the new id/name pattern.

### Grid response is small / parses zero rows / facility not switching

If the POST returns the same page (default facility's grid)
regardless of which facility you POSTed for, NovaRIS may have
changed how it processes the postback. The current implementation
sets `__EVENTTARGET` to the facility dropdown's name and leaves
`__ASYNCPOST` unset (full-page postback). If the page now requires
a different trigger:

```powershell
python novaRIS_exception_scraper.py --facility=Inview-Fremont --limit=5 --save-grid=debug/grid_fre.html --dry-run
```

Inspect `debug/grid_fre.html`'s rendered facility — if it doesn't
match the requested facility, the postback wasn't recognized.
Workarounds: try a different `__EVENTTARGET` (e.g. an empty string
or one of the page's buttons), or capture a real browser's request
via DevTools and mirror its form fields.

### "ERROR: pc1.facilities.facility_name=X was not found in the live NovaRIS facility dropdown"

The facility was renamed in NovaRIS, OR `pc1.facilities.facility_name`
has a typo (case + whitespace sensitive). The error prints the live
dropdown options — update `pc1.facilities.facility_name` to match
exactly, then re-run.

### Unknown machine names piling up in the final NOTE

NovaRIS shows machines that `pc1.modalities` doesn't carry. Common
causes:

- `pc1.modalities` is stale → run
  `novaRIS_modalities_scraper.py --facility=<name>` to refresh.
- NovaRIS uses a placeholder/inactive machine the team doesn't
  want tracked (e.g. `ZTest`, `UNKNOWN`) → safe to ignore.

The scraper writes nothing for unknown machines, so leaving them
unresolved is non-destructive.

### Pass-1 parsing produces zero rows

The grid HTML or the row class names changed. Save the response:

```powershell
python novaRIS_exception_scraper.py --facility=Inview-Fremont --modality=FRE-MRI --save-grid=debug/grid.html --dry-run
```

Inspect `debug/grid.html` and update `parse_grid()` (specifically
the `GRID_ROW_RE` regex and the 10-cell extraction) to match the new
shape.

### Pass-2 popup fetches consistently fail

If `WARN popup` is logged for many rules, NovaRIS is either
rate-limiting or rejecting the requests. Try:

- Lower `--workers=2` and re-run.
- Save the first popup response — `--save-popup=debug/popup.html`
  — and inspect for an auth/session error page.

### `pc1.check_scheduleexceptions_consistency` trigger raises

The scraper resolves both `facility_id` and `modality_id` from
`pc1.facilities` / `pc1.modalities` rows that already belong to the
active tenant, so this shouldn't happen during a normal run. If it
does, capture the error message and inspect the resolved IDs in the
`ris_metadata` JSONB — the trigger names the conflicting
`client_id`s explicitly.

### Login fails

`NOVARISUSER` / `NOVARISPASSWORD` in `.env` are wrong, the account is
locked, or NovaRIS is down. The scraper prints
`ERROR: NovaRIS login failed.` and exits with status 1.

---

## Internals (one-paragraph summary)

`scrape()` resolves the tenant, logs in to NovaRIS, GETs
`ModalityScheduling.aspx`, parses the facility dropdown, and iterates
each `pc1.facilities` row with `is_client = true`. For each facility
it does a fresh GET (resets `__VIEWSTATE`), then `post_facility_grid()`
issues a plain ASP.NET full-page postback with `facilitiesDD` set to
the facility's NovaRIS ID and `ModalityDD` left blank — the server
re-renders the grid with every machine's rules for that facility.
`parse_grid()` extracts rows from the full HTML response (10 cells
per `<tr>` plus the IDs in `setActionSource('<modID>',
'<frequencyId>', '<rowIdx>')`). Each row's machine name is resolved
to a `modality_id` via `fetch_modality_machine_map()`
(`pc1.modalities.modality_machine` keyed by `facility_id`,
`is_active = true`); unknown names are dropped with a per-facility
debug line and accumulated for the final NOTE block. A
`ThreadPoolExecutor` then parallel-fetches
`ModalitySchedulingPopup.aspx` per Weekly/Monthly/Daily row (pass 2)
with exponential-backoff retry; `parse_popup()` extracts the
day-mask + repeat-every + end-date-override. `build_db_record()`
assembles each row with `client_id`, the resolved `facility_id` and
`modality_id`, content_hash computed over the documented hashed
fields, and `ris_metadata` carrying the writer name + NovaRIS
dropdown IDs for trace. `write_delta()` or `write_initial_load()`
then performs the DB writes per facility — both scope by
`(client_id, facility_id, source_record_key IS NOT NULL)` so
manually-inserted rows are never touched.

---

## Related

- [`pc1.scheduleexceptions`](./scheduleexceptions.md) — the table
  this scraper maintains.
- [`pc1.facilities`](./facilities.md) — supplies the per-facility
  iteration via `is_client = true`.
- [`pc1.modalities`](./modalities.md) — supplies the
  `modality_machine` ↔ `modality_id` lookup.
- [`novaRIS_modalities_scraper.py` doc](./novaris_modalities_scraper.md)
  — sibling per-facility scraper; same facility-resolution pattern.
- [`novaRIS_standardprocedure_scraper.py` doc](./novaris_standardprocedure_scraper.md)
  — sibling two-pass scraper; same content-hash / delta-sync pattern.
