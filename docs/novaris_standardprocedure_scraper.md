# `novaRIS_standardprocedure_scraper.py`

Scrapes the standard-procedures catalog from NovaRIS
(`ViewStandardProcedures.aspx` + `StandardProcedureWizard.aspx`)
and syncs each procedure into Supabase `pc1.proceduresestimate`.

This is the **on-demand sync job** that keeps PC1's catalog of
procedure timings aligned with NovaRIS. It is run manually — there
is no schedule. See [When to run this](#when-to-run-this) below.

For the data model the scraper writes to, see
[`pc1.proceduresestimate`](./proceduresestimate.md).

---

## Quick reference

```powershell
# Normal on-demand run — delta sync across every modality
python novaRIS_standardprocedure_scraper.py

# Same, but don't write anything (preview)
python novaRIS_standardprocedure_scraper.py --dry-run

# One modality only (for testing the parser)
python novaRIS_standardprocedure_scraper.py --modality=US

# First-time seed for a newly-onboarded tenant
python novaRIS_standardprocedure_scraper.py --initial-load

# Cross-tenant one-off (overrides .env CLIENT_ID for this run)
python novaRIS_standardprocedure_scraper.py --client-id=1002

# Debug: cap to 5 procedures per modality and save the first grid + wizard
python novaRIS_standardprocedure_scraper.py --modality=US --limit=5 \
    --save-grid=debug/grid_us.html --save-wizard=debug/wizard_us.html
```

---

## Prerequisites

### `.env`

The scraper requires these variables (loaded via `python-dotenv`):

| Variable          | Used for                                  |
|-------------------|-------------------------------------------|
| `NOVARISURL`      | Base URL for NovaRIS (no trailing slash). |
| `NOVARISUSER`     | NovaRIS login user.                       |
| `NOVARISPASSWORD` | NovaRIS login password.                   |
| `SUPABASE_URL`    | Supabase project URL.                     |
| `SUPABASE_KEY`    | Supabase service role / write key.        |
| `CLIENT_ID`       | Optional. Default tenant (falls back to `1001` if unset). |

### Database

Migration `0002_create_pc1_proceduresestimate.sql` must be applied.
It creates `pc1.proceduresestimate` with:

- The three FKs the scraper relies on: `client_id` (NOT NULL),
  `facility_id` (nullable, always NULL for scraper writes),
  `modality_id` (nullable, always NULL for scraper writes).
- The four-shape unique partial index on
  `(client_id, COALESCE(facility_id,0), COALESCE(modality_id,0), source_record_key)`
  that lets manually-curated override rows coexist with the
  scraper's global rows.
- The `pc1.check_proceduresestimate_consistency` trigger that
  validates `client_id` matches the FK'd facility's and modality's
  `client_id`. The scraper writes only globals (both FKs NULL) so
  the trigger short-circuits on every scraper INSERT/UPDATE — no
  cost, but it's there to catch any future writer that touches
  override rows incorrectly.

### `pc1.clients`

The scraper reads `pc1.clients(id = client_id).slot_size` to compute
`required_slots = ceil(required_time / slot_size)` for every row.

- If the row is missing or `slot_size` is NULL, the scraper falls
  back to **15 minutes** silently. Set a real value on `pc1.clients`
  to get accurate per-tenant slot counts.
- The scraper does NOT read facility-specific `slot_size` overrides
  (because it writes global rows, not per-facility rows). If a
  facility's `slot_size` differs from the tenant default, the
  scheduler resolves the discrepancy at query time — not the
  scraper.

### No facility seeding required

Unlike `novaRIS_modalities_scraper.py`, this scraper iterates the
**Modality Type dropdown** on the procedures page, not the Facility
dropdown. The same procedure catalog applies to every facility a
tenant has, so no per-facility setup is needed before running.

---

## Modes

### Default — delta sync

For each Modality Type in the dropdown (or only the one named by
`--modality`):

1. **Pass 1** — POST `ViewStandardProcedures.aspx` with that modality
   selected, parse the grid into in-memory records
   (`source_record_key`, `required_time` from the grid, `modality_type`,
   `procedure_desc`).
2. After all modalities are scraped, dedupe across modalities (a
   procedure should appear once, but the API has surprised us).
3. **Pass 2** — for each unique procedure, GET
   `StandardProcedureWizard.aspx?type=dialog&standardProcedureID=<id>`
   in a `ThreadPoolExecutor` (default 6 workers). The wizard supplies
   the editable fields the grid omits: `anatomical_area`,
   `exam_prep_instructions`, `exam_prep_requires_prompt`. Modality and
   required_time are also re-read for verification — the wizard wins
   when both sources supply a value.
4. Fetch every existing **global** scraper-managed row in
   `pc1.proceduresestimate` for this client
   (`facility_id IS NULL AND modality_id IS NULL AND source_record_key IS NOT NULL`).
5. For each scraped row, decide:
   - **No existing row** → INSERT (counter: `insert`)
   - **Existing row, hash matches, `is_active = true`** → no-op
     (counter: `unchanged`)
   - **Existing row, hash differs, `is_active = true`** → UPDATE
     (counter: `update`)
   - **Existing row, `is_active = false`** → UPDATE + reactivate,
     preserve `created_at` (counter: `reactivated`; also rolled into
     `update`)
6. Compute the set of `source_record_key` values present in DB but
   missing from the scrape → bulk-UPDATE them to `is_active = false`
   (counter: `deactivate`).

INSERTs are batched in groups of 200. UPDATEs are issued **one row
at a time** — PostgREST upsert can't target the partial unique index
that backs the 4-shape override pattern, so the scraper uses
explicit per-row PATCHes. With realistic delta volumes (a handful of
changed rows per run), per-row UPDATEs are still fast.

Soft-deletes are batched in groups of 500.

### `--initial-load` — wipe + seed

1. `DELETE FROM pc1.proceduresestimate WHERE client_id = ? AND
   facility_id IS NULL AND modality_id IS NULL AND source_record_key
   IS NOT NULL`
2. Bulk INSERT every scraped row fresh.

**What survives the wipe:**

- Manually-inserted override rows (any row where `facility_id IS NOT
  NULL` or `modality_id IS NOT NULL`) — the scraper-managed rows are
  always globals, so the wipe leaves overrides alone.
- App-side rows with `source_record_key IS NULL` — those don't
  belong to the scraper's lifecycle.

**What gets burned:**

- Every previous scraper-written row's surrogate `id` — initial-load
  is not idempotent in the way delta is. Use it once when seeding a
  fresh tenant, or after a deliberate manual cleanup.

---

## Flags

| Flag                    | Effect |
|-------------------------|--------|
| `--initial-load`        | Switch from delta mode to wipe + bulk insert. Affects only scraper-managed global rows; override rows preserved. |
| `--dry-run`             | Parse + compute everything, including the existing-row fetch (so the insert/update/unchanged breakdown is realistic). Skip every Supabase write. |
| `--modality=NAME`       | Limit to one modality (e.g. `US`). Must match the dropdown's option `value` exactly. Useful for testing the parser against a small modality without waiting for all ~955 wizard fetches. |
| `--limit=N`             | Cap the number of procedures **per modality** to `N`. Combine with `--dry-run` for fast end-to-end testing. |
| `--workers=N`           | Parallel wizard fetches in pass 2. Default 6. NovaRIS is single-threaded behind the dialog endpoint — pushing past ~8 workers triggers timeouts without speeding up the run. |
| `--client-id=NNNN`      | Override the active tenant for this run. Beats the `CLIENT_ID` env var and the default. |
| `--quiet`               | Suppress per-modality progress chatter. The summary line and pass-2 progress lines still print. |
| `--save-grid=FILE`      | Save the HTML of the first grid response to `FILE`. Recommended path: `debug/<filename>.html` (gitignored). Use this when pass-1 parsing produces zero rows. |
| `--save-wizard=FILE`    | Save the HTML of the first wizard response to `FILE`. Recommended path: `debug/<filename>.html`. Use this when wizard fields come back empty or look wrong. |

---

## Two-pass scrape explained

NovaRIS exposes a procedure's data across two pages, and the scraper
has to visit both:

### Pass 1 — Grid (`ViewStandardProcedures.aspx`)

A single page that lists procedures filtered by Modality Type via a
dropdown. Each `<tr>` carries:

| Cell                    | Captured as       |
|-------------------------|-------------------|
| ID (in `onclick="setActionSource('12345','0')"`) | `source_record_key` |
| Required Time (minutes) | `required_time_grid` (used as fallback) |
| Modality Type           | `modality_type_grid` (used as fallback) |
| Procedure Name          | `procedure_desc_grid` (used as fallback) |

The scraper POSTs once per modality dropdown value to render each
filtered grid. One HTTP round-trip per modality (~10-20 total).

### Pass 2 — Wizard (`StandardProcedureWizard.aspx?type=dialog`)

A per-procedure detail dialog, accessed via query string
`?type=dialog&standardProcedureID=<id>&rwndrnd=<cache-buster>`. The
`rwndrnd` parameter is a NovaRIS-side cache-buster — without it
Telerik returns a stale page on hot reloads.

The wizard supplies five fields:

| Wizard control                            | Captured as |
|-------------------------------------------|-------------|
| `procedureName` input value               | `procedure_desc` (wins over grid) |
| `modalityTypeDD` selected option value    | `modality_type` (wins over grid) |
| `requiredTime` input value                | `required_time` (wins over grid) |
| `anatomicalAreaDD` selected option text   | `anatomical_area` |
| `examPrepInstructions` textarea text      | `exam_prep_instructions` |
| `requiredField` checkbox checked state    | `exam_prep_requires_prompt` |

One HTTP round-trip per procedure (~955 for Fremont). Pass 2 is the
dominant cost — wall-clock is bounded by NovaRIS's response time
times procedures-divided-by-workers.

### Why two passes (and not just the wizard)

The wizard endpoint requires the procedure ID. The procedure ID
isn't exposed anywhere except inside the grid's row `onclick`
handlers. So pass 1 is unavoidable — it's how the scraper learns
which IDs exist.

### When pass-2 fetches fail

If a wizard fetch fails all three retries (0.5s / 1s / 2s backoff),
the scraper:

- Logs `WARN wizard <pid>: <exception>` to stderr.
- Writes the procedure to PC1 anyway, using **only the grid-side
  data**. `anatomical_area`, `exam_prep_instructions`, and
  `exam_prep_requires_prompt` will be NULL/false.
- Exits with status `3` at the end if any wizard fetch failed, so an
  operator notices even from the exit code alone.

The reasoning: a procedure with missing intake metadata is still
useful (the scheduler primarily needs `modality_type`,
`procedure_code`, and `required_slots`). Re-running the scraper a
few minutes later usually clears transient failures.

---

## Log format reference

### Pass-1 (per-modality)

```
  [pass1] modality='US' ... 187 procedures.
  [pass1] modality='CT' ... 234 procedures.
  [pass1] modality='MR' ... 198 procedures.
```

### Pass-2 (progress every 50 procedures)

```
  [pass2] fetching wizard for 955 procedures (6 workers) ...
    progress: 50/955 (5%)  rate: 8.3/s  ETA: 1m 49s  failed=0
    progress: 100/955 (10%)  rate: 8.1/s  ETA: 1m 45s  failed=0
    ...
    progress: 955/955 (100%)  rate: 7.9/s  ETA: 0m 00s  failed=2
```

### Delta summary line

```
[delta] 955 scraped procedures  insert=3  update=12 (reactivated=1)  unchanged=940  deactivate=5 -> applied.
```

| Field                | Meaning |
|----------------------|---------|
| `scraped procedures` | Unique rows after dedupe across modalities. |
| `insert`             | Rows the scraper saw for the first time. |
| `update`             | Rows whose `content_hash` changed OR that were reactivated. **Includes** the `reactivated` count. |
| `reactivated`        | Rows that were `is_active=false` in DB and are now flipped back to `true`. **Subset of `update`.** |
| `unchanged`          | Rows whose `content_hash` matched DB exactly. Zero writes. |
| `deactivate`         | Rows present in DB but **missing** from this scrape — flipped to `is_active=false`. |

### Final summary

```
Done. Procedures processed: 945  Elapsed: 20m 04s  Rate: 0.8/s
```

`Procedures processed` is the post-dedupe row count, not the number
of writes.

---

## Measured performance

Real numbers from on-demand runs against the Inview tenant
(`client_id = 1`).

| Mode                 | Procedures | Wizard fetches | Wall-clock | Rate    |
|----------------------|-----------:|---------------:|-----------:|--------:|
| `--initial-load`     |        945 |            945 |   ~20 min  | ~0.8/s  |
| Default delta (all-unchanged) | 945 |        945 |   ~20 min  | ~0.8/s  |
| `--modality=US --dry-run` (subset) | ~190 | ~190 |    ~4 min  | ~0.8/s  |

Pass-2 wizard fetches dominate the wall-clock — one HTTP round-trip
per procedure, ~6 fetches/sec at default `--workers=6`. Increasing
workers past ~8 triggers timeouts without speeding the run up.

### Why delta isn't faster than initial-load

Counter-intuitive but correct: a default delta run with no
source-side changes (`unchanged = 945`) takes about the **same**
wall-clock as `--initial-load`, because both modes have to fetch
the same ~945 wizards in pass 2. The content-hash skip happens
**after** pass 2, in `write_delta()` — it short-circuits DB writes,
but the NovaRIS round-trips already paid.

A future optimization could skip the wizard fetch for procedures
whose grid-side fingerprint (modality + required_time + desc) is
already matched in DB. The current implementation prioritizes
simplicity and correctness over wall-clock; the procedures catalog
is small enough that 20 min/run is acceptable.

---

## Run recipes

### When to run this

Procedure catalogs change rarely — typically months between
meaningful edits in NovaRIS. There is **no automatic schedule**: the
scraper is run manually whenever a sync is wanted. Typical triggers:

- A new tenant was onboarded — run once with `--initial-load`.
- A NovaRIS admin has indicated that procedures changed — run a
  default delta to pick up the diff.
- A new CPT code was added or an existing procedure's required time
  was retuned — run a default delta.
- Routine maintenance check, roughly once a month.

The scraper is idempotent in delta mode: re-running without
source-side changes produces an all-`unchanged` line and zero DB
writes. There's no downside to running it more often than strictly
needed — except the ~20 minutes of wall-clock cost dominated by
pass-2 wizard fetches (see [Measured performance](#measured-performance)).

### Routine on-demand run

```powershell
cd C:\Ajay\Scheduler-Automation-pc1
python novaRIS_standardprocedure_scraper.py
```

Optionally capture the output to a log file (handy when diffing
successive runs):

```powershell
python novaRIS_standardprocedure_scraper.py *> "C:\Logs\novaris_procedures_$(Get-Date -f yyyy-MM-dd).log"
$LASTEXITCODE   # 0 = clean, 1 = login failed, 2 = unknown modality, 3 = one or more wizard fetches failed
```

### Future direction

This scraper is a **stopgap** until a direct NovaRIS ↔ PC1 sync API
exists. That API will replace the two-pass HTML-scrape model with a
defined push/pull contract, at which point this scraper (and its
~20-minute wall-clock) goes away entirely. Don't invest in heavy
operational tooling around it (cron jobs, alerting, log pipelines)
— keep it as a manual command for the few months it's expected to
remain.

### Previewing what would change

```powershell
python novaRIS_standardprocedure_scraper.py --dry-run
```

Same scrape + diff computation as a real run; the summary line
prints `(dry run, not applied)` instead of `-> applied`. Note that
dry-run still takes the full pass-2 wall-clock — it doesn't skip the
wizard fetches, only the DB writes.

### Fast iteration during development

```powershell
python novaRIS_standardprocedure_scraper.py --modality=US --limit=5 --dry-run
```

Caps work to one modality × 5 procedures = 5 wizard fetches. The
delta breakdown still reflects real DB state for those 5 procedures
so you can validate the insert/update/unchanged logic without
waiting 20 minutes per attempt.

### Cross-tenant invocation

```powershell
python novaRIS_standardprocedure_scraper.py --client-id=1002
```

The `--client-id` flag wins over `.env`'s `CLIENT_ID`. Useful for
running ad-hoc against a tenant other than the one configured in the
environment.

### Re-running after a partial failure

Delta mode is idempotent. If a previous run died partway through
(network blip, NovaRIS reboot, ctrl-C), just re-run with the same
flags — the content-hash skip on unchanged rows means the second run
finishes faster and converges to a clean state.

`--initial-load` is NOT idempotent. If it dies partway, the table
will be in a partial state (some rows deleted, some not yet
inserted). Either re-run `--initial-load` to complete the wipe +
re-seed, or run a default delta and let it converge through inserts.

---

## Troubleshooting

### "ERROR: could not find Modality Type dropdown on ViewStandardProcedures.aspx"

The grid page's HTML changed and the dropdown's id/name no longer
contains `modalitytype`. Capture the page and inspect:

```powershell
python novaRIS_standardprocedure_scraper.py --dry-run --save-grid=debug/grid.html
type debug/grid.html | findstr /i "<select"
```

Update `find_modality_dropdown()` in the scraper to match whatever
id/name the page now uses.

### "ERROR: modality 'X' not found in dropdown options"

The value passed to `--modality` doesn't match any dropdown
`<option value="...">` value (case-sensitive). The error doesn't
list available options because the script exits before the
post-fetch summary. Re-run without `--modality` to see the full
list, or capture the initial grid HTML and read the `<select>`
directly.

### Pass-1 parsing produces zero rows

The grid HTML or the row class names probably changed. Save the
first grid response:

```powershell
python novaRIS_standardprocedure_scraper.py --modality=US --limit=1 \
    --save-grid=debug/grid_us.html --dry-run
```

Inspect `debug/grid_us.html` and update `parse_grid()` (specifically
the `setActionSource` regex `SET_ACTION_RE` and the cell-extraction
logic) to match the new shape.

### Pass-2 wizard fetches consistently fail

If `failed=N` is non-zero with `N` close to the total, NovaRIS is
either rate-limiting or rejecting the wizard requests. Try:

- Lower `--workers=2` and re-run. If success rate jumps, NovaRIS is
  rate-limiting; tune workers down permanently.
- Save the first wizard response — `--save-wizard=debug/wizard.html`
  — and inspect for an auth/session error page. Stale session
  cookies are the usual cause; the scraper re-logs in at the start
  of each run, but if a previous run held a stale cookie a forced
  fresh `.env` re-read sometimes helps.

### `procedure_code` is `[]` for procedures that should have CPT codes

The procedure description doesn't end in a trailing parenthesized
CPT list (`(76705)`) and doesn't match the fallback regex
(`...US 76641`). Check `procedure_desc` in DB:

```sql
SELECT source_record_key, procedure_desc
  FROM pc1.proceduresestimate
 WHERE client_id = 1
   AND array_length(procedure_code, 1) IS NULL
 LIMIT 20;
```

If the description has a CPT code in a format the parser doesn't
recognize, add a new regex branch to `parse_procedure_codes()` and
re-run a delta to update those rows.

### `pc1.check_proceduresestimate_consistency` trigger raises

Should not happen during scraper runs — the scraper writes only
`facility_id=NULL` and `modality_id=NULL` rows, which short-circuit
the trigger's checks. If it raises anyway, the scraper has a bug
that's writing a non-NULL FK by mistake. Capture the error and
inspect the record build path.

### Supabase writes fail mid-run

The scraper does not currently checkpoint. If a delta run dies
after some INSERTs/UPDATEs have committed and others haven't, the
safe recovery is just to re-run — delta mode is idempotent and the
unchanged rows short-circuit. `--initial-load` is **not**
idempotent; see [Re-running after a partial failure](#re-running-after-a-partial-failure).

### Login fails

`NOVARISUSER` / `NOVARISPASSWORD` in `.env` are wrong, the account
is locked, or NovaRIS is down. The scraper prints `ERROR: NovaRIS
login failed.` and exits with status 1.

---

## Internals (one-paragraph summary)

`scrape()` resolves the tenant, logs in to NovaRIS, GETs
`ViewStandardProcedures.aspx`, parses the Modality Type dropdown,
and iterates each modality value via `post_grid_for_modality()`
(pass 1). Grid rows are deduped on `source_record_key`. The unique
procedure IDs feed a `ThreadPoolExecutor` (default 6 workers) that
calls `fetch_wizard()` per ID with exponential backoff (pass 2).
After both passes, `get_global_slot_size()` reads
`pc1.clients.slot_size` (fallback 15) and `build_db_record()`
assembles each row with `facility_id=NULL`, `modality_id=NULL`,
content_hash computed over the documented hashed fields, and
`ris_metadata` carrying the writer name + originating modality
dropdown value. `write_delta()` or `write_initial_load()` then
performs the DB writes — both use the global-only filter
(`facility_id IS NULL AND modality_id IS NULL AND source_record_key
IS NOT NULL`) so manually-curated override rows and app-side rows
are never touched. The delta path uses per-row UPDATEs (PostgREST
can't ON CONFLICT against a partial unique index) and batched
INSERTs + soft-deletes. See the module docstring in
`novaRIS_standardprocedure_scraper.py` for the exact field-by-field
schema mapping.

---

## Related

- [`pc1.proceduresestimate`](./proceduresestimate.md) — the table
  this scraper maintains; see the override-design section there for
  how the scraper's global rows interact with manually-inserted
  facility/machine overrides.
- [`pc1.modalities`](./modalities.md) and the
  [modalities scraper](./novaris_modalities_scraper.md) — for the
  parallel per-machine catalog, populated by a separate (per-facility)
  scraper.
