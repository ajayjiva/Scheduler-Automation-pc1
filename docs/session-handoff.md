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
| [`docs/reconcile_exceptions.md`](./reconcile_exceptions.md) | `reconcile_exceptions.py` — Phase 3 writer. Reads active `pc1.scheduleexceptions` rules and overlays them onto `pc1.machineschedule` (`exceptions[]` / `exception_ids[]` / `availability`). Modality matched by FK. Availability rule: in-hours slots `=0` if any Hard OR `order_id IS NOT NULL`; out-of-hours untouched (generator owns). Booked-slot Hard-exception conflict surfaces `CONFLICTS` warning + exit code 4 |
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
| 1 | Port **`pc1.machineschedule`** — the slot calendar | **DONE** — schema migration `0004` (PR #7), generator `generate_machineschedule.py` (PR #8), migration `0005` (column rename to `date_and_time_utc`, seq, business-hours availability), all merged | Generator emits 24-hour grid; `availability=1` in business hours, `0` outside |
| 2 | Port **`reconcile_exceptions.py`** | **DONE** — `reconcile_exceptions.py` + `docs/reconcile_exceptions.md` merged in PR #9. Reads active `pc1.scheduleexceptions` (modality FK match), overlays onto `pc1.machineschedule`. Availability rule: in-hours slots get `0` if any Hard exception OR `order_id IS NOT NULL`; out-of-hours slots untouched (generator owns them). Booked-slot Hard conflict surfaces a `CONFLICTS` warning + exit code 4 — `order_id` blocker rule lets the Phase 4 booking workflow coexist without write-ordering ceremony | Idempotent re-runs (no DB writes when desired state already matches). Default iterates all `is_client=true` facilities; `--facility=NAME` for one. Parallelism / batching ported verbatim from legacy. Past slots never modified (`range_start = max(--start-date, today)`) |
| 3.5 | **`pc1.orders_v` compatibility-layer view** (+ plain columns on `pc1.orders` / `pc1.patients`) | **IN PROGRESS** — migrations `0006`–`0009` + `docs/orders.md`/`orders_v.md` on branch `claude/trusting-galileo-B97ad`; test-order seed pending | `pc1.orders` already existed (RIS-shaped) so `0008` ALTERs it (not a stub create). View: multi-row per order, FK columns, `pe_facility_id` for the resolver, tenant+active scoping. `ris_*` (raw) vs plain (internal) column convention. Decouples engine-port work (3.6) from the team's still-evolving orders schema |
| 3.6 | Port the **scheduling engine** (`main.py` + helpers) | Deferred — depends on (3.5) | Reads from `pc1.orders_v` (the compatibility-layer view from 3.5). Same engine code regardless of the underlying orders table. Legacy reference: `main.py`, `get_orders.py`, `per_machine_resolver.py`, `first_modalitytype_scheduler.py`, `all_modality_scheduler.py`, `next_modalitytype_scheduler.py`, `cumulative_open_slots.py`, `resource_scheduler.py` |
| 4 | **Real orders schema** (team-driven) | Deferred — design in flux | When the team settles the orders schema, single migration ALTERs `pc1.orders` + replaces `pc1.orders_v`. Engine code from 3.6 keeps working because the view's column contract stays the same. |
| — | Delete `MODALITY_MAP` + `FACILITY_MODALITIES` from `novaRIS_common.py` | Deferred — marked DEPRECATED in PR #3 | Final consolidation pass |
| — | `pc1.user_profiles` lifecycle | Open question | Referenced by every audit FK but how does a row get into it? Login UI? Manual SQL? |

## 7. Project Today (snapshot)

**Tables in pc1 schema**:
- `pc1.clients` — tenant identity + global per-tenant defaults (`slot_size`, `opening_time` / `closing_time`, `advance_booking_days`, `timezone`, etc.)
- `pc1.facilities` — per-tenant facility list with `is_client` gate and per-facility overrides (all the same parameter columns)
- `pc1.modalities` — per-facility machine inventory, populated by `novaRIS_modalities_scraper.py`
- `pc1.proceduresestimate` — procedure catalog with 4-shape override design, populated by `novaRIS_standardprocedure_scraper.py`
- `pc1.scheduleexceptions` — scheduling-exception rules with flat recurrence-mask columns, populated by `novaRIS_exception_scraper.py`
- **`pc1.machineschedule` — slot calendar.** Generator-produced (not RIS-sourced); UTC `date_and_time_utc`; facility-local `start_time`/`end_time`/`seq` (the `YYYYMMDDHHMMSS` engine-contract integer); paired `exceptions[]` / `exception_ids[]` arrays; `order_id` column present with FK to `pc1.orders` **deferred** to Phase 4. See [`docs/machineschedule.md`](./machineschedule.md).
- `pc1.user_profiles` — referenced by audit FKs (lifecycle TBD)

**Working scrapers**:
- `novaRIS_modalities_scraper.py` — per-facility iteration, runtime facility-id resolution, plain full-page postbacks
- `novaRIS_standardprocedure_scraper.py` — per-modality iteration, two-pass scrape (grid → wizard), parallel detail fetches, ~20-min full run for ~945 procedures
- `novaRIS_exception_scraper.py` — per-facility iteration, two-pass scrape (grid → popup, popup only for recurring rules), plain full-page postbacks, ~2 min/facility for ~8K rows

**Working calendar writers** (Phase 1–3 outputs):
- `generate_machineschedule.py` — blank-slot generator. Default iterates `is_client=true` facilities. 24-hour grid; `availability=1` in business hours, `0` outside. ON CONFLICT DO NOTHING idempotency. ~1,300 rows/sec.
- `reconcile_exceptions.py` — exception overlay writer. Default iterates `is_client=true` facilities. In-hours `availability=0` when any Hard exception OR `order_id IS NOT NULL`; out-of-hours untouched. CONFLICTS warning + exit code 4 for booked-vs-Hard. ~14 s idempotent re-run per facility.

**Shared infrastructure**:
- `supabase_client.py` — `get_supabase()` factory (12 lines)
- `client_context.py` — `resolve_client_id()` and `add_client_id_arg()` CLI helper (the `get_param`/`get_client_parameters` functions are legacy — read from a `clientparameters` table that doesn't exist in pc1; do NOT use them)
- `novaRIS_common.py` — `login()`, `make_session()`, form/HTML helpers, env-driven `BASE_URL`/`USERNAME`/`PASSWORD`

**Test tenant**: `client_id = 1` (Inview Imaging). Real NovaRIS account
in `.env`. Live data as of last refresh (Phase 3 verification + daily-ops run):
- `pc1.modalities`: scraped modalities across the 2 active facilities (Antioch + Inview-Fremont)
- `pc1.proceduresestimate`: 945 procedures
- `pc1.scheduleexceptions`: ~16,000 exception rules across the 2 active facilities
- `pc1.machineschedule`: ~50,000+ slots covering today → today + ~30 days; `exceptions[]` / `exception_ids[]` / `availability` reconciled and current
- `pc1.orders`: **already exists** as a RIS-shaped table (`ris_order_id`, `ris_order_status`, `ris_order_type`, `ris_requesting_date`, … + sync quintet). Phase 3.5 ALTERs it to add the plain internal columns the view reads (`procedure_description` + `ris_procedure_description`, `order_status`, `order_type`, `requesting_date`, `preferred_language`, `is_active`) and a cross-tenant trigger. See [`docs/orders.md`](./orders.md). Test orders not yet seeded.

**Active facilities** (`is_client = true` in `pc1.facilities`): Antioch Medical Imaging, Inview-Fremont. The other ~10 facility rows in `pc1.facilities` are inactive contracts retained for historical FK targets.

**Routine daily-ops command sequence** (operator-driven; no cron):
```powershell
python novaRIS_exception_scraper.py            # delta refresh from NovaRIS
python generate_machineschedule.py --days-ahead=30   # extend rolling horizon
python reconcile_exceptions.py --days-ahead=30       # overlay current exceptions
```

## 8. Next task: orders compatibility layer (Phase 3.5)

The team is **still designing the orders table schema** and won't
land it for a while. To stay on schedule, we're decoupling the
engine port (3.6) from the orders schema decision (Phase 4) by
introducing a `pc1.orders_v` view as a **compatibility layer**.

Strategy: define a stable column contract on `pc1.orders_v` now,
back it with a stub `pc1.orders` table + hand-crafted test data,
port the engine against the view. When the team finalizes the real
orders schema, only the view definition changes. Engine code stays
identical.

### 8.1 Why a view, not direct table reads

Postgres regular VIEWs are query-rewrite rules — there is **no
performance overhead** vs writing the joins inline as long as:
- The view doesn't use `DISTINCT ON` or aggregations
- Underlying join columns are indexed
- The view exposes columns the planner can push WHERE clauses
  against

A 4–5-join view of `orders` + `proceduresestimate` + `facilities` +
`modalities` runs the same plan as if `main.py` wrote the joins
itself. The user explicitly asked about this — the discussion is
in the session that produced this handoff. Short answer:
**non-materialized view, performance is comparable to direct joins.**

The user wants this guarantee maintained — don't switch to
MATERIALIZED VIEW or function-based views without re-discussing.

### 8.2 Design decisions already locked in

These were agreed during the session that produced this handoff.
Don't re-litigate; just implement.

| # | Decision | Rationale |
|---|---|---|
| 1 | **Sub-phase split**: 3.5 (orders compat layer) → 3.6 (engine port reading the view) → Phase 4 (team's real orders schema replaces the underlying tables; view contract stays) | Decouples work; engine port is testable without real orders |
| 2 | **`pc1.orders_v` returns multiple rows per order** — one row per matching `pc1.proceduresestimate` entry (no `DISTINCT ON`). Precedence resolved in Python by `per_machine_resolver.py`. Matches the final legacy shape (`orders_v_per_machine_rows.sql`) | Engine needs per-machine candidate rows; per_machine_resolver expects multi-row input |
| 3 | **FK columns over text names** for facility / modality joins. Expose `facility_id`, `modality_id` instead of legacy `facility` / `module` text. Engine port will use FK joins. | pc1's FK convention; avoids text-spelling drift |
| 4 | **Keep `procedure_description` (text)** in the view. The scraper brings descriptions over, not IDs — display layer wants the text. | User-requested |
| 5 | **Defer `stat_order`, `machine_skill`, `contrast_skill`** for now. Not used in the current scheduling logic. Add columns to the view when the engine logic that needs them lands. | User-requested — don't pay for unused contract surface |
| 6 | **Test data is hand-crafted ~10 patients**, inserted directly into `pc1.orders` (single-modality, multi-modality, per-facility override, per-machine override, multi-CPT). **Patient rows insert jointly** — there isn't a `pc1.patients` table yet; we'll create whatever's needed when the engine port starts | User-requested; lets engine port proceed without real patient data |
| 7 | **The user will provide draft view DDL** at the start of the next session. Don't draft it yourself — they have a specific shape in mind | Captured from user directly |

### 8.3 Where the engine-side column requirements come from

The legacy main.py + helpers expect these columns on each `orders_v`
row (from the 3,025-line bundle reviewed during Phase 3 design):

| Column | Source (in legacy) | Used by |
|---|---|---|
| `order_id` | `orders.id` | dedup, conflict reporting in `per_machine_resolver.py` |
| `client_id` | `orders.client_id` | tenant scope filter |
| `patient_id` | `orders.patient_id` | top-level engine input filter |
| `facility_id` ← (pc1 change from legacy `facility` text) | `orders.facility_id` | matching against pe rows in `per_machine_resolver._row_tier()` |
| `modality_id` ← (pc1 change from legacy `module` text) | `proceduresestimate.modality_id` | per-machine override resolution |
| `pe_facility_id` ← (pc1 change from legacy `pe_facility` text) | `proceduresestimate.facility_id` | per-facility override resolution |
| `procedure_code` | `proceduresestimate.procedure_code[]` | order-to-procedure JOIN: `o.procedure_code = ANY(pe.procedure_code)` |
| `procedure_description` | `proceduresestimate.procedure_desc` | display + transitional safety-net JOIN |
| `required_slots` | `proceduresestimate.required_slots` | scheduler block sizing |
| `modality_type` | `proceduresestimate.modality_type` | engine modality grouping |
| **DEFERRED:** `stat_order`, `machine_skill`, `contrast_skill` | n/a yet | Add when engine logic uses them |

When you start the next session, **the very first thing** is to
ask the user for the view DDL they had in mind. Don't infer — they
explicitly said they'd provide it. Once you have it, cross-check
against this column list and flag any gaps.

### 8.4 Stub `pc1.orders` shape (proposal — confirm with user)

> **SUPERSEDED during implementation.** `pc1.orders` already existed as a
> RIS-shaped table, so we did not create a stub — `0008` ALTERs the existing
> table to add the plain internal columns. The strawman below is kept only as a
> record of the original plan; see [`docs/orders.md`](./orders.md) for the
> actual shape.

The view needs an underlying table. Until the team's real schema
lands, we make a stub:

```sql
-- Stub. Minimal columns to support orders_v. Will be replaced when
-- the team's real orders schema lands; orders_v's contract stays.
CREATE TABLE pc1.orders (
    id                bigserial    PRIMARY KEY,
    client_id         bigint       NOT NULL REFERENCES pc1.clients(id),
    patient_id        bigint       NOT NULL,
    facility_id       bigint       NOT NULL REFERENCES pc1.facilities(id),
    procedure_code    text         NOT NULL,        -- single CPT; view does the array containment
    procedure_description text     NULL,            -- denormalized for the engine display
    is_active         boolean      NOT NULL DEFAULT true,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now(),
    created_by        bigint       NULL REFERENCES pc1.user_profiles(id) ON DELETE SET NULL,
    updated_by        bigint       NULL REFERENCES pc1.user_profiles(id) ON DELETE SET NULL
    -- Cross-tenant trigger on client_id / facility_id (matches sibling tables)
);
```

This is a **strawman** — the next session should confirm with the
user before applying. The view definition (user-provided) will tell
us if any additional source columns are required.

### 8.5 What the next session should NOT do

- Don't try to anticipate the team's real orders schema. They're
  still discussing. Whatever we build for 3.5 is **explicitly stub**.
- Don't add columns to `orders_v` for `stat_order` / `machine_skill`
  / `contrast_skill`. User explicitly deferred them.
- Don't `DISTINCT ON` collapse the view. Multi-row shape.
- Don't bake the FK to `pc1.orders` on `pc1.machineschedule.order_id`
  yet — that's part of the real Phase 4. The column exists, the FK
  constraint is deferred.
- Don't propose a MATERIALIZED VIEW or function-based view. Regular
  VIEW only.

### 8.6 Suggested work order for Phase 3.5

1. **Start the session by asking** the user for the `orders_v` DDL
   they have in mind. Read it.
2. **Inventory main.py / get_orders.py / per_machine_resolver.py**
   for every `orders_v` row column reference. Compare against the
   user's DDL. Flag any gaps.
3. **Draft the stub `pc1.orders` table migration** that supports
   the user's view. Use the §8.4 strawman as starting point; adjust
   based on their view's required source columns.
4. **Draft the `pc1.orders_v` migration** (CREATE VIEW). User's DDL
   is the input; we just package it as a numbered migration with
   an `orders_v_compat_layer` doc.
5. **Draft `docs/orders.md` + `docs/orders_v.md`** — schema docs
   following the existing conventions. Explicitly mark both as
   "stub for Phase 3.5; will be replaced when Phase 4 lands."
6. **Hand-craft test data** — ~10 patient orders covering the
   scenarios in §8.2 decision #6. Insert via a separate
   `migrations/000X_seed_test_orders.sql` (NOT part of the prod
   migration sequence; clearly labeled).
7. **Verification:** SQL queries that exercise the view from each
   covered shape. Document expected row counts per patient.
8. **PR.** Schema + docs + seed data in one PR. Step-by-step
   verification + merge pattern same as PRs #7, #8, #9.

Phase 3.6 (engine port) is a separate PR. Even larger surface; do
NOT bundle.

### 8.7 Legacy reference files (in the 3,025-line bundle)

Read these to inform the column contract and engine behavior:

| Legacy file | Why it matters |
|---|---|
| `main.py` | Top-level: pulls orders via `get_summary_list()`, builds combination matrix, calls scheduler, prints options |
| `get_orders.py` | Wraps the `orders_v` SELECT, returns `(summary_list, rows, facility)` tuple |
| `per_machine_resolver.py` | The 4-tier precedence logic (`facility,machine` → `facility,NULL` → `NULL,machine` → `NULL,NULL`). Pure functions; readable |
| `first_modalitytype_scheduler.py` | Computes eligible anchor-block slot rows; reads `calc_cumm_below`, `availability`, `modality_machine` |
| `all_modality_scheduler.py` | Builds full chains; key contract: `str(seq)[:8]` = facility-local YYYYMMDD. **pc1 honors this** — see [`docs/machineschedule.md` → seq engine contract](./machineschedule.md#the-seq-engine-contract) |
| `next_modalitytype_scheduler.py` | Adjacent-block search; same seq[:8] contract |
| `cumulative_open_slots.py` | Walks consecutive availability=1 rows; group-by `(modality_machine, facility-local-date)` |
| `resource_scheduler.py` | Resource availability check; honored only when tenant has `use_technician_calendar=true` (Inview Imaging has it false; pc1's `use_technician_calendar` column exists on both clients + facilities) |
| `option_filters.py` | Dedup + Pareto-prune on the option output |
| `tz_helpers.py` | `resolve_facility_tz(cp)`, `slot_local_dt(row, facility_tz)`, etc. Reconciler ported the helpers inline — see `reconcile_exceptions.py` for the pattern |

The bundle's main.py uses an old `clientparameters` table that
**does not exist in pc1**. Read `client_context.py` in the current
repo for what to use instead (read directly from `pc1.clients` and
`pc1.facilities`).

### 8.8 Critical contract pc1 already honors

The Phase 1-3 work in pc1 set up two key invariants the engine
relies on. Don't break them:

1. **`pc1.machineschedule.seq` = facility-local YYYYMMDDHHMMSS
   bigint**. Engine extracts the local date via `str(seq)[:8]`.
   Documented in [`docs/machineschedule.md` → seq engine contract](./machineschedule.md#the-seq-engine-contract).
2. **`pc1.machineschedule.start_time` / `end_time` are facility-
   local clock times.** Engine displays them directly to patients
   without UTC conversion. `date_and_time_utc` is the UTC instant
   for query filtering; `start_time` is the local human-facing time.
3. **`pc1.machineschedule.availability = 0` when:** (a) out-of-hours
   (generator-owned) OR (b) in-hours + Hard exception (reconciler-
   owned) OR (c) in-hours + `order_id IS NOT NULL` (reconciler-
   owned). Phase 4 booking workflow writes `(order_id=N,
   availability=0)` atomically. See
   [`docs/reconcile_exceptions.md` → Availability rule](./reconcile_exceptions.md#availability-rule).

## 9. How to use this file in a new chat

First message to a new Claude Code session in this repo:

> "Please read `docs/session-handoff.md` first to get oriented.
> Then read `docs/<relevant-table>.md` and `docs/<closest-sibling-scraper>.md`
> if we're going to touch schema or a scraper. I want to [your task here]."

That's enough to give the new session full context in <5% of the
context window, leaving plenty of room for the new work.

If the new task is the **orders compatibility layer (Phase 3.5)**
— the current next planned work — the relevant references are:
- This file's §8 (full design context + decisions already locked
  in + suggested work order)
- `docs/proceduresestimate.md` — the catalog table the view will
  JOIN against (4-shape override row design)
- `migrations/0002_create_pc1_proceduresestimate.sql` and
  `migrations/0003_create_pc1_scheduleexceptions.sql` — sibling
  migrations; use as templates for the new `pc1.orders` stub
  (cross-tenant trigger, audit columns, source-tracking-quintet
  pattern)
- `docs/machineschedule.md` — defines the `seq` engine contract,
  paired-array invariant, and the order_id-as-blocker rule that
  Phase 4 booking workflow depends on
- `docs/reconcile_exceptions.md` — defines the availability rule
  (in-hours = 0 if Hard exception OR order_id NOT NULL); the
  engine port reads slots subject to this rule
- This file's §4 (conventions) — schema shape, scraper patterns,
  audit columns, cross-tenant trigger pattern

**At the start of the next session**, ask the user for the draft
`pc1.orders_v` DDL — they explicitly said they'd provide it. Don't
draft the view yourself before getting their input.

---

*Last refreshed after PR #9 landed (Phase 3 reconcile_exceptions.py
+ data refresh via daily-ops command sequence). If this file is
more than a few months old when you read it, expect drift between
it and the actual codebase — the `git log` and per-table docs are
the most trustworthy sources.*
