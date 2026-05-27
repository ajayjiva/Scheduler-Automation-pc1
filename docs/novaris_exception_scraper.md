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
2. **Fresh GET** of `ModalityScheduling.aspx`, then POST a
   facility-change UpdatePanel postback. The server returns the
   refreshed modality dropdown options for this facility.
3. Resolve each NovaRIS modality dropdown option's machine name
   against `pc1.modalities.modality_machine` for this facility.
   Unknown names are skipped (logged in the per-facility debug line
   and reported in the final summary).
4. For each resolved `(modality_dd_id, modality_id)`:
   - **Pass 1** — POST a modality-change UpdatePanel postback,
     parse the grid (10 cells per row, `setActionSource('<modID>',
     '<frequencyId>', '<rowIdx>')` for the IDs).
   - **Pass 2** — for each row whose recurrence is `Weekly`,
     `Monthly`, or `Daily`, GET
     `ModalitySchedulingPopup.aspx?frequencyId=<id>&isOccurrence=false`
     in a `ThreadPoolExecutor` (default 6 workers) to read the
     day-of-week / day-of-month mask + `repeat_every` interval +
     any "End On" date override.
   - `recurrence=None` rules skip pass-2 entirely.
5. After all modalities for the facility are scraped, dedupe by
   `source_record_key` and run `write_delta` scoped to
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
| `--modality=NAME`       | Limit to one machine. **Case-insensitive substring** match against NovaRIS modality dropdown labels (e.g. `FRE-MRI`, or just `US` to hit every US machine at the facility). |
| `--limit=N`             | Cap rules per `(facility, modality)`. Combine with `--dry-run` for fast end-to-end testing. |
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

A grid filtered by Facility + Modality. The page is rendered via
an ASP.NET **UpdatePanel** AJAX postback — responses come back as
pipe-delimited segments (`length|type|id|content|...`) rather than
full HTML. The scraper concatenates the `updatePanel` segments and
parses them with BeautifulSoup just like a normal HTML page.

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

One HTTP POST per `(facility, modality)` pair.

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

### Per-facility pass-1 + per-modality pass-1+2

```
  [Inview-Fremont] selecting facility (NovaRIS id=2) ... ok.
  [Inview-Fremont] modality options: 8
  [pass1] Inview-Fremont/FRE-MRI (NovaRIS modID=1) ... 42 rows
  [pass1] Inview-Fremont/US1-F (NovaRIS modID=2) ... 17 rows
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
Done. Total records processed: 1,547  Elapsed: 8m 22s  Rate: 3.1/s

NOTE: 2 NovaRIS machine name(s) were not found in pc1.modalities — they were skipped without writing exception rows:
  - [Inview-Fremont] 'UNKNOWN'
  - [Inview-Oakland] 'ZTest'
```

The `NOTE` block is informational — these are NovaRIS-side machines
that PC1 doesn't track yet. If the names ought to be tracked, add
them to `pc1.modalities` (typically via the modalities scraper) and
re-run.

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

Caps work to one facility × one machine × 5 rules. The delta
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

### "ERROR: could not find dropdowns."

The grid page's HTML changed and the facility/modality dropdown
id/name patterns no longer match. Capture the page:

```powershell
python novaRIS_exception_scraper.py --facility=Inview-Fremont --save-grid=debug/grid.html --dry-run
type debug/grid.html | findstr /i "<select"
```

Update `find_dropdown_name(soup, key)` call-sites at the top of
`scrape()` to match the new id/name pattern.

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
it does a fresh GET (VIEWSTATE-bloat fix), POSTs a facility-change
UpdatePanel postback to load the per-facility modality dropdown,
then iterates each modality option whose machine name matches a
`pc1.modalities` row via `fetch_modality_machine_map()`. Unknown
machine names are accumulated into a final NOTE block.
`_post_dropdown_change()` handles the UpdatePanel transport
(`__ASYNCPOST=true`, ScriptManager field, pipe-delimited response
parsing) using helpers from `novaRIS_common.py`. For each
`(facility, modality)`, `parse_grid()` extracts rows (pass 1), and a
`ThreadPoolExecutor` parallel-fetches `ModalitySchedulingPopup.aspx`
per Weekly/Monthly/Daily row (pass 2). `parse_popup()` extracts the
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
