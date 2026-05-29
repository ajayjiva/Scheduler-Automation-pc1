# Session Handoff — Scheduler Automation (pc1)

> **Purpose.** When you start a new Claude session for this repo, point
> Claude at this file as the first step: it captures the project's
> stable state, conventions, the established scraper pattern, the
> user's working preferences, deferred work, and the current task so
> we don't burn context re-deriving any of it.
>
> The **authoritative** references are:
> - `docs/facilities.md`, `docs/modalities.md`,
>   `docs/proceduresestimate.md`, `docs/scheduleexceptions.md` —
>   table schemas
> - `docs/novaris_modalities_scraper.md`,
>   `docs/novaris_standardprocedure_scraper.md`,
>   `docs/novaris_exception_scraper.md` — scraper operations
> - `migrations/*.sql` — the actual schema in Supabase
> - This file — a *navigator* pointing into the above plus
>   session-context
>
> Keep this file updated as the project evolves. Any major addition
> (new table, new script, new deferred item, new convention) should
> land a one-liner here so future sessions stay oriented.

---

## 1. Project one-liner

Multi-tenant patient-scheduling system that scrapes data from a RIS
(currently NovaRIS) into Supabase (`pc1` schema) and will eventually
compute appointment options for patients with multiple orders across
multiple imaging modalities.

**Stack:** Python 3 + Supabase (PostgREST + Postgres) + NovaRIS
(ASP.NET WebForms scraping). Multi-tenant-ready from day one; primary
tenant today is `client_id = 1` (Inview Imaging — historically `1001`
in the legacy project; renumbered as part of the pc1 cutover).

**Predecessor:** A legacy project against `public.*` tables exists
and was the starting point for many of these patterns. The pc1
codebase is a clean rebuild — not a verbatim port. Conventions have
been standardized and tightened during the rewrite (see §4).

## 2. Repo state

- **Branch in use:** `main` (the only long-lived branch).
- **All feature work** lives in short-lived `feature/<topic>` or
  `docs/<topic>` branches → PR → squash-merge to `main` → branch
  deleted.
- **Recent PRs** (most recent first):
  - **#5 — Add NovaRIS exception scraper (Phase 2).**
    `novaRIS_exception_scraper.py` + `docs/novaris_exception_scraper.md`.
    Per-facility iteration via plain ASP.NET full-page postbacks
    (the page does NOT use MS UpdatePanel AJAX — the first
    implementation tried the legacy AJAX transport and was rewritten
    after a `pageRedirect → DefaultErrorPage` diagnosed via
    `findstr` on the initial page). Server returns every machine's
    rules for a facility in one response (`ModalityDD=""`), so no
    per-modality iteration is needed. End-to-end verified at scale:
    16,063 rows across 2 `is_client=true` facilities in 4m 17s
    (initial-load), all-`unchanged` on delta re-run. Doc includes
    a "Measured performance" section with per-facility throughput,
    step-by-step verification table, time-breakdown analysis, and
    linear-scaling estimates for fully-onboarded tenants.
    Side-update: corrected `novaris_standardprocedure_scraper.md`
    timing estimate from ~15 min → ~20 min (actual measured) and
    added a "Why delta isn't faster than initial-load" note.
  - **#4 — Add pc1.scheduleexceptions table (Phase 1: schema + docs).**
    `migrations/0003_create_pc1_scheduleexceptions.sql` + 38 boolean
    recurrence-mask columns (weekdays_only + 7 day-of-week + 31
    day-of-month) + CHECK constraints on `recurrence` ∈
    {None,Daily,Weekly,Monthly} and `type` ∈ {Hard,Soft} + unique
    partial index on `(client_id, facility_id, source_record_key)
    WHERE source_record_key IS NOT NULL` (no 4-shape override
    pattern — exception rules are operational events, not catalog
    entries) + cross-tenant trigger
    `pc1.check_scheduleexceptions_consistency()` +
    `docs/scheduleexceptions.md`.
  - **#3 — Add NovaRIS standard-procedure scraper (Phase 2).**
    Two-pass scrape (grid → wizard) into `pc1.proceduresestimate`.
    Includes a pass-1 `__VIEWSTATE`-bloat fix (fresh GET per
    modality) that reduced full runtime from ~196 min to ~20 min,
    the `_write_debug_html()` helper that auto-creates `debug/`,
    and deprecation banners on the now-dead `MODALITY_MAP` /
    `FACILITY_MODALITIES` dicts in `novaRIS_common.py`.
- **All migrations are applied** in Supabase. Schema matches what's
  in `migrations/` and what's documented in the per-table docs.

## 3. Read these first when picking up

| File | What it covers |
|---|---|
| **This file (`docs/session-handoff.md`)** | Project orientation, conventions, established patterns, working preferences |
| [`docs/facilities.md`](./facilities.md) | `pc1.facilities` schema — the FK target for everything else. Naming-exactness contract with NovaRIS; `is_client` gate semantics |
| [`docs/modalities.md`](./modalities.md) | `pc1.modalities` schema — one row per physical machine. Business key, content_hash composition, lifecycle |
| [`docs/proceduresestimate.md`](./proceduresestimate.md) | `pc1.proceduresestimate` schema — procedure catalog. Four-shape per-machine override design. Cross-tenant trigger semantics |
| [`docs/scheduleexceptions.md`](./scheduleexceptions.md) | `pc1.scheduleexceptions` schema — scheduling-exception rules (LUNCH, holidays, downtime). Flat recurrence-mask columns, CHECK-constrained `recurrence`/`type`, no override pattern |
| [`docs/machineschedule.md`](./machineschedule.md) | `pc1.machineschedule` schema — slot calendar. Generator-produced (not RIS-sourced), so no `is_active`/`content_hash`/`ris_*`. UTC `date_and_time_utc` plus facility-local `start_time` / `end_time` / `seq` (the engine-contract `YYYYMMDDHHMMSS` integer). Paired `exceptions`/`exception_ids` arrays. `order_id` FK deferred to Phase 4 |
| [`docs/machineschedule_generator.md`](./machineschedule_generator.md) | `generate_machineschedule.py` — Phase 2 writer for the slot calendar. 24-hour day generation, facility-local → UTC via `zoneinfo`, ON CONFLICT DO NOTHING idempotency, modality filter (`is_active=true AND status IN ('Active', NULL)`). No destructive flags — wipe is SQL-only |
| [`docs/novaris_modalities_scraper.md`](./novaris_modalities_scraper.md) | The first NovaRIS scraper — reference for per-facility iteration, login flow, runtime facility-id resolution |
| [`docs/novaris_standardprocedure_scraper.md`](./novaris_standardprocedure_scraper.md) | The second NovaRIS scraper — reference for per-modality iteration, two-pass (grid → wizard) scrape, ThreadPoolExecutor for parallel detail fetches |
| [`docs/novaris_exception_scraper.md`](./novaris_exception_scraper.md) | The third NovaRIS scraper — reference for per-facility iteration with plain full-page postbacks, name-based modality_id lookup, post-parse `--modality` filtering |

For a task that touches a new schema, read at least the relevant
table doc + the closest sibling scraper doc — that's enough to match
the established pattern.

## 4. Conventions (concise reference)

### 4.1 Standardized terminology

| Term | Meaning |
|---|---|
| **`pc1` schema** | The new (rebuilt) Supabase schema. Multi-tenant by `client_id`. Default `public.*` tables are legacy and should not be referenced from new code. |
| **`is_client` gate** | `pc1.facilities.is_client = true` flags a facility as currently contracted by the tenant. Scrapers iterate only `is_client=true` facilities; `false` rows are inactive contracts kept for historical FK targets. |
| **Two-phase pattern** | New tables ship in two PRs: Phase 1 = schema migration + table doc; Phase 2 = scraper + scraper doc. Each phase = its own PR. SQL verification before merging schema PRs; end-to-end run before merging scraper PRs. |
| **Four-shape override pattern** | For tables with `facility_id` + `modality_id` FKs that need per-facility / per-machine variants (`pc1.proceduresestimate`). The 4 shapes are `(NULL, NULL)` global / `(X, NULL)` facility-level / `(NULL, K)` per-machine global / `(X, K)` facility+machine. Enforced via a unique partial index using `COALESCE(<col>, 0::bigint)` sentinels. |
| **`content_hash`** | SHA-256 of business fields, stored on every scraper-managed row. Delta sync skips rows whose recomputed hash matches the DB-stored value — these are `unchanged` rows with **zero writes**. Excludes `client_id`, audit timestamps, `is_active`, `created_by`/`updated_by`. |
| **`unchanged` / `insert` / `update` / `reactivated` / `deactivate`** | The five outcomes of a delta-sync row: hash matched / new row / hash differed / `is_active` flipped back to `true` (subset of `update`) / present in DB but missing from scrape → `is_active=false`. |
| **`source_record_key`** | The RIS-side stable identifier (e.g. NovaRIS `frequencyId`, `standardProcedureID`). Half of the business key for scraper-managed rows. NULL allowed so manually-inserted rows can coexist. |
| **Fresh GET per facility/modality** | VIEWSTATE-bloat mitigation. ASP.NET grows `__VIEWSTATE` across postbacks; without resetting between iterations, payloads snowball and NovaRIS spends minutes deserializing each one. Cost: ~1s/iteration. Win: hours saved. |
| **Cross-tenant safety trigger** | `BEFORE INSERT OR UPDATE OF client_id, facility_id, modality_id` trigger calling `pc1.check_<table>_consistency()`. Validates that `client_id` matches the FK'd facility's and modality's tenant. Short-circuits when both FKs are NULL. |
| **Soft-delete via `is_active`** | Removals from the RIS are represented by `UPDATE … SET is_active = false`, never `DELETE`. Scrapers reactivate (`is_active=true`) on next sync if the row reappears. App-side `DELETE` is reserved for true cleanup. |
| **Writer identity in `ris_metadata.writer`** | Scraper rows write `created_by` / `updated_by` as `NULL` (bigint FK to `pc1.user_profiles` — scripts have no user). The script identity goes in `ris_metadata.writer` for traceability. |
| **On-demand sync** | All scrapers run manually — no schedule, no cron. Re-run when the upstream RIS has changed. Delta mode is idempotent (re-runs converge to `unchanged=N`). |
| **`debug/` folder** | Gitignored landing pad for `--save-grid` / `--save-wizard` / `--save-popup` dumps. Auto-created by `_write_debug_html()`. Never commit anything here. |
| **Measured performance** | Every scraper doc includes a "Measured performance" section with real numbers from end-to-end verification — per-facility row counts + wall-clock + rate. Future operators compare drift against these baselines. |

### 4.2 Schema conventions

Every tenant-scoped table in `pc1` follows this shape:

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PRIMARY KEY | Always. Don't use serial/int4 |
| `client_id` | `bigint NOT NULL` | FK to `pc1.clients(id)`. **No DEFAULT** (multi-tenant footgun) |
| `<entity FKs>` | `bigint NOT NULL` or NULL | E.g. `facility_id`, `modality_id`. NULL allowed when the entity supports a "global" tier (override patterns) or for forward-compat (`pc1.scheduleexceptions.modality_id`) |
| **Business fields** | varies | Whatever the entity actually represents |
| `is_active` | `boolean NOT NULL DEFAULT true` | Soft-delete flag; `false` means removed from source |
| `source_record_key` | `varchar(255) NULL` | Stable RIS-side ID, NULL allowed so manually-inserted rows can coexist |
| `content_hash` | `varchar(255) NULL` | SHA-256 of business fields for delta-sync skip-if-unchanged |
| `ris_system` | `varchar(50) NULL DEFAULT 'konica_exa'` | Which RIS produced the row |
| `ris_sync_status` | `varchar(20) NULL DEFAULT 'synced'` | Sync state |
| `ris_last_synced_at` | `timestamptz NULL` | When RIS-side data was last pulled |
| `ris_metadata` | `jsonb NULL` | Writer name + raw source-system trace info |
| `synced_at` | `timestamptz NULL` | When PC1 last wrote this row from a sync |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Set once on INSERT |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Writers MUST set explicitly on every UPDATE — DEFAULT only fires on INSERT |
| `created_by` | `bigint NULL` | FK to `pc1.user_profiles(id) ON DELETE SET NULL`. NULL when written by automation |
| `updated_by` | `bigint NULL` | Same |

**Naming**: `<target>_id` for FK columns (`facility_id`, `modality_id`, `client_id`).
Audit columns are **`created_at`/`updated_at`** (not `creation_date`/`last_update_date`).
Audit user columns are **`created_by`/`updated_by`** as bigint FKs (not text script names).

**CHECK constraints** for enum-like text columns: pin values to the
documented vocabulary so scraper bugs fail at INSERT time. Pattern:
`CHECK (col IS NULL OR col IN ('val1', 'val2', ...))`. Used on
`pc1.scheduleexceptions.recurrence` and `.type`.

### 4.3 Migration conventions

- One file per migration, in `migrations/`, numbered like
  `0001_<purpose>.sql`, `0002_<purpose>.sql`, …
- **Idempotent**: `CREATE TABLE IF NOT EXISTS`, `CREATE OR REPLACE
  FUNCTION`, `CREATE INDEX IF NOT EXISTS`, `DROP TRIGGER IF EXISTS`
  before `CREATE TRIGGER`. The migration must be safe to replay.
- Top of file: comment block explaining purpose + shape rationale +
  idempotency promise.
- Applied by hand in the Supabase SQL editor by the user **before
  merging the PR**. PRs that include only migration files wait on
  this verification step. The table doc (e.g.
  `docs/scheduleexceptions.md`) includes a "Verification queries
  (post-migration)" section with the SQL to confirm shape.
- Existing migrations:
  - `0001_pc1_modalities_novaris_compat.sql` — relaxes legacy NOT
    NULL constraints, adds `(client_id, facility_name)` unique
  - `0002_create_pc1_proceduresestimate.sql` — full table create +
    4-shape override unique index + cross-tenant trigger
  - `0003_create_pc1_scheduleexceptions.sql` — full table create +
    CHECK constraints + simple `(client_id, facility_id,
    source_record_key)` unique partial + cross-tenant trigger
  - `0004_create_pc1_machineschedule.sql` — full table create +
    `(client_id, facility_id, modality_id, date_and_time)` UNIQUE
    business key + GIN on `exception_ids` + cross-tenant trigger.
    No `is_active` / `content_hash` / `source_record_key` / `ris_*`
    (slots are generator-produced, not RIS-sourced). `order_id`
    column present but FK to `pc1.orders` deferred until Phase 4.
    The `date_and_time` column was renamed in 0005 — see below.
  - `0005_rename_machineschedule_dt_to_utc.sql` — renames
    `pc1.machineschedule.date_and_time` to `date_and_time_utc` so
    the UTC nature is explicit on every read (matches the
    facility-LOCAL `start_time` / `end_time` columns by contrast).
    Idempotent via an `information_schema` guard. Discovered during
    Phase 2 verification; see `docs/machineschedule.md` →
    [The `seq` engine contract](./machineschedule.md#the-seq-engine-contract)
    for the related `seq` column semantics agreed at the same time.
  - **Next available number: 0006**

### 4.4 Scraper conventions

Established by `novaRIS_modalities_scraper.py`, refined by
`novaRIS_standardprocedure_scraper.py`, hardened by
`novaRIS_exception_scraper.py`. New scrapers should follow this pattern.

**Module structure** (top-down):

```python
"""<scraper>.py — module docstring with schema mapping table"""
from dotenv import load_dotenv; load_dotenv()

import argparse, contextlib, hashlib, io, json, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup

from supabase_client import get_supabase
from client_context import add_client_id_arg, resolve_client_id
from novaRIS_common import (
    BASE_URL, USERNAME,
    extract_all_form_fields, extract_form_state_from_html,
    login, make_session,
)

# Constants
URL_FOO       = f"{BASE_URL}/...aspx"
WRITER_NAME   = "<scraper_filename>.py"   # for ris_metadata.writer
RIS_SYSTEM    = "NovaRIS"
PC1_SCHEMA    = "pc1"

_QUIET = False
def vprint(*a, **kw):
    if not _QUIET: print(*a, **kw)
def _table(supabase, name): return supabase.schema(PC1_SCHEMA).table(name)
def _chunked(seq, n): ...
def _write_debug_html(path, html): ...   # auto-creates parent dir

# ─── HTML / form parsing helpers (per-scraper, specific to its page) ─────
# ─── Pass 1 (grid / list) ────────────────────────────────────────────────
# ─── Pass 2 (detail / dialog) — if needed ────────────────────────────────
# ─── Content hash + record building ──────────────────────────────────────
HASHED_FIELDS = (...)   # business fields only
def compute_hash(rec): ...
def build_db_record(...): ...
# ─── Supabase IO ─────────────────────────────────────────────────────────
def _fetch_existing(supabase, client_id, ...): ...
def write_initial_load(supabase, client_id, ..., records, dry_run): ...
def write_delta(supabase, client_id, ..., records, dry_run): ...
# ─── Driver ──────────────────────────────────────────────────────────────
def scrape(args): ...
def main(): ...
```

**Key patterns to apply**:

| Pattern | Why |
|---|---|
| Fresh GET before each facility/modality POST | Prevents `__VIEWSTATE` bloat across ASP.NET postbacks (cost: 1s/iteration; saves hours on multi-iteration runs) |
| `_table(supabase, "name")` (schema-qualified) everywhere | pc1 is not the default schema; PostgREST needs explicit selection |
| Writes set both `created_at` and `updated_at` on INSERT (column DEFAULT only fires on INSERT) | Same value, ensures the column list is uniform across batch INSERTs |
| Writes always update `updated_at` explicitly on UPDATE | DEFAULT only fires on INSERT |
| `created_by` / `updated_by` write NULL for automation; identity goes in `ris_metadata.writer` | Bigint FKs to `user_profiles` aren't meaningful for scripts; user attribution comes when a real user later edits a row |
| `ris_metadata` includes `{writer: WRITER_NAME, ...}` | Trace info for debugging |
| Content hash excludes `client_id`, `is_active`, all timestamps | Re-keying / soft-deletes shouldn't churn hashes |
| Per-row UPDATE for changes, batched INSERTs in groups of 200 | PostgREST upsert can't target partial unique indexes (override patterns + scheduleexceptions both use them) |
| Soft-deletes via batched UPDATE in groups of 500 | Keyed list works with `.in_("id", batch)` |
| Scoped wipe on initial-load + delta fetch filtered to scraper-managed rows (`source_record_key IS NOT NULL`) | Preserves manually-inserted rows |
| Plain full-page ASP.NET postbacks (NOT MS UpdatePanel AJAX) | The legacy AJAX transport doesn't match the current NovaRIS pages. Default to plain postback with `__EVENTTARGET = dropdown_name`. Use `findstr` for `PageRequestManager` if uncertain — its absence confirms plain transport. |
| ThreadPoolExecutor with default 6 workers for per-row detail fetches | NovaRIS is single-threaded behind dialog endpoints; >8 workers triggers timeouts |
| Per-procedure/per-popup retry with exponential backoff (0.5/1/2s) | Smooths over transient NovaRIS hiccups |
| Debug HTML output via `--save-grid`/`--save-wizard`/`--save-popup` flags writes to `debug/<file>` (gitignored) | Folder auto-created by `_write_debug_html()` helper |
| Standard CLI flags: `--initial-load`, `--dry-run`, `--modality`/`--facility`, `--limit`, `--workers`, `--quiet`, `--client-id`, `--save-grid` (and `--save-wizard`/`--save-popup` for two-pass scrapers) | Consistent across scrapers |
| Exit codes: 0=clean, 1=login failed, 2=specified entity not found, 3=partial failure (some rows skipped or detail fetches failed) | Cron-friendly |
| Unknown machine names (NovaRIS-side names not in `pc1.modalities`) skipped with per-facility debug line + final NOTE block with per-machine row counts | Renames in NovaRIS surface visibly without failing the run |

**`pc1.clients` for tenant-level config**: scrapers that need per-tenant
defaults (e.g. `slot_size`) read from `pc1.clients` directly; there is
no `clientparameters` table. Facility-specific overrides live on
`pc1.facilities` (e.g. `pc1.facilities.slot_size`). The scheduling
engine resolves the merge: facility row's value wins if non-NULL,
otherwise falls back to client row.

### 4.5 Operational conventions

- **One feature branch per piece of work**, branched from latest `main`
- **Branch naming**: `feature/<short-name>`, `fix/<short-name>`, `docs/<short-name>`
- **Don't push empty branches** — push after first commit
- **Small, focused PRs** — one concern per PR
- **Sequential, not stacked** — merge each PR before starting the next
- **Two-phase pattern for new tables**: Phase 1 = schema migration + table doc; Phase 2 = scraper + scraper doc. Each phase = its own PR
- **SQL verification before merging schema PRs** — user applies the migration in Supabase and runs the table doc's verification queries; only then does the PR get merged
- **End-to-end verification before merging scraper PRs** — at minimum: smoke test with `--limit=5 --dry-run`, then `--initial-load` (single facility), then a delta re-run to confirm `unchanged=N` (proves content_hash determinism), then a full-tenant run. The "Measured performance" section in the scraper doc records the verification numbers.
- **Squash-merge** all PRs. Trim the auto-concatenated squash body to just the PR summary
- **Delete branch** locally and remotely after merge:
  ```powershell
  git checkout main
  git pull --ff-only origin main
  git branch -D <branch-name>     # safe-fails if never had local copy
  git fetch --prune origin
  ```

### 4.6 Deprecation policy

- **Don't delete dead code** during feature work. Mark with a
  visible banner comment explaining what it was, what the live
  equivalent is, and that it's scheduled for removal
- The final consolidation pass (when the user is ready) will sweep
  everything marked DEPRECATED out

Current deprecation markers:
- `novaRIS_common.py:73-95` — `MODALITY_MAP` and `FACILITY_MODALITIES`
  dicts. Live equivalents are `pc1.modalities` + joins to
  `pc1.facilities`

## 5. Working preferences

Learned across multiple sessions. Apply to all subsequent sessions.

### 5.1 Process

- **Don't write code without confirmation** for non-trivial design
  decisions. Give thoughts first, then build only after explicit
  go-ahead. For obvious mechanical implementations (e.g. "implement
  what we just agreed on"), proceed without re-asking
- **Recommendations with rationale** — when offering options,
  recommend one and explain why; let the user pick or redirect
- **Surface alternatives the user might not have considered**, but
  don't list every possibility — focus on the 2-3 that actually matter
- **Tables over prose** when comparing options or listing structured
  info. The user reads quickly through tables
- **Explain the WHY** for non-obvious decisions, especially in
  commit messages
- **Step-by-step PowerShell** for any procedure the user needs to
  run locally. Always assume Windows + PowerShell; `&&` doesn't
  chain in older PowerShell — use separate commands or `;`

### 5.2 Verification rhythm

- Each PR gets verified before merge (SQL queries for schema PRs,
  end-to-end runs for scraper PRs)
- Each scraper run gets verified with the documented sanity queries
- For scrapers, a working sequence has emerged that minimizes total
  time: skip the dry-run entirely if the data is non-production,
  jump straight to `--initial-load` on a single facility, then a
  delta re-run as the determinism proof, then a full-tenant
  initial-load, then a full-tenant delta. Reset path if anything
  goes wrong is documented in `docs/novaris_exception_scraper.md`.

### 5.3 Communication tone

- Concise. The user reads fast and prefers tight responses over
  exhaustive ones
- One-sentence updates between tool calls are good; one-paragraph
  updates are usually too much
- Celebrate wins briefly and move forward — don't dwell on results
  the user already sees in their terminal
- When something goes wrong, lead with diagnosis + fix, not apology

### 5.4 Branch/commit hygiene

- Always commit related changes together (don't split "the fix" and
  "the test for the fix" into separate commits)
- Always commit unrelated changes separately (don't bundle a perf
  fix and a doc update)
- Commit messages: short title (under 70 chars), then body with
  rationale + per-line breakdown for larger changes
- Reference legacy patterns explicitly when porting ("matches the
  pattern in novaRIS_modalities_scraper.py")

## 6. Deferred / pending work

The forward roadmap, in execution order:

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | Port **`pc1.machineschedule`** — the slot calendar | **Phase 2 IN PROGRESS** — schema migration `0004` shipped (PR #7); `generate_machineschedule.py` + `docs/machineschedule_generator.md` + post-verification fixups (migration `0005` column rename to `date_and_time_utc`; `seq` populated as facility-local YYYYMMDDHHMMSS; initial `availability` driven by `[opening_time, closing_time)` business-hours window) on `feature/pc1-machineschedule-generator` (PR #8). Phase 3 (reconciler) still pending — see §8 | Generates blank availability slots per `(modality, date_and_time_utc)` for a rolling window. Legacy: `create_blank_calendar.py` |
| 2 | Port **`reconcile_exceptions.py`** | Deferred — depends on (1) Phase 2 landing first | Reads active `pc1.scheduleexceptions`, expands recurrence rules to slot-level, surgically updates `pc1.machineschedule.availability` + `exceptions[]` + `exception_ids[]`. Existing legacy script in repo is a strong starting point — it just needs pc1-schema adaptation |
| 3 | Scrape **patient orders** into `pc1.orders` | Deferred — needed to drive the scheduling engine | Source page in NovaRIS TBD (likely `Orders.aspx` or similar). May or may not need a two-pass scrape. New table; two-phase PR pattern applies |
| 4 | Port scheduling engine (patient-options builder) | Deferred — depends on (1), (2), (3) | Core business logic; legacy `main.py`. Compute appointment options for patients with multiple orders across multiple modalities |
| — | Delete `MODALITY_MAP` + `FACILITY_MODALITIES` from `novaRIS_common.py` | Deferred — marked DEPRECATED in PR #3 | Final consolidation pass |
| — | `pc1.user_profiles` lifecycle | Open question | Referenced by every audit FK but how does a row get into it? Login UI? Manual SQL? |

## 7. Project Today (snapshot)

**Tables in pc1 schema**:
- `pc1.clients` — tenant identity + global per-tenant defaults (`slot_size`, etc.)
- `pc1.facilities` — per-tenant facility list with `is_client` gate and per-facility overrides
- `pc1.modalities` — per-facility machine inventory, populated by `novaRIS_modalities_scraper.py`
- `pc1.proceduresestimate` — procedure catalog with 4-shape override design, populated by `novaRIS_standardprocedure_scraper.py`
- `pc1.scheduleexceptions` — scheduling-exception rules with flat recurrence-mask columns, populated by `novaRIS_exception_scraper.py`
- `pc1.user_profiles` — referenced by audit FKs (lifecycle TBD)

**Working scrapers**:
- `novaRIS_modalities_scraper.py` — per-facility iteration, runtime facility-id resolution, plain full-page postbacks
- `novaRIS_standardprocedure_scraper.py` — per-modality iteration, two-pass scrape (grid → wizard), parallel detail fetches, ~20-min full run for ~945 procedures
- `novaRIS_exception_scraper.py` — per-facility iteration, two-pass scrape (grid → popup, popup only for recurring rules), plain full-page postbacks, ~2 min/facility for ~8K rows

**Shared infrastructure**:
- `supabase_client.py` — `get_supabase()` factory (12 lines)
- `client_context.py` — `resolve_client_id()` and `add_client_id_arg()` CLI helper (the `get_param`/`get_client_parameters` functions are legacy — read from a `clientparameters` table that doesn't exist in pc1; do NOT use them)
- `novaRIS_common.py` — `login()`, `make_session()`, form/HTML helpers, env-driven `BASE_URL`/`USERNAME`/`PASSWORD`

**Test tenant**: `client_id = 1` (Inview Imaging). Real NovaRIS account
in `.env`. Production data as of session ship:
- `pc1.modalities`: ~scraped modalities across 2 active facilities
- `pc1.proceduresestimate`: 945 procedures
- `pc1.scheduleexceptions`: 16,063 exception rules across 2 facilities (Antioch 7,990 / Inview-Fremont 8,073)

**Active facilities** (`is_client = true` in `pc1.facilities`): Antioch Medical Imaging, Inview-Fremont. The other ~10 facility rows in `pc1.facilities` are inactive contracts retained for historical FK targets.

## 8. Next task: `pc1.machineschedule`

The slot calendar. One row per `(client_id, facility_id, modality_id,
date_and_time)` representing a bookable time slot. The scheduling
engine fills these with orders; the reconciler (next-after-this
deferred item) blocks them based on `pc1.scheduleexceptions`.

### 8.1 Legacy reference (for context only — do NOT port verbatim)

The legacy `machineschedule` table carried at minimum:

- `client_id`, `facility` (text), `modality_machine` (text) → become `facility_id`, `modality_id` FKs in pc1
- `date_and_time` — UTC timestamptz (legacy migrated from facility-local to UTC; pc1 should match)
- `availability` — integer (0 = blocked, 1 = free; the reconciler updates this)
- `exceptions` — `text[]` — display labels like `'LUNCH (H)'`, `'HOLIDAY (S)'`
- `exception_ids` — `text[]` — paired positionally with `exceptions`; each element is a `source_record_key` from `pc1.scheduleexceptions`
- `order_id` / `slot_status` / etc. — for engine-side bookkeeping; scope TBD
- Audit columns (legacy names — **rename in pc1**)

Legacy script: `create_blank_calendar.py` (a generator, not a
scraper — calendar slots are not RIS-derived). Pattern: walk every
active modality, walk every date in the rolling window, walk every
slot in the working day, INSERT.

### 8.2 Expected workflow (two-phase, same as previous tables)

**Phase 1**: schema migration + docs
- New migration: `migrations/0004_create_pc1_machineschedule.sql`
- New doc: `docs/machineschedule.md` (schema reference, slot-arithmetic
  semantics, time-zone handling, common queries, verification queries)
- PR scope: migration + doc only
- Verification: SQL queries in Supabase confirming table shape,
  constraints, indexes, and trigger before merge

**Phase 2**: blank-slot generator + docs
- New script: `create_blank_calendar.py` (or rename for pc1
  consistency — `generate_machineschedule.py` is the leading
  candidate)
- New doc: `docs/machineschedule_generator.md` (or similar)
- PR scope: script + doc
- Verification: end-to-end run for a single facility, sanity SQL
  on the generated rows (one row per slot × machine × day; no
  duplicates; correct time zone)

### 8.3 Things to ask the user before starting Phase 1

Don't design the schema in isolation — confirm these first:

1. **`date_and_time` storage**: UTC (matching legacy post-migration)
   or facility-local? UTC is the standard pc1 convention; recommend UTC
2. **Working-hours source**: where does "this machine is open
   8am-6pm Mon-Fri" live? Options:
   - Inspect existing `pc1.facilities` + `pc1.modalities` schemas
     for unused columns
   - Add new columns to `pc1.modalities` (or a new
     `pc1.modality_hours` table) for per-machine schedules
   - Use a single per-tenant working-day default
3. **Time zone**: needs to be per-facility (Inview-Fremont is
   America/Los_Angeles; future tenants may span TZs). Inspect
   `pc1.facilities` for an existing `timezone` column; add one if
   missing
4. **Slot size resolution**: confirmed in §4.4 — `pc1.clients.slot_size`
   default + `pc1.facilities.slot_size` override. Generator computes
   `required_slots = ceil(slot_window / slot_size)`. **Verify both
   columns exist** (they're referenced in earlier docs but I haven't
   independently confirmed they're in the current pc1.clients /
   pc1.facilities schema)
5. **Rolling window strategy**:
   - How far ahead to pre-generate? 30 days? 90 days? Per-tenant config?
   - Cadence: nightly cron to extend (matches legacy) or on-demand?
   - For pc1's on-demand-first philosophy, recommend on-demand with
     a `--days-ahead=N` flag; the operator runs it weekly
6. **`exceptions` / `exception_ids` columns**: keep as `text[]`
   (legacy) or switch to a JSONB array of `{key, label}` objects?
   The legacy reconciler reads both as paired arrays — keep `text[]`
   to avoid rewriting the reconciler. CHECK constraint that they
   have equal length is impossible in Postgres without a trigger;
   accept the discipline-enforced positional pairing
7. **Initial `availability`**: hardcode `1` (free) at generation
   time? The reconciler will flip Hard-exception slots to `0`
   on its first run. Recommend yes
8. **Idempotency**: re-running the generator for an already-generated
   window should be a no-op (or extend the window only). Use an
   ON CONFLICT clause against `(client_id, facility_id, modality_id,
   date_and_time)` to skip duplicates
9. **Cross-tenant trigger**: yes — same pattern as
   `pc1.proceduresestimate` / `pc1.scheduleexceptions`. Validates
   `client_id` matches `facility_id`'s and `modality_id`'s tenant
10. **Order-side columns** (`order_id`, `slot_status`, etc.): in
    scope for this phase, or defer until Phase 3 (`pc1.orders`)?
    Recommend defer — generate the calendar with just the
    reconciler-relevant columns (availability + exceptions arrays)
    and add order-side columns when the orders work lands

### 8.4 Things to consider for Phase 2 (the generator)

- **Idempotent re-runs**: ON CONFLICT DO NOTHING is the cheapest
  win; lets operators re-run the generator without thinking about
  state
- **Time-zone math**: store UTC but iterate facility-local. The
  `working_hours = 08:00-18:00` is facility-local; the generated
  `date_and_time` is `(facility-local 08:00).astimezone(UTC)`. Zoneinfo
  handles DST automatically when used correctly
- **Batch INSERT**: probably groups of 1000-5000 for fast loads
  (one machine × 90 days × 16-hour day × 4 slots/hour = 5,760 rows;
  small enough to bulk-insert per machine)
- **Don't depend on `pc1.scheduleexceptions`** during generation —
  blank slots are blank by definition. The reconciler is the only
  thing that touches `availability`
- **CLI flags**: `--facility`, `--modality`, `--days-ahead`,
  `--start-date` (default today), `--dry-run`, `--client-id`,
  `--quiet`. Use existing add_client_id_arg helper
- **"Measured performance" section** in the doc with actual numbers
  from the verification run (precedent set in PR #5)

## 9. How to use this file in a new chat

First message to a new Claude Code session in this repo:

> "Please read `docs/session-handoff.md` first to get oriented.
> Then read `docs/<relevant-table>.md` and `docs/<closest-sibling-scraper>.md`
> if we're going to touch schema or a scraper. I want to [your task here]."

That's enough to give the new session full context in <5% of the
context window, leaving plenty of room for the new work.

If the new task is **`pc1.machineschedule`** (the next planned
work), the relevant references are:
- `docs/proceduresestimate.md` and `docs/scheduleexceptions.md` —
  closest sibling table designs; both use the cross-tenant trigger
  and standard audit columns
- `migrations/0002_create_pc1_proceduresestimate.sql` and
  `migrations/0003_create_pc1_scheduleexceptions.sql` — sibling
  migrations; use as templates for shape + trigger + indexes
- `docs/novaris_exception_scraper.md` — the most recently-shipped
  scraper, includes the "Measured performance" precedent
- This file's §4 (conventions) and §8 (next-task notes)

---

*Last refreshed after PRs #4 + #5 landed. If this file is more than
a few months old when you read it, expect drift between it and the
actual codebase — the `git log` and per-table docs are the most
trustworthy sources.*
