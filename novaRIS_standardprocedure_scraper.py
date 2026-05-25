"""
novaRIS_standardprocedure_scraper.py

Scrapes the standard-procedures catalog from NovaRIS into the
Supabase `pc1.proceduresestimate` table.

Why two passes
--------------
NovaRIS surfaces a procedure's data across two pages:

    Pass 1 — ViewStandardProcedures.aspx (the grid).
             Filtered by Modality Type via a dropdown. Each <tr>
             carries setActionSource('<procedureID>', '<rowIdx>') plus
             4 visible cells: ID, Required Time (minutes), Modality
             Type, Procedure Name.

    Pass 2 — StandardProcedureWizard.aspx?type=dialog
             &standardProcedureID=<id> (the per-procedure detail dialog).
             Re-confirms modality_type + required_time and adds three
             fields the grid omits: anatomical_area,
             exam_prep_instructions, exam_prep_requires_prompt.

Pass 2 is the expensive one — one HTTP round-trip per procedure —
so the scraper runs it in a ThreadPoolExecutor with a small worker
count. NovaRIS exposes ~955 procedures for Fremont alone, so the
total wall-clock for a full run is ~15 minutes (the bulk of that is
network I/O against NovaRIS, not Supabase).

Schema mapping (pc1.proceduresestimate)
---------------------------------------
The scraper writes **global** rows only — `facility_id` and
`modality_id` are always NULL. Per-facility / per-machine override
rows are inserted manually (or by a future override-management tool)
and the scraper leaves them alone.

    client_id           ← --client-id (resolve_client_id)
    facility_id         ← NULL  (global row — scraper never writes overrides)
    modality_id         ← NULL  (global row — scraper never writes overrides)
    ris_system          ← 'NovaRIS' (hardcoded)
    ris_sync_status     ← 'synced' (hardcoded — set on every write)
    modality_type       ← wizard `modalityTypeDD` selected option (falls back to grid cell)
    procedure_code      ← parse_procedure_codes(procedure_desc) — text[]
    procedure_desc      ← wizard `procedureName` (falls back to grid cell)
    required_time       ← wizard `requiredTime` (falls back to grid cell)
    required_slots      ← ceil(required_time / slot_size)  where
                          slot_size = pc1.clients.slot_size (per-tenant
                          global default, fallback 15)
    anatomical_area     ← wizard `anatomicalAreaDD` selected option text
    exam_prep_instructions ← wizard `examPrepInstructions` textarea
    exam_prep_requires_prompt ← wizard `requiredField` checkbox
    source_record_key   ← procedure ID from setActionSource(...)
    content_hash        ← SHA-256 of business fields (see HASHED_FIELDS)
    ris_metadata        ← {writer, modality_dropdown_value, ...} — trace info
    is_active           ← True for scraped rows; flipped False on soft-delete
    created_by / updated_by ← NULL (bigint FK to user_profiles — scraper has no user)
    created_at          ← set on insert; preserved on update
    updated_at          ← now() on every write
    synced_at           ← now() on every write
    ris_last_synced_at  ← now() on every write

Modes
-----
    --initial-load   Wipe **only scraper-managed global rows** for this
                     client (rows where facility_id IS NULL AND
                     modality_id IS NULL AND source_record_key IS NOT
                     NULL), then bulk insert. Manually-inserted
                     override rows are untouched. Use once when first
                     onboarding a tenant.
    (default)        Delta sync:
                       * one paginated SELECT of existing global rows
                       * skip rows whose content_hash matches (no write)
                       * INSERT new rows in batches
                       * UPDATE changed rows per-row (PostgREST upsert
                         can't target a partial unique index)
                       * UPDATE soft-deletes for keys missing from the
                         scrape

Flags
-----
    --dry-run             Parse only; no Supabase writes.
    --modality=NAME       Limit to a single Modality Type (e.g. 'US').
    --limit=N             At most N procedures per modality (testing).
    --workers=N           Parallel wizard fetches (default 6).
    --quiet               Suppress per-row chatter.
    --client-id=NNNN      Override the active tenant.
    --save-grid=FILE      Save the first grid HTML response (debugging).
    --save-wizard=FILE    Save the first wizard HTML response (debugging).

Required .env
-------------
    NOVARISURL, NOVARISUSER, NOVARISPASSWORD, SUPABASE_URL, SUPABASE_KEY
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from supabase_client import get_supabase
from client_context import add_client_id_arg, resolve_client_id
from novaRIS_common import (
    BASE_URL,
    USERNAME,
    extract_all_form_fields,
    extract_form_state_from_html,
    login,
    make_session,
)

VIEW_PROCEDURES_URL = f"{BASE_URL}/ViewStandardProcedures.aspx"
WIZARD_URL          = f"{BASE_URL}/StandardProcedureWizard.aspx"

# Identifier recorded in ris_metadata.writer for traceability.
WRITER_NAME = "novaRIS_standardprocedure_scraper.py"

# Source RIS value written to the ris_system column.
RIS_SYSTEM = "NovaRIS"

# Schema-qualified Supabase access — pc1 schema must be exposed via
# PostgREST `db-schemas` for these calls to reach the DB.
PC1_SCHEMA = "pc1"

# Fallback used when pc1.clients.slot_size is NULL or the column is
# missing. The scraper writes one global proceduresestimate row per
# procedure, so it needs a single per-tenant slot_size to compute
# required_slots. Facility-specific overrides would require facility-
# specific rows, which is a separate (manually-curated) workflow.
DEFAULT_SLOT_MINUTES = 15

# Grid <tr> onclick callback: setActionSource('<procedureID>', '<rowIdx>')
SET_ACTION_RE = re.compile(
    r"setActionSource\(\s*['\"](\d+)['\"]\s*,\s*['\"]\d+['\"]\s*\)"
)

# Primary CPT-code format: trailing parens like "(12345,67890)" or
# "(76705)" or "(73719,A9579)". HCPCS letter+4digit codes (A9579) coexist
# with 5-digit CPT codes (76705) in the same parenthesized list.
PROCEDURE_CODE_RE = re.compile(r"\(([\dA-Za-z,\s]+)\)\s*$")

# Fallback for procedure names that have CPT codes at the end WITHOUT
# parentheses. Seen in NM/INTERVENTIONAL/US_TATE families. Examples:
#   "T-Left Breast Complete US 76641"
#   "I--PARACENTESIS 49080"
#   "NM--BONE SCAN - 3 PHASE 78315"
#   "T-Carotid Bilateral US 93880, 76536"
# The end-of-string anchor + strict 5-digit (or HCPCS A9579-style) token
# pattern prevents false positives like "1 VIEW" or "3D".
PROCEDURE_CODE_FALLBACK_RE = re.compile(
    r"((?:\b(?:[A-Z]\d{4}|\d{5})\b[,\s]*)+)\s*$"
)
PROCEDURE_CODE_TOKEN_RE = re.compile(r"\b(?:[A-Z]\d{4}|\d{5})\b")

_QUIET = False


def vprint(*args, **kwargs):
    if not _QUIET:
        print(*args, **kwargs)


def _table(supabase, name: str):
    return supabase.schema(PC1_SCHEMA).table(name)


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _write_debug_html(path: str, html: str) -> None:
    """
    Write HTML to `path`, creating the parent directory if needed.
    The repo ships with debug/ in .gitignore so dumps land there
    without polluting the working tree, but the folder isn't tracked
    — so it might not exist in a fresh clone.
    """
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")


# ── Modality dropdown discovery ─────────────────────────────────────────────

def find_modality_dropdown(soup: BeautifulSoup):
    """Return the <select> element used as the grid's Modality Type filter."""
    for sel in soup.find_all("select"):
        attrs = ((sel.get("id") or "") + " " + (sel.get("name") or "")).lower()
        if "modalitytype" in attrs:
            return sel
    return None


def list_modality_options(soup: BeautifulSoup) -> list:
    """Return [{'value','label'}] for non-empty Modality Type options."""
    sel = find_modality_dropdown(soup)
    if not sel:
        return []
    out = []
    for opt in sel.find_all("option"):
        val = (opt.get("value") or "").strip()
        label = opt.get_text(strip=True)
        if val:
            out.append({"value": val, "label": label})
    return out


# ── Pass 1: grid scrape per modality ────────────────────────────────────────

def post_grid_for_modality(session, form_state, base_fields, dd_name, dd_value):
    """
    Submit ViewStandardProcedures.aspx with the Modality Type filter
    set to `dd_value`. Returns (response_html, refreshed_base_fields).

    The page is a plain ASP.NET WebForms full-page postback (no
    UpdatePanel AJAX) — so we send the whole form back with
    __EVENTTARGET set to the dropdown's UniqueID and parse the full
    HTML response.
    """
    payload = dict(base_fields)
    payload.update({
        "__EVENTTARGET":        dd_name,
        "__EVENTARGUMENT":      "",
        "__LASTFOCUS":          "",
        "__VIEWSTATE":          form_state.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": form_state.get("__VIEWSTATEGENERATOR", ""),
        "__EVENTVALIDATION":    form_state.get("__EVENTVALIDATION", ""),
    })
    payload[dd_name] = dd_value
    r = session.post(VIEW_PROCEDURES_URL, data=payload, timeout=60)
    r.raise_for_status()
    new_state = extract_form_state_from_html(r.text)
    for k, v in new_state.items():
        if v:
            form_state[k] = v
    new_base = extract_all_form_fields(r.text)
    return r.text, (new_base or base_fields)


def parse_grid(html: str) -> list:
    """Parse procedure rows from the rendered grid."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        oc = tr.get("onclick") or ""
        if "setActionSource" not in oc:
            continue
        m = SET_ACTION_RE.search(oc)
        if not m:
            continue
        proc_id = m.group(1)
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        cells = [td.get_text(strip=True) for td in tds[:4]]
        try:
            req_time = int(cells[1]) if cells[1] else None
        except ValueError:
            req_time = None
        rows.append({
            "source_record_key":   proc_id,
            "required_time_grid":  req_time,
            "modality_type_grid":  cells[2] or None,
            "procedure_desc_grid": cells[3],
        })
    return rows


# ── Pass 2: per-procedure wizard fetch ──────────────────────────────────────

def fetch_wizard(session, procedure_id: str, timeout: int = 60,
                 max_retries: int = 3) -> str:
    """
    GET the StandardProcedureWizard for a given procedure ID. The
    `rwndrnd` query param is a NovaRIS-side cache-buster — without it
    Telerik returns a stale page on hot reloads.

    Retries with exponential backoff (0.5s, 1s, 2s) on transient HTTP
    errors. Three attempts total — anything that fails all three is
    a real problem worth surfacing to the operator.
    """
    rwnd = f"0.{int(time.time()*1000) & 0xFFFFFFFFFFFFF}"
    params = {
        "type": "dialog",
        "standardProcedureID": procedure_id,
        "rwndrnd": rwnd,
    }
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = session.get(WIZARD_URL, params=params, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise last_exc


def parse_wizard(html: str) -> dict:
    """
    Extract the editable fields from the wizard dialog. Returns a
    dict with the fields the grid doesn't expose. Modality and
    required_time are also re-read here for cross-verification — the
    wizard wins over the grid when both supply a value.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {
        "procedure_desc":            None,
        "modality_type":             None,
        "required_time":             None,
        "anatomical_area":           None,
        "exam_prep_instructions":    None,
        "exam_prep_requires_prompt": False,
    }

    inp = soup.find("input", {"id": "ctl00_ContentPlaceHolder1_procedureName"})
    if inp:
        out["procedure_desc"] = inp.get("value") or None

    sel = soup.find("select", {"id": "ctl00_ContentPlaceHolder1_modalityTypeDD"})
    if sel:
        opt = sel.find("option", selected=True)
        if opt:
            out["modality_type"] = (opt.get("value") or "").strip() or None

    inp = soup.find("input", {"id": "ctl00_ContentPlaceHolder1_requiredTime"})
    if inp:
        try:
            v = int(inp.get("value", "") or 0)
            out["required_time"] = v if v > 0 else None
        except ValueError:
            pass

    sel = soup.find("select", {"id": "ctl00_ContentPlaceHolder1_anatomicalAreaDD"})
    if sel:
        opt = sel.find("option", selected=True)
        if opt:
            text = opt.get_text(strip=True)
            out["anatomical_area"] = text or None

    ta = soup.find("textarea",
                   {"id": "ctl00_ContentPlaceHolder1_examPrepInstructions"})
    if ta:
        text = (ta.text or "").strip()
        out["exam_prep_instructions"] = text or None

    inp = soup.find("input",
                    {"id": "ctl00_ContentPlaceHolder1_requiredField",
                     "type": "checkbox"})
    if inp is None:
        inp = soup.find("input",
                        {"name": "ctl00$ContentPlaceHolder1$requiredField"})
    if inp is not None:
        out["exam_prep_requires_prompt"] = inp.has_attr("checked")

    return out


# ── CPT code extraction ─────────────────────────────────────────────────────

def parse_procedure_codes(procedure_desc: str) -> list:
    """
    Extract CPT (or HCPCS) codes from a procedure description.

    Primary format — trailing parenthesized list:
        "MR ANGIO HEAD (70544,70545)"  →  ['70544', '70545']
        "X-RAY ABDOMEN (76705)"        →  ['76705']
        "HCPCS RADIOTRACER (73719,A9579)" → ['73719', 'A9579']

    Fallback — trailing CPT-like tokens at end-of-string without
    parens (US_TATE/INTERVENTIONAL/NM families):
        "T-Left Breast Complete US 76641"     →  ['76641']
        "T-Carotid Bilateral US 93880, 76536" →  ['93880', '76536']
        "NM--BONE SCAN - 3 PHASE 78315"       →  ['78315']

    Returns [] when no codes are detected (common for descriptions
    NovaRIS hasn't fully maintained — the row still gets persisted
    with an empty array so order-side joins fall through to the
    description-equality safety net).
    """
    if not procedure_desc:
        return []

    m = PROCEDURE_CODE_RE.search(procedure_desc)
    if m:
        parts = m.group(1).split(",")
        return [p.strip() for p in parts if p.strip()]

    m = PROCEDURE_CODE_FALLBACK_RE.search(procedure_desc)
    if m:
        return PROCEDURE_CODE_TOKEN_RE.findall(m.group(1))

    return []


# ── slot_size lookup + required_slots ───────────────────────────────────────

def get_global_slot_size(supabase, client_id: int) -> int:
    """
    Read the global default slot_size (in minutes) from pc1.clients.

    The scraper writes global proceduresestimate rows (facility_id IS
    NULL) so it uses the per-tenant default rather than any specific
    facility's slot_size. If pc1.clients.slot_size is NULL or the row
    is missing, falls back to DEFAULT_SLOT_MINUTES.

    Note: facilities with their own slot_size will see required_slots
    that don't match their per-facility arithmetic. The scheduler is
    responsible for recomputing per-facility when it matters; the
    stored value here is the per-tenant default.
    """
    row = (
        _table(supabase, "clients")
        .select("slot_size")
        .eq("id", client_id)
        .limit(1)
        .execute()
        .data or [None]
    )[0]
    if row is None or row.get("slot_size") is None:
        return DEFAULT_SLOT_MINUTES
    return int(row["slot_size"])


def required_slots_from_minutes(minutes, slot_minutes: int) -> int:
    """ceil(minutes / slot_minutes). Returns 0 for missing/invalid input."""
    if not minutes or int(minutes) <= 0:
        return 0
    return (int(minutes) + slot_minutes - 1) // slot_minutes


# ── Content hash + record building ──────────────────────────────────────────

# Hashed fields are the business columns the scraper writes. facility_id /
# modality_id are always NULL for scraper rows so they contribute "null"
# uniformly — including them is fine and matches the documented composition.
# client_id is excluded (re-keying a tenant shouldn't churn every hash).
# Audit timestamps + is_active are excluded (they're not "content").
HASHED_FIELDS = (
    "facility_id", "modality_id",
    "modality_type",
    "procedure_code", "procedure_desc",
    "required_time", "required_slots",
    "anatomical_area",
    "exam_prep_instructions", "exam_prep_requires_prompt",
)


def compute_hash(record: dict) -> str:
    payload = json.dumps(
        {k: record.get(k) for k in HASHED_FIELDS},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_db_record(grid_row: dict, wizard: dict, client_id: int,
                    slot_minutes: int, modality_dd_value: str,
                    now_iso: str) -> dict:
    """
    Merge a grid row + its wizard data into the dict shape PostgREST
    expects for pc1.proceduresestimate. Wizard values win when both
    sources supply a field (wizard is the authoritative editor page;
    grid is just the listing).
    """
    procedure_desc = wizard.get("procedure_desc") or grid_row.get("procedure_desc_grid")
    modality_type  = wizard.get("modality_type")  or grid_row.get("modality_type_grid")
    required_time  = wizard.get("required_time")  or grid_row.get("required_time_grid")
    procedure_code = parse_procedure_codes(procedure_desc or "")

    rec = {
        "client_id":                 client_id,
        "facility_id":               None,        # scraper writes globals only
        "modality_id":               None,        # scraper writes globals only
        "modality_type":             modality_type,
        "procedure_code":            procedure_code,
        "procedure_desc":            procedure_desc,
        "required_time":             required_time,
        "required_slots":            required_slots_from_minutes(required_time, slot_minutes),
        "anatomical_area":           wizard.get("anatomical_area"),
        "exam_prep_instructions":    wizard.get("exam_prep_instructions"),
        "exam_prep_requires_prompt": bool(wizard.get("exam_prep_requires_prompt")),
        "source_record_key":        grid_row["source_record_key"],
        "is_active":                 True,
        "ris_system":                RIS_SYSTEM,
        "ris_sync_status":           "synced",
        "ris_metadata": {
            "writer":            WRITER_NAME,
            "modality_dropdown": modality_dd_value,
        },
        "updated_at":                now_iso,
        "synced_at":                 now_iso,
        "ris_last_synced_at":        now_iso,
        "created_by":                None,
        "updated_by":                None,
    }
    rec["content_hash"] = compute_hash(rec)
    return rec


# ── Supabase IO ─────────────────────────────────────────────────────────────

def _fetch_existing_global(supabase, client_id: int) -> dict:
    """
    Paginated SELECT of every scraper-managed row for this client —
    i.e. global rows (facility_id IS NULL AND modality_id IS NULL)
    that have a source_record_key. Manually-inserted override rows
    (non-NULL facility_id or modality_id) are intentionally excluded;
    they're not part of the scraper's lifecycle.

    Returns dict keyed by source_record_key.
    """
    out = {}
    page_size = 1000
    start = 0
    while True:
        end = start + page_size - 1
        page = (
            _table(supabase, "proceduresestimate")
            .select("id, source_record_key, content_hash, is_active, created_at")
            .eq("client_id", client_id)
            .is_("facility_id", "null")
            .is_("modality_id", "null")
            .not_.is_("source_record_key", "null")
            .range(start, end)
            .execute()
            .data or []
        )
        for r in page:
            key = r.get("source_record_key")
            if key:
                out[key] = r
        if len(page) < page_size:
            break
        start += page_size
    return out


def write_initial_load(supabase, client_id: int, records: list, dry_run: bool):
    """
    Wipe scraper-managed global rows for this client, then bulk insert.
    Override rows (non-NULL facility_id or modality_id) and app-side
    rows (NULL source_record_key) are preserved.
    """
    print(f"  [initial-load] {len(records)} procedures", end="", flush=True)
    if dry_run:
        print(" (dry run, not saved).")
        return

    (
        _table(supabase, "proceduresestimate")
        .delete()
        .eq("client_id", client_id)
        .is_("facility_id", "null")
        .is_("modality_id", "null")
        .not_.is_("source_record_key", "null")
        .execute()
    )

    for batch in _chunked(records, 200):
        # created_at must be set explicitly on initial-load inserts
        # so the column list is uniform across the batch — PostgREST
        # normalizes columns and rejects rows that omit a NOT NULL
        # column even if the DB has a DEFAULT.
        for r in batch:
            r["created_at"] = r["updated_at"]
        _table(supabase, "proceduresestimate").insert(batch).execute()

    print(" → wiped + inserted.")


def write_delta(supabase, client_id: int, records: list, dry_run: bool):
    """
    Delta sync.

        - skip rows whose content_hash matches the DB (unchanged)
        - INSERT new rows in batches
        - UPDATE changed rows per-row (PostgREST upsert can't target
          the partial unique index we use for the 4-shape override
          pattern)
        - UPDATE soft-deletes for keys missing from the scrape

    created_at is preserved on UPDATE (we copy it from the existing
    row into the payload). For INSERT it's set to now via the DEFAULT
    on the column.
    """
    print(f"  [delta] {len(records)} scraped procedures", end="", flush=True)

    by_key = _fetch_existing_global(supabase, client_id)

    to_insert: list = []
    to_update: list = []   # (id, payload) tuples
    unchanged = 0
    reactivated = 0

    for r in records:
        key = r["source_record_key"]
        prev = by_key.get(key)
        if prev is None:
            r["created_at"] = r["updated_at"]
            to_insert.append(r)
        elif not prev.get("is_active"):
            payload = dict(r)
            payload["created_at"] = prev.get("created_at") or r["updated_at"]
            payload["is_active"] = True
            to_update.append((prev["id"], payload))
            reactivated += 1
        elif prev.get("content_hash") != r["content_hash"]:
            payload = dict(r)
            payload["created_at"] = prev.get("created_at") or r["updated_at"]
            to_update.append((prev["id"], payload))
        else:
            unchanged += 1

    scraped_keys = {r["source_record_key"] for r in records}
    to_deactivate = [
        row["id"] for key, row in by_key.items()
        if row.get("is_active") and key not in scraped_keys
    ]

    updates = len(to_update)
    print(f"  insert={len(to_insert)}  update={updates} "
          f"(reactivated={reactivated})  unchanged={unchanged}"
          f"  deactivate={len(to_deactivate)}", end="")

    if dry_run:
        print(" (dry run, not applied).")
        return

    for batch in _chunked(to_insert, 200):
        _table(supabase, "proceduresestimate").insert(batch).execute()

    for rid, payload in to_update:
        _table(supabase, "proceduresestimate") \
            .update(payload) \
            .eq("id", rid) \
            .execute()

    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in _chunked(to_deactivate, 500):
        _table(supabase, "proceduresestimate").update(
            {
                "is_active":          False,
                "updated_at":         now_iso,
                "synced_at":          now_iso,
                "ris_last_synced_at": now_iso,
                "ris_sync_status":    "synced",
                "updated_by":         None,
            }
        ).in_("id", batch).execute()
    print(" → applied.")


# ── Driver ──────────────────────────────────────────────────────────────────

def scrape(args):
    global _QUIET
    _QUIET = args.quiet

    started_at = time.time()
    client_id = resolve_client_id(args)
    # Always create the client — even in dry-run we read the existing
    # rows and pc1.clients.slot_size for an honest preview. The write
    # functions short-circuit before any DML when dry_run=True.
    supabase = get_supabase()

    session = make_session()
    if USERNAME:
        # NovaRIS login prints progress lines; swallow them under
        # --quiet so cron output stays compact.
        if _QUIET:
            with contextlib.redirect_stdout(io.StringIO()):
                ok = login(session)
        else:
            ok = login(session)
        if not ok:
            print("ERROR: NovaRIS login failed.")
            sys.exit(1)

    vprint(f"GET {VIEW_PROCEDURES_URL} ...")
    r = session.get(VIEW_PROCEDURES_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    modality_dd = find_modality_dropdown(soup)
    if modality_dd is None:
        print("ERROR: could not find Modality Type dropdown on "
              "ViewStandardProcedures.aspx.")
        sys.exit(1)
    dd_name = modality_dd.get("name") or ""
    options = list_modality_options(soup)

    if args.modality:
        wanted = args.modality.lower()
        options = [o for o in options if o["value"].lower() == wanted]
        if not options:
            print(f"ERROR: modality {args.modality!r} not found in dropdown options.")
            sys.exit(2)

    vprint(f"  modality dropdown: {dd_name!r} with {len(options)} "
           f"option(s) to scrape.")

    form_state = extract_form_state_from_html(r.text)
    base_fields = extract_all_form_fields(r.text)

    saved_grid = saved_wizard = False
    all_grid_rows: list = []
    grid_rows_by_modality: dict = {}   # source_record_key → modality_dd_value

    # Pass 1: per-modality grid scrape
    for opt in options:
        mod_value = opt["value"]
        vprint(f"  [pass1] modality={mod_value!r} ... ", end="", flush=True)
        try:
            html, base_fields = post_grid_for_modality(
                session, form_state, base_fields, dd_name, mod_value
            )
        except Exception as exc:
            print(f"ERROR posting modality {mod_value!r}: {exc}")
            continue

        if args.save_grid and not saved_grid:
            _write_debug_html(args.save_grid, html)
            vprint(f"\n    [debug] grid saved to {args.save_grid!r}", end="")
            saved_grid = True

        rows = parse_grid(html)
        if args.limit:
            rows = rows[:args.limit]
        vprint(f"{len(rows)} procedures.")
        for row in rows:
            grid_rows_by_modality[row["source_record_key"]] = mod_value
        all_grid_rows.extend(rows)

    # Dedupe on source_record_key. A procedure SHOULD only appear under
    # one modality, but the API has surprised us before — be defensive.
    by_id = {}
    for row in all_grid_rows:
        by_id[row["source_record_key"]] = row
    all_grid_rows = list(by_id.values())

    if not all_grid_rows:
        print("No procedures parsed. Nothing to do.")
        return
    vprint(f"\n  Total unique procedures (Pass 1): {len(all_grid_rows)}")

    # Pass 2: parallel wizard fetches with progress + ETA reporting
    workers = max(1, args.workers)
    total = len(all_grid_rows)
    print(f"  [pass2] fetching wizard for {total} procedures "
          f"({workers} workers) ...")
    wizards: dict = {}
    progress_lock = threading.Lock()
    progress = {"done": 0, "failed": 0}
    pass2_started = time.time()

    def _fetch_one(pid):
        try:
            return pid, fetch_wizard(session, pid)
        except Exception as exc:
            return pid, f"__ERROR__:{exc}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for pid, html in ex.map(
            _fetch_one, (r["source_record_key"] for r in all_grid_rows)
        ):
            if isinstance(html, str) and html.startswith("__ERROR__:"):
                with progress_lock:
                    progress["failed"] += 1
                print(f"\n    WARN wizard {pid}: {html[10:]}")
                wizards[pid] = ""
            else:
                wizards[pid] = html
                if args.save_wizard and not saved_wizard:
                    _write_debug_html(args.save_wizard, html)
                    vprint(f"\n    [debug] wizard saved to "
                           f"{args.save_wizard!r}", end="")
                    saved_wizard = True

            # Progress: print every 50 completions + at the end with ETA.
            with progress_lock:
                progress["done"] += 1
                done = progress["done"]
                failed = progress["failed"]
            if done % 50 == 0 or done == total:
                elapsed = time.time() - pass2_started
                rate = done / elapsed if elapsed > 0 else 0
                remaining = total - done
                eta_s = int(remaining / rate) if rate > 0 else 0
                em, es = divmod(eta_s, 60)
                pct = (done * 100) // total
                print(f"    progress: {done}/{total} ({pct}%)  "
                      f"rate: {rate:.1f}/s  ETA: {em}m {es:02d}s  "
                      f"failed={failed}", flush=True)

    # Read the per-tenant slot_size once; required_slots = ceil(
    # required_time / slot_size) is computed per record below.
    slot_minutes = get_global_slot_size(supabase, client_id)
    vprint(f"  slot_size (from pc1.clients): {slot_minutes} min")

    now_iso = datetime.now(timezone.utc).isoformat()
    records = []
    for row in all_grid_rows:
        pid = row["source_record_key"]
        wiz_html = wizards.get(pid, "")
        wiz = parse_wizard(wiz_html) if wiz_html else {}
        mod_dd = grid_rows_by_modality.get(pid, "")
        records.append(build_db_record(row, wiz, client_id, slot_minutes,
                                        mod_dd, now_iso))

    if args.initial_load:
        write_initial_load(supabase, client_id, records, args.dry_run)
    else:
        write_delta(supabase, client_id, records, args.dry_run)

    elapsed = time.time() - started_at
    mm, ss = divmod(int(elapsed), 60)
    rate = len(records) / elapsed if elapsed > 0 else 0
    print(f"\nDone. Procedures processed: {len(records)}  "
          f"Elapsed: {mm}m {ss}s  Rate: {rate:.1f}/s")

    if progress["failed"]:
        print(f"WARNING: {progress['failed']} wizard fetches failed — "
              f"those procedures were written with grid-only data "
              f"(missing anatomical_area / exam_prep_*).")
        sys.exit(3)


DESCRIPTION = (
    "Scrape standard procedures from NovaRIS ViewStandardProcedures."
    "aspx into pc1.proceduresestimate. Modes: --initial-load "
    "(wipe scraper-managed global rows + bulk insert) or default "
    "delta (content-hash skip + insert/update + soft-delete)."
)


def main():
    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument("--initial-load", action="store_true",
                   help="Wipe scraper-managed global rows for this client "
                        "(facility_id IS NULL AND modality_id IS NULL AND "
                        "source_record_key IS NOT NULL), then bulk insert. "
                        "Override rows and app-side rows are preserved.")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse only; do not write to Supabase. Still reads "
                        "existing rows for the delta breakdown.")
    p.add_argument("--modality", default="",
                   help="Limit to one Modality Type (e.g. 'US'). Must match "
                        "the dropdown's option value exactly.")
    p.add_argument("--limit", type=int, default=0,
                   help="At most N procedures per modality. Use with "
                        "--dry-run for fast end-to-end testing.")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel wizard fetches in pass 2 (default 6). "
                        "NovaRIS is single-threaded behind the dialog "
                        "endpoint — pushing past ~8 workers triggers "
                        "timeouts without speeding up the run.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-modality progress chatter.")
    p.add_argument("--save-grid", default="",
                   help="Save the first grid HTML to FILE. Recommended path: "
                        "debug/<filename>.html (gitignored).")
    p.add_argument("--save-wizard", default="",
                   help="Save the first wizard HTML to FILE. Recommended "
                        "path: debug/<filename>.html (gitignored).")
    add_client_id_arg(p)
    args = p.parse_args()
    scrape(args)


if __name__ == "__main__":
    main()
