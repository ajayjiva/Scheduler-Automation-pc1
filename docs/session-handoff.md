# Session Handoff — Scheduler Automation (pc1)

> **Purpose.** When you start a new Claude session for this repo, point Claude
> at this file as the first step: it captures the project's stable state,
> conventions, the established scraper pattern, your working preferences,
> deferred work, and (when relevant) the current task so we don't burn
> context re-deriving any of it.
>
> The **authoritative** references are:
> - `docs/facilities.md`, `docs/modalities.md`, `docs/proceduresestimate.md` — table schemas
> - `docs/novaris_modalities_scraper.md`, `docs/novaris_standardprocedure_scraper.md` — scraper operations
> - `migrations/*.sql` — the actual schema in Supabase
> - This file — a *navigator* pointing into the above plus session-context
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
tenant today is `client_id = 1` (Inview Imagine — historically `1001`
in the legacy project; renumbered as part of the pc1 cutover).

**Predecessor:** A legacy project against `public.*` tables exists
and was the starting point for many of these patterns. The pc1
codebase is a clean rebuild — not a verbatim port. Conventions have
been standardized and tightened during the rewrite (see §4).

## 2. Repo state

- **Branch in use:** `main` (the only long-lived branch).
- **All feature work** lives in short-lived `feature/<topic>` branches
  → PR → squash-merge to `main` → branch deleted.
- **Recent PRs** (most recent first):
  - **#3 — Add NovaRIS standard-procedure scraper (Phase 2).** Two-pass
    scrape (grid → wizard) into `pc1.proceduresestimate`. Includes a
    pass-1 `__VIEWSTATE`-bloat fix (fresh GET per modality) that
    reduced full runtime from ~196 min to ~15.5 min, the
    `_write_debug_html()` helper that auto-creates `debug/`, and
    deprecation banners on the now-dead `MODALITY_MAP` /
    `FACILITY_MODALITIES` dicts in `novaRIS_common.py`. Verified
    end-to-end with both `--initial-load` and delta modes against
    945 procedures.
  - **#2 — Add pc1.proceduresestimate table (Phase 1: schema + docs).**
    `migrations/0002_create_pc1_proceduresestimate.sql` (table + 4-shape
    override unique index + cross-tenant trigger) plus
    `docs/proceduresestimate.md`.
  - **#1 — Drop FACILITY_MAP, reframe NovaRIS scraper as on-demand.**
    `novaRIS_modalities_scraper.py` now resolves facility IDs at
    runtime by parsing the live NovaRIS facility dropdown and
    matching against `pc1.facilities.facility_name`. Docs reframed
    around the on-demand cadence; nightly-cron language removed.
- **All migrations are applied** in Supabase. Schema matches what's
  in `migrations/` and what's documented in the per-table docs.

## 3. Read these first when picking up

| File | What it covers |
|---|---|
| **This file (`docs/session-handoff.md`)** | Project orientation, conventions, established patterns, working preferences |
| [`docs/facilities.md`](./facilities.md) | `pc1.facilities` schema — the FK target for everything else. Naming-exactness contract with NovaRIS; `is_client` gate semantics |
| [`docs/modalities.md`](./modalities.md) | `pc1.modalities` schema — one row per physical machine. Business key, content_hash composition, lifecycle |
| [`docs/proceduresestimate.md`](./proceduresestimate.md) | `pc1.proceduresestimate` schema — procedure catalog. Four-shape per-machine override design. Cross-tenant trigger semantics |
| [`docs/novaris_modalities_scraper.md`](./novaris_modalities_scraper.md) | The first NovaRIS scraper — the reference for per-facility scrape loops, login flow, runtime facility-id resolution |
| [`docs/novaris_standardprocedure_scraper.md`](./novaris_standardprocedure_scraper.md) | The second NovaRIS scraper — reference for per-modality iteration, two-pass (grid → wizard) scrape, `ThreadPoolExecutor` for parallel detail fetches |

For a task that touches a new schema, read at least the relevant
table doc + the closest sibling scraper doc — that's enough to match
the established pattern.

## 4. Conventions (concise reference)

### 4.1 Schema conventions

Every tenant-scoped table in `pc1` follows this shape:

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PRIMARY KEY | Always. Don't use serial/int4 |
| `client_id` | `bigint NOT NULL` | FK to `pc1.clients(id)`. **No DEFAULT** (multi-tenant footgun) |
| `<entity FKs>` | `bigint NOT NULL` or NULL | E.g. `facility_id`, `modality_id`. NULL allowed when the entity supports a "global" tier (override patterns) |
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

**Cross-tenant safety triggers**: any table with FKs to multiple
tenant-scoped tables should have a `BEFORE INSERT OR UPDATE OF` trigger
validating `client_id` matches across all FK relationships. Pattern:
`pc1.check_<table>_consistency()` function called by a single trigger.

**Override patterns**: tables that need facility/machine overrides
(e.g. `pc1.proceduresestimate`) use **nullable FK columns + a partial
unique index** with `COALESCE(<col>, 0::bigint)` sentinels. PostgREST
upsert can't target a partial index, so writes use per-row UPDATE
plus batched INSERTs.

### 4.2 Migration conventions

- One file per migration, in `migrations/`, numbered like
  `0001_<purpose>.sql`, `0002_<purpose>.sql`, …
- **Idempotent**: `CREATE TABLE IF NOT EXISTS`, `CREATE OR REPLACE
  FUNCTION`, `CREATE INDEX IF NOT EXISTS`, `DROP TRIGGER IF EXISTS`
  before `CREATE TRIGGER`. The migration must be safe to replay.
- Top of file: comment block explaining purpose + shape rationale +
  idempotency promise.
- Applied by hand in the Supabase SQL editor by the user **before
  merging the PR**. PRs that include only migration files wait on
  this verification step.
- Existing migrations:
  - `0001_pc1_modalities_novaris_compat.sql` — relaxes legacy NOT
    NULL constraints, adds `(client_id, facility_name)` unique
  - `0002_create_pc1_proceduresestimate.sql` — full table create +
    4-shape override unique index + cross-tenant trigger
  - **Next available number: 0003**

### 4.3 Scraper conventions

Established by `novaRIS_modalities_scraper.py` and refined by
`novaRIS_standardprocedure_scraper.py`. New scrapers should follow
this pattern.

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
def _fetch_existing_global(supabase, client_id): ...
def write_initial_load(supabase, client_id, records, dry_run): ...
def write_delta(supabase, client_id, records, dry_run): ...
# ─── Driver ──────────────────────────────────────────────────────────────
def scrape(args): ...
def main(): ...
```

**Key patterns to apply**:

| Pattern | Why |
|---|---|
| Fresh GET before each modality/facility POST | Prevents `__VIEWSTATE` bloat across ASP.NET postbacks (cost: 1s/iteration; saves hours on multi-iteration runs) |
| `_table(supabase, "name").schema("pc1")` everywhere | pc1 is not the default schema; PostgREST needs explicit selection |
| Writes set both `created_at` and `updated_at` on INSERT (column DEFAULT only fires on INSERT) | Same value, ensures the column list is uniform across batch INSERTs |
| Writes always update `updated_at` explicitly on UPDATE | DEFAULT only fires on INSERT |
| `created_by` / `updated_by` write NULL for automation; identity goes in `ris_metadata.writer` | Bigint FKs to `user_profiles` aren't meaningful for scripts; user attribution comes when a real user later edits a row |
| `ris_metadata` includes `{writer: WRITER_NAME, ...}` | Trace info for debugging |
| Content hash excludes `client_id`, `is_active`, all timestamps | Re-keying / soft-deletes shouldn't churn hashes |
| Per-row UPDATE for changes, batched INSERTs in groups of 200 | PostgREST upsert can't target partial unique indexes (override patterns use them) |
| Soft-deletes via batched UPDATE in groups of 500 | Keyed list works with `.in_("id", batch)` |
| Scoped to scraper-managed rows on both initial-load wipe and delta fetch (filter `facility_id IS NULL AND modality_id IS NULL AND source_record_key IS NOT NULL` for global-writing scrapers) | Preserves manually-inserted override and app-side rows |
| ThreadPoolExecutor with default 6 workers for per-row detail fetches | NovaRIS is single-threaded behind dialog endpoints; >8 workers triggers timeouts |
| Per-procedure retry with exponential backoff (0.5/1/2s) | Smooths over transient NovaRIS hiccups |
| Debug HTML output via `--save-grid`/`--save-wizard` flags writes to `debug/<file>` (gitignored) | Folder auto-created by `_write_debug_html()` helper |
| Standard CLI flags: `--initial-load`, `--dry-run`, `--modality`/`--facility`, `--limit`, `--workers`, `--quiet`, `--client-id`, `--save-grid` (and `--save-wizard` if two-pass) | Consistent across scrapers |
| Exit codes: 0=clean, 1=login failed, 2=specified entity not found, 3=partial failure (some rows skipped or wizard fetches failed) | Cron-friendly |

**`pc1.clients` for tenant-level config**: scrapers that need per-tenant
defaults (e.g. `slot_size`) read from `pc1.clients` directly; there is
no `clientparameters` table. Facility-specific overrides live on
`pc1.facilities` (e.g. `pc1.facilities.slot_size`). The scheduling
engine resolves the merge: facility row's value wins if non-NULL,
otherwise falls back to client row.

### 4.4 Operational conventions

- **One feature branch per piece of work**, branched from latest `main`
- **Branch naming**: `feature/<short-name>`, `fix/<short-name>`, `docs/<short-name>`
- **Don't push empty branches** — push after first commit
- **Small, focused PRs** — one concern per PR
- **Sequential, not stacked** — merge each PR before starting the next
- **Two-phase pattern for new tables**: Phase 1 = schema migration + table doc; Phase 2 = scraper + scraper doc. Each phase = its own PR
- **SQL verification before merging schema PRs** — user applies the migration in Supabase and runs verification queries; only then does the PR get merged
- **End-to-end verification before merging scraper PRs** — at minimum: smoke test with `--modality=X --limit=5 --dry-run`, then `--initial-load`, then a delta run to confirm `unchanged=N` (proves content_hash determinism)
- **Squash-merge** all PRs. Trim the auto-concatenated squash body to just the PR summary
- **Delete branch** locally and remotely after merge:
  ```powershell
  git checkout main
  git pull --ff-only origin main
  git branch -D feature/<name>
  git fetch --prune origin
  ```

### 4.5 Debug folder

- `debug/` is in `.gitignore` (whole folder ignored)
- Scrapers' `--save-grid` / `--save-wizard` flags should default users
  toward `debug/<file>.html` (recommended in `--help` text)
- The `_write_debug_html()` helper auto-creates `debug/` if it
  doesn't exist (fresh clones don't have it)
- Treat anything in `debug/` as throwaway — never commit, never rely on

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

Learned during the standard-procedure scraper work. Apply to all
subsequent sessions.

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
- "Run a dry-run first" is the default — except when the user
  explicitly says skip it (e.g., "no production data, just run it")

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

| Item | Status | Pointer |
|---|---|---|
| Delete `MODALITY_MAP` + `FACILITY_MODALITIES` from `novaRIS_common.py` | **Deferred** — marked DEPRECATED in PR #3 | `novaRIS_common.py:73-95`. Final consolidation pass |
| Port `pc1.scheduleexceptions` from legacy `scheduleexceptions` | **NEXT TASK** — see §8 | Legacy spec lives in the legacy data-model.md (file uploaded in prior session if needed) |
| Port `pc1.machineschedule` (the slot calendar) | **Deferred** | Legacy used `create_blank_calendar.py` + nightly rolling-window extension. PC1 equivalent will need rethinking around the new FK shapes |
| Port `pc1.orders` | **Deferred** | Source for orders was outside the legacy scrapers' scope. May or may not need a NovaRIS scraper |
| Port scheduling engine (`main.py` patient-options builder) | **Deferred** | Core business logic; depends on `pc1.machineschedule` + `pc1.proceduresestimate` + `pc1.orders` being populated |
| `pc1.user_profiles` is referenced by every audit FK but its lifecycle isn't documented | **Open question** | Future: how does a row get into `pc1.user_profiles`? Login UI? Manual SQL? |
| Cross-tenant FK validation triggers on `pc1.modalities` | Already exists (`trg_modality_facility_client_check`) | Mentioned for pattern-matching when designing new triggers |

## 7. Project Today (snapshot)

**Tables in pc1 schema**:
- `pc1.clients` — tenant identity + global per-tenant defaults (`slot_size`, etc.)
- `pc1.facilities` — per-tenant facility list with `is_client` filter and per-facility overrides
- `pc1.modalities` — per-facility machine inventory, populated by `novaRIS_modalities_scraper.py`
- `pc1.proceduresestimate` — procedure catalog with 4-shape override design, populated by `novaRIS_standardprocedure_scraper.py`
- `pc1.user_profiles` — referenced by audit FKs (lifecycle TBD)

**Working scrapers**:
- `novaRIS_modalities_scraper.py` — per-facility iteration, runtime facility-id resolution
- `novaRIS_standardprocedure_scraper.py` — per-modality iteration, two-pass scrape, parallel detail fetches

**Shared infrastructure**:
- `supabase_client.py` — `get_supabase()` factory (12 lines)
- `client_context.py` — `resolve_client_id()` and `add_client_id_arg()` CLI helper (the `get_param`/`get_client_parameters` functions are legacy — read from a `clientparameters` table that doesn't exist in pc1; do NOT use them)
- `novaRIS_common.py` — `login()`, `make_session()`, form/HTML helpers, env-driven `BASE_URL`/`USERNAME`/`PASSWORD`

**Test tenant**: `client_id = 1` (Inview Imagine). Real NovaRIS account
in `.env`. Production data: 945 procedures in `pc1.proceduresestimate`
+ scraped modalities in `pc1.modalities`.

## 8. Next task: `pc1.scheduleexceptions`

The Modality Scheduling UI in NovaRIS represents recurring/one-off
exception rules (LUNCH, holidays, machine downtime, technician
out-of-office, etc.). Each rule blocks or annotates one or more
slots in the calendar. The legacy table was `scheduleexceptions` in
the public schema; the pc1 version will follow the same shape but
adapted to the pc1 conventions (§4).

### 8.1 Legacy reference (for context only — do NOT port verbatim)

The legacy `scheduleexceptions` table carried:

- `client_id` — tenant
- `facility` (text) — facility scope **→ becomes `facility_id bigint` FK in pc1**
- `modality_machine` (text) — machine the rule applies to **→ becomes `modality_id bigint` FK in pc1**
- `description` — free-text label (e.g. "LUNCH")
- `start_date` / `start_time` / `end_date` / `end_time` — rule window
- `recurrence` — `None` / `Daily` / `Weekly` / `Monthly`
- `type` — `Hard` (blocks slot, `availability=0`) / `Soft` (display-only)
- `repeat_every` — currently unused; **drop in pc1**
- `weekdays_only` — for `Daily` recurrence, Mon-Fri only
- `is_sunday` … `is_saturday` — for `Weekly` recurrence (day-of-week mask)
- `day_1` … `day_31` — for `Monthly` recurrence (day-of-month mask)
- `is_active` — soft-delete flag
- `source_record_key` — NovaRIS-side ID
- `content_hash` — for delta sync
- Audit columns (legacy names — **rename in pc1**)

Legacy NovaRIS volume: ~35,000 records across all facilities. Daily
cadence — much larger and more frequent sync than `proceduresestimate`.

### 8.2 Expected workflow (two-phase, same as Phase 1+2 for proceduresestimate)

**Phase 1**: schema migration + docs
- New migration: `migrations/0003_create_pc1_scheduleexceptions.sql`
- New doc: `docs/scheduleexceptions.md` (schema reference, lifecycle,
  recurrence semantics, common queries)
- PR scope: migration + doc only
- Verification: SQL queries in Supabase confirming table shape,
  constraints, and trigger before merge

**Phase 2**: scraper + docs
- New scraper: `novaRIS_exception_scraper.py` (or similar — match
  the legacy naming if possible)
- New doc: `docs/novaris_exception_scraper.md`
- PR scope: scraper + doc
- Verification: end-to-end run against real NovaRIS, then delta to
  confirm `unchanged=N`

### 8.3 Things to ask the user before starting Phase 1

Don't design the schema in isolation — confirm these first:

1. **Schema location**: same as `pc1.proceduresestimate` — `pc1.scheduleexceptions`. Verify the table doesn't already exist in pc1 from prior work
2. **FK choices**:
   - `facility_id` → `pc1.facilities(id)` (NOT NULL or nullable for "all facilities" rules?)
   - `modality_id` → `pc1.modalities(id)` (NOT NULL or nullable for "all machines" rules?)
   - The legacy used text columns; pc1 should match the proceduresestimate pattern of real FKs
3. **Recurrence semantics**: keep all four types (None/Daily/Weekly/Monthly) and all day-mask columns? Or simplify by storing recurrence as a JSONB column instead of 31 day-of-month flags? (User's call — recommend keeping flat columns to match the legacy reader logic, but flag the option)
4. **Type column (`Hard`/`Soft`)**: keep verbatim? Use a CHECK constraint to restrict values?
5. **`repeat_every`**: legacy says unused — drop it. Confirm
6. **`source_record_key` uniqueness**: legacy spec says "Not referenced from any other table." Use the same (client_id, facility_id, source_record_key) pattern as proceduresestimate, or simpler `(client_id, source_record_key)`?
7. **Cross-tenant trigger**: this is a multi-FK table (facility_id, modality_id) — apply the same `check_<table>_consistency()` pattern from proceduresestimate?
8. **Cadence**: legacy ran daily; will the pc1 scraper match that?
   Or on-demand like the procedures scraper? The 35k-row volume might
   change the answer

### 8.4 Things to consider for Phase 2 (the scraper)

- Source page: probably `ViewScheduleExceptions.aspx` or similar
  in NovaRIS — verify with user before writing
- Iteration shape: per-facility (like modalities) or per-modality (like procedures) or some combination?
- Two-pass vs. one-pass: does the NovaRIS UI expose all fields in
  the grid, or is a per-row detail dialog needed?
- Volume implication: 35k rows means pass-2 (if needed) would take
  much longer than 945 procedures. Plan workers + retries
  accordingly
- Recurrence-mask serialization: the legacy stored 7 boolean columns
  for day-of-week and 31 for day-of-month. Scraping them out of
  NovaRIS HTML and writing them back in this shape is straightforward
  but verbose. Worth eyeballing the NovaRIS UI shape before writing
  the parser

## 9. How to use this file in a new chat

First message to a new Claude Code session in this repo:

> "Please read `docs/session-handoff.md` first to get oriented.
> Then read `docs/<relevant-table>.md` and `docs/<closest-sibling-scraper>.md`
> if we're going to touch schema or a scraper. I want to [your task here]."

That's enough to give the new session full context in <5% of the
context window, leaving plenty of room for the new work.

If the new task is the **`pc1.scheduleexceptions` scraper**, the
relevant references are:
- `docs/proceduresestimate.md` + `docs/novaris_standardprocedure_scraper.md` — closest siblings (both written following the standardized pattern); use as templates
- `migrations/0002_create_pc1_proceduresestimate.sql` — closest sibling migration; use as template
- This file's §4 (conventions) and §8 (next-task notes)

---

*Last refreshed: end of the session that landed PRs #1–#3. If this
file is more than a few months old when you read it, expect drift
between it and the actual codebase — the `git log` and per-table
docs are the most trustworthy sources.*
