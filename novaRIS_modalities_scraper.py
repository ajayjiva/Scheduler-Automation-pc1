"""
novaRIS_modalities_scraper.py

Scrapes the per-facility list of modality machines from the NovaRIS
ViewModalities.aspx page and writes them to the Supabase
`pc1.modalities` table.

The page renders a single grid for each facility selected via the
"Facility:" dropdown. Each row carries 7 visible fields:

    Name          (modality_machine — e.g. 'US1-F', 'FRE-MRI')
    Facility      (facility label   — e.g. 'Inview-Fremont')
    Room          (room             — e.g. '1', 'Mammo', or blank)
    Status        (status           — 'Active', 'Inactive', 'UNKNOWN')
    Modality Type (modality_type    — 'US', 'CT', 'MR', etc.)
    AE Title      (ae_title         — 'FREUS1', 'SONOACQ', or blank)
    Worklist      (worklist_enabled — checkbox state)

Schema mapping (pc1.modalities)
-------------------------------
    client_id          ← --client-id (resolve_client_id)
    facility_id        ← pc1.facilities lookup by (client_id, facility_name)
    ris_system         ← 'NovaRIS' (hardcoded)
    ris_modality_id    ← NULL (NovaRIS grid has no internal id)
    ris_modality_code  ← parsed modality_type
    ris_modality_name  ← NULL
    modality_type      ← parsed modality_type
    modality_machine   ← parsed Name field
    source_record_key  ← parsed Name field (unique within a facility)
    room/status/ae_title/worklist_enabled  ← direct copies
    content_hash       ← SHA-256 of source-side fields only
                         (facility_id / client_id excluded so re-keying
                         a facility doesn't churn every hash)
    ris_metadata       ← {writer, facility_label}
    is_active          ← True for scraped rows; flipped False on soft-delete

Modes
-----
    --initial-load   Wipe per-facility rows, then bulk insert.
                     Use this once when first onboarding a facility,
                     or after a manual cleanup.
    (default)        Delta sync. For each facility:
                       * fetch (source_record_key, content_hash) from DB
                       * skip rows whose hash matches (no write)
                       * upsert new/changed rows
                       * soft-delete (is_active=false) rows whose
                         source_record_key is missing from the scrape

Facility scope
--------------
When --facility is NOT passed, the scraper iterates every row in
pc1.facilities for the active tenant that has `is_client = true`.
Non-client facilities are skipped silently (no NovaRIS round-trip,
no DB write). Pass --facility=NAME to bypass this filter for one-off
testing.

There is no hardcoded list of facilities anywhere in this codebase.
For each iterated facility, the scraper looks up the NovaRIS facility
ID at run time by parsing the live "Facility:" dropdown on
ViewModalities.aspx and matching the option text to
pc1.facilities.facility_name exactly (case + whitespace sensitive).

If a pc1.facilities row's facility_name has no matching dropdown
option, the scraper prints a loud ERROR naming the mismatch and the
options it actually saw, skips that facility, processes the rest, and
exits non-zero so a cron monitor catches it. This is how facility
renames in NovaRIS surface: as a clear actionable error on the next
nightly run.

Flags
-----
    --facility=NAME       Limit to one facility (e.g. 'Inview-Fremont').
                          Default: iterate every is_client=true facility
                          in pc1.facilities.
    --dry-run             Parse only; do not touch Supabase.
    --client-id=NNNN      Override the active tenant for this run.
    --quiet               Suppress per-facility progress chatter.
    --save-grid=FILE      Save the first parsed grid HTML to FILE for
                          debugging.

Required .env
-------------
    NOVARISURL, NOVARISUSER, NOVARISPASSWORD, SUPABASE_URL, SUPABASE_KEY
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from supabase_client import get_supabase
from client_context import add_client_id_arg, resolve_client_id
from novaRIS_common import (
    BASE_URL,
    extract_all_form_fields,
    extract_form_state_from_html,
    login,
    make_session,
)

VIEW_MODALITIES_URL = f"{BASE_URL}/ViewModalities.aspx"

# Identifier recorded in ris_metadata.writer for traceability.
WRITER_NAME = "novaRIS_modalities_scraper.py"

# Source RIS for ris_system column.
RIS_SYSTEM = "NovaRIS"

# Schema-qualified Supabase access. The new project keeps multi-tenant
# tables under pc1 rather than public; PostgREST must expose pc1 in
# db-schemas for these calls to reach the DB.
PC1_SCHEMA = "pc1"

_QUIET = False


def vprint(*args, **kwargs):
    if not _QUIET:
        print(*args, **kwargs)


def _table(supabase, name: str):
    return supabase.schema(PC1_SCHEMA).table(name)


# ── Form / page helpers (copied verbatim from the old scraper) ──────────────

def _find_dropdown_name(soup: BeautifulSoup, key: str) -> str:
    """Find the `name` of a <select> whose id contains `key` (case-insensitive)."""
    for sel in soup.find_all("select"):
        attrs = (sel.get("id") or "") + " " + (sel.get("name") or "")
        if key.lower() in attrs.lower():
            return sel.get("name", "")
    return ""


def _parse_facility_dropdown(soup: BeautifulSoup, dropdown_name: str) -> dict:
    """
    Build {facility_display_name: NovaRIS facilitiesDD value} from the
    live ViewModalities.aspx page. The scraper uses this map at run
    time to translate pc1.facilities.facility_name into the value the
    POST needs — so onboarding a new facility is a single INSERT into
    pc1.facilities, never a code change.

    Empty option values (the "Please select…" placeholder) are skipped.
    """
    select = soup.find("select", {"name": dropdown_name})
    if select is None:
        return {}
    mapping: dict = {}
    for opt in select.find_all("option"):
        value = (opt.get("value") or "").strip()
        if not value:
            continue
        label = opt.get_text(strip=True)
        if not label:
            continue
        mapping[label] = value
    return mapping


def _find_search_event_target(soup: BeautifulSoup) -> str:
    """
    Find the __EVENTTARGET name for the Search button.

    On ViewModalities.aspx the button is rendered as an anchor:
      <a href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$filterButton','')">Search</a>

    We pull the first argument out of the __doPostBack call. Falls back
    to a plain <input type=submit value=Search> for robustness against
    future UI changes.
    """
    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip().lower()
        if text != "search":
            continue
        href = a.get("href", "") or ""
        m = re.search(r"__doPostBack\(\s*'([^']+)'", href)
        if m:
            return m.group(1)
    for inp in soup.find_all("input", {"type": ["submit", "button"]}):
        val = (inp.get("value") or "").strip().lower()
        if val == "search":
            return inp.get("name", "") or ""
    return ""


def _post_search(session, form_state, base_fields, facility_dd_name,
                 facility_id, search_event_target):
    """
    Submit the Search button as a standard ASP.NET WebForms postback.

    ViewModalities.aspx uses a plain full-page form post (no UpdatePanel
    AJAX), so we just POST the form with __EVENTTARGET set to the
    button's UniqueID and parse the full HTML response.
    """
    payload = dict(base_fields)
    payload.update({
        "__EVENTTARGET":        search_event_target,
        "__EVENTARGUMENT":      "",
        "__LASTFOCUS":          "",
        "__VIEWSTATE":          form_state.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": form_state.get("__VIEWSTATEGENERATOR", ""),
        "__EVENTVALIDATION":    form_state.get("__EVENTVALIDATION", ""),
    })
    if facility_dd_name:
        payload[facility_dd_name] = facility_id

    r = session.post(VIEW_MODALITIES_URL, data=payload, timeout=60)
    r.raise_for_status()
    new_state = extract_form_state_from_html(r.text)
    for k, v in new_state.items():
        if v:
            form_state[k] = v
    return r.text


# ── Grid parsing (copied verbatim from the old scraper) ─────────────────────

# Confirmed by inspecting the live page:
#   <td>Name</td><td>Facility</td><td>Room</td><td>Status</td>
#   <td>Modality Type</td><td>AE Title</td>
#   <td><span><input type="checkbox" ...></span></td>
GRID_COLUMN_COUNT = 7


def _cell_text(cell) -> str:
    return (cell.get_text(strip=True) or "").strip()


def _parse_worklist_cell(cell) -> bool:
    """Worklist Enabled cell wraps a checkbox; checked → True."""
    inp = cell.find("input", {"type": "checkbox"})
    if inp is None:
        return False
    return inp.has_attr("checked")


def parse_modalities_grid(html: str, facility_label: str) -> list:
    """
    Parse the modalities grid HTML and return a list of parsed dicts.

    NovaRIS alternates two row classes — odd rows use `rowstyle`, even
    rows use `altrowstyle`. Match either to capture all rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all(
        "tr",
        class_=lambda c: c and any(
            cls in ("rowstyle", "altrowstyle") for cls in c.split()
        ),
    )
    out = []
    for tr in rows:
        cells = tr.find_all("td")
        if len(cells) < GRID_COLUMN_COUNT:
            continue
        name      = _cell_text(cells[0])
        facility  = _cell_text(cells[1]) or facility_label
        room      = _cell_text(cells[2]) or None
        status    = _cell_text(cells[3]) or None
        mod_type  = _cell_text(cells[4]) or None
        ae_title  = _cell_text(cells[5]) or None
        worklist  = _parse_worklist_cell(cells[6])

        if not name:
            continue
        out.append({
            "modality_machine":  name,
            "facility_label":    facility,
            "room":              room,
            "status":            status,
            "modality_type":     mod_type,
            "ae_title":          ae_title,
            "worklist_enabled":  worklist,
        })
    return out


# ── Hashing & DB shaping ────────────────────────────────────────────────────

# Source-side fields only. facility_id and client_id are deliberately
# excluded so re-keying a facility doesn't invalidate every hash.
HASHED_FIELDS = (
    "modality_machine", "facility_label", "room", "status",
    "modality_type", "ae_title", "worklist_enabled",
)


def compute_hash(parsed: dict) -> str:
    payload = json.dumps(
        {k: parsed.get(k) for k in HASHED_FIELDS},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Facility lookup ─────────────────────────────────────────────────────────

_FACILITY_ID_CACHE: dict = {}


def fetch_client_facility_names(supabase, client_id: int) -> list:
    """
    Return the sorted list of pc1.facilities.facility_name values for
    this client that are flagged is_client=true. These are the
    facilities the nightly run will attempt to sync — each one is
    matched against the live NovaRIS dropdown at run time.

    Returns sorted for deterministic per-facility log ordering.
    """
    rows = (
        _table(supabase, "facilities")
        .select("facility_name")
        .eq("client_id", client_id)
        .eq("is_client", True)
        .execute()
        .data or []
    )
    return sorted({row["facility_name"] for row in rows})


def resolve_facility_id(supabase, client_id: int, facility_label: str):
    """
    Look up pc1.facilities.id for (client_id, facility_name). Hard-fail
    if missing — facilities are expected to be pre-seeded with exact
    NovaRIS display labels. Crashing here beats writing orphan modality
    rows pointed at a NULL facility.
    """
    cache_key = (client_id, facility_label)
    if cache_key in _FACILITY_ID_CACHE:
        return _FACILITY_ID_CACHE[cache_key]
    row = (
        _table(supabase, "facilities")
        .select("id")
        .eq("client_id", client_id)
        .eq("facility_name", facility_label)
        .limit(1)
        .execute()
        .data or [None]
    )[0]
    if row is None:
        raise RuntimeError(
            f"No pc1.facilities row found for client_id={client_id}, "
            f"facility_name={facility_label!r}. Seed the facility "
            f"before running the scraper."
        )
    _FACILITY_ID_CACHE[cache_key] = row["id"]
    return row["id"]


def build_db_record(parsed: dict, client_id: int, facility_id,
                    now_iso: str) -> dict:
    mod_type = parsed.get("modality_type")
    return {
        "client_id":           client_id,
        "facility_id":         facility_id,
        "ris_system":          RIS_SYSTEM,
        "ris_modality_id":     None,
        "ris_modality_code":   mod_type,
        "ris_modality_name":   None,
        "modality_type":       mod_type,
        "modality_machine":    parsed["modality_machine"],
        "source_record_key":   parsed["modality_machine"],
        "room":                parsed.get("room"),
        "status":              parsed.get("status"),
        "ae_title":            parsed.get("ae_title"),
        "worklist_enabled":    bool(parsed.get("worklist_enabled")),
        "is_active":           True,
        "content_hash":        compute_hash(parsed),
        "ris_metadata": {
            "writer":         WRITER_NAME,
            "facility_label": parsed["facility_label"],
        },
        "updated_at":          now_iso,
        "synced_at":           now_iso,
        "ris_last_synced_at":  now_iso,
        "created_by":          None,
        "updated_by":          None,
    }


# ── Supabase write paths ────────────────────────────────────────────────────

def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fetch_all_existing(supabase, client_id: int, facility_id) -> list:
    """Fetch every pc1.modalities row for (client, facility) — paginated."""
    out = []
    page_size = 1000
    start = 0
    while True:
        end = start + page_size - 1
        page = (
            _table(supabase, "modalities")
            .select("id, source_record_key, content_hash, is_active, "
                    "created_at")
            .eq("client_id", client_id)
            .eq("facility_id", facility_id)
            .range(start, end)
            .execute()
            .data or []
        )
        out.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return out


def write_initial_load(supabase, facility_label: str, client_id: int,
                       facility_id, records: list, dry_run: bool):
    """Wipe per-facility rows for this client, then bulk insert."""
    if dry_run:
        print(f"  [{facility_label}] would wipe + insert {len(records)} records "
              f"(dry run, no writes).")
        return
    _table(supabase, "modalities") \
        .delete() \
        .eq("client_id", client_id) \
        .eq("facility_id", facility_id) \
        .execute()
    for batch in _chunked(records, 200):
        for r in batch:
            r["created_at"] = r["updated_at"]
        _table(supabase, "modalities").insert(batch).execute()
    print(f"  [{facility_label}] initial-load: wiped + inserted "
          f"{len(records)} rows.")


def write_delta(supabase, facility_label: str, client_id: int, facility_id,
                records: list, dry_run: bool):
    """
    Delta sync.

        - skip rows whose content_hash matches the DB (unchanged)
        - upsert inserts + content-hash updates + reactivations
        - soft-delete (is_active=false) keys missing from the scrape

    created_at is set on every row in to_upsert before the batched
    upsert. PostgREST normalizes column lists across rows in a batch,
    so any row missing created_at would fail the NOT NULL check.
    """
    existing = _fetch_all_existing(supabase, client_id, facility_id)
    by_key = {row["source_record_key"]: row
              for row in existing if row.get("source_record_key")}

    to_upsert, unchanged, reactivated, inserts = [], 0, 0, 0
    for r in records:
        key = r["source_record_key"]
        prev = by_key.get(key)
        if prev is None:
            r["created_at"] = r["updated_at"]
            to_upsert.append(r)
            inserts += 1
        elif not prev.get("is_active"):
            r["created_at"] = prev.get("created_at") or r["updated_at"]
            r["is_active"] = True
            to_upsert.append(r)
            reactivated += 1
        elif prev.get("content_hash") != r["content_hash"]:
            r["created_at"] = prev.get("created_at") or r["updated_at"]
            to_upsert.append(r)
        else:
            unchanged += 1

    scraped_keys = {r["source_record_key"] for r in records}
    to_deactivate = [
        row["id"] for key, row in by_key.items()
        if row.get("is_active") and key not in scraped_keys
    ]

    updates = len(to_upsert) - inserts

    print(f"  [delta] {facility_label}: {len(records)} scraped records  "
          f"insert={inserts}  update={updates} (reactivated={reactivated})  "
          f"unchanged={unchanged}  deactivate={len(to_deactivate)}", end="")

    if dry_run:
        print(" (dry run, not applied).")
        return

    for batch in _chunked(to_upsert, 500):
        _table(supabase, "modalities").upsert(
            batch, on_conflict="facility_id,source_record_key"
        ).execute()

    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in _chunked(to_deactivate, 500):
        _table(supabase, "modalities").update(
            {
                "is_active":          False,
                "updated_at":         now_iso,
                "synced_at":          now_iso,
                "ris_last_synced_at": now_iso,
                "updated_by":         None,
            }
        ).in_("id", batch).execute()
    print(" -> applied.")


# ── Driver ──────────────────────────────────────────────────────────────────

def scrape(args):
    global _QUIET
    _QUIET = args.quiet

    client_id = resolve_client_id(args)
    supabase = get_supabase()

    if args.facility:
        facilities = [args.facility]
        single_facility_mode = True
    else:
        single_facility_mode = False
        facilities = fetch_client_facility_names(supabase, client_id)
        if not facilities:
            print(f"No is_client=true facilities found in pc1.facilities "
                  f"for client_id={client_id}. Nothing to scrape.")
            return

    session = make_session()
    if not login(session):
        sys.exit(1)

    started_at = time.time()
    grand_total = 0
    missing_facilities: list = []

    r = session.get(VIEW_MODALITIES_URL, timeout=30)
    r.raise_for_status()
    soup_initial = BeautifulSoup(r.text, "html.parser")

    facility_dd  = _find_dropdown_name(soup_initial, "facilitiesDD") \
                or _find_dropdown_name(soup_initial, "facility")
    search_evt   = _find_search_event_target(soup_initial)

    vprint(f"GET {VIEW_MODALITIES_URL}")
    vprint(f"  facility dropdown:    {facility_dd!r}")
    vprint(f"  search event target:  {search_evt!r}")

    if not facility_dd or not search_evt:
        debug_path = "viewmodalities_initial.html"
        with open(debug_path, "w", encoding="utf-8") as fh:
            fh.write(r.text)
        print(f"ERROR: could not locate the facility dropdown or Search "
              f"button on ViewModalities.aspx.")
        print(f"  Saved initial page HTML to {debug_path!r} for inspection.")
        sys.exit(1)

    # Match pc1.facilities.facility_name to the NovaRIS facilitiesDD
    # value by exact display-text equality against the live dropdown.
    runtime_facility_map = _parse_facility_dropdown(soup_initial, facility_dd)
    if not runtime_facility_map:
        print(f"ERROR: NovaRIS facility dropdown {facility_dd!r} returned "
              f"no usable options. Cannot map pc1.facilities.facility_name "
              f"to NovaRIS facility IDs.")
        sys.exit(1)
    vprint(f"  facility dropdown options: {len(runtime_facility_map)}")

    save_grid_path = args.save_grid

    for facility_label in facilities:
        novaRIS_facility_id = runtime_facility_map.get(facility_label)
        if novaRIS_facility_id is None:
            available = ", ".join(sorted(runtime_facility_map))
            print(
                f"ERROR: pc1.facilities.facility_name={facility_label!r} "
                f"(client_id={client_id}) was not found in the live "
                f"NovaRIS facility dropdown. Either the facility was "
                f"renamed in NovaRIS, or the pc1.facilities row has a "
                f"typo. Update pc1.facilities.facility_name to match "
                f"the NovaRIS display text exactly.\n"
                f"  NovaRIS dropdown currently offers: {available}"
            )
            if single_facility_mode:
                sys.exit(2)
            missing_facilities.append(facility_label)
            continue

        vprint(f"  [{facility_label}] selecting facility "
               f"(NovaRIS id={novaRIS_facility_id}) and posting Search ...",
               end=" ", flush=True)

        r = session.get(VIEW_MODALITIES_URL, timeout=30)
        r.raise_for_status()
        form_state = extract_form_state_from_html(r.text)
        base_fields = extract_all_form_fields(r.text)

        grid_html = _post_search(
            session, form_state, base_fields, facility_dd,
            novaRIS_facility_id, search_evt,
        )

        if save_grid_path:
            with open(save_grid_path, "w", encoding="utf-8") as fh:
                fh.write(grid_html)
            print(f"\n    [debug] grid HTML saved to {save_grid_path!r}")
            save_grid_path = ""

        parsed = parse_modalities_grid(grid_html, facility_label)
        vprint(f"{len(parsed)} rows parsed.")

        if not parsed:
            print(f"  [{facility_label}] no records — skipping write.")
            continue

        facility_id = resolve_facility_id(supabase, client_id, facility_label)

        now_iso = datetime.now(timezone.utc).isoformat()
        records = [build_db_record(p, client_id, facility_id, now_iso)
                   for p in parsed]

        # Defensive dedupe on the upsert conflict target.
        unique = {}
        for r_ in records:
            unique[(r_["facility_id"], r_["source_record_key"])] = r_
        records = list(unique.values())

        if args.initial_load:
            write_initial_load(supabase, facility_label, client_id,
                               facility_id, records, args.dry_run)
        else:
            write_delta(supabase, facility_label, client_id, facility_id,
                        records, args.dry_run)

        grand_total += len(records)

    elapsed = time.time() - started_at
    mm, ss = divmod(int(elapsed), 60)
    rate = grand_total / elapsed if elapsed > 0 else 0
    print(f"\nDone. Total records processed: {grand_total}  "
          f"Elapsed: {mm}m {ss}s  Rate: {rate:.1f} records/sec")

    if missing_facilities:
        print(
            f"WARNING: {len(missing_facilities)} facility(ies) skipped — "
            f"no match in NovaRIS dropdown: "
            f"{', '.join(missing_facilities)}. Update "
            f"pc1.facilities.facility_name for each, then re-run."
        )
        sys.exit(3)


DESCRIPTION = (
    "Scrape per-facility modality-machine grids from NovaRIS and "
    "sync to Supabase pc1.modalities. Modes: --initial-load "
    "(wipe + bulk insert per facility) or default delta "
    "(content-hash skip + upsert + soft-delete)."
)


def main():
    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument("--initial-load", action="store_true",
                   help="Wipe per-facility rows for this client then bulk "
                        "insert. Use once for a fresh facility, or after "
                        "manual cleanup.")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse only; do not write to Supabase.")
    p.add_argument("--facility", default="",
                   help="Limit to one facility (e.g. 'Inview-Fremont'). "
                        "Must match a pc1.facilities.facility_name AND "
                        "the live NovaRIS dropdown text exactly. Default "
                        "iterates every is_client=true facility in "
                        "pc1.facilities.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-facility progress chatter.")
    p.add_argument("--save-grid", default="",
                   help="Save the first grid HTML to FILE (use this if "
                        "parsing produces zero rows so the parser can "
                        "be adjusted).")
    add_client_id_arg(p)
    args = p.parse_args()
    scrape(args)


if __name__ == "__main__":
    main()
