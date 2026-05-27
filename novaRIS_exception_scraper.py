"""
novaRIS_exception_scraper.py

Scrapes scheduling-exception rules from NovaRIS into the Supabase
`pc1.scheduleexceptions` table. Each row is one rule — LUNCH, holiday,
machine downtime, technician out-of-office, etc. — that blocks
(`type='Hard'`) or annotates (`type='Soft'`) one or more calendar
slots, optionally repeating Daily / Weekly / Monthly.

Why two passes
--------------
NovaRIS surfaces an exception rule's data across two pages:

    Pass 1 — ModalityScheduling.aspx (the grid).
             Filtered by Facility + Modality via two dropdowns. The
             page is rendered via an ASP.NET UpdatePanel AJAX postback
             — the grid HTML comes back wrapped in pipe-delimited
             segments rather than a full HTML response. Each <tr>
             carries setActionSource('<modalityID>', '<frequencyId>',
             '<rowIdx>') plus 10 visible cells: Name (machine),
             Modality Type, Facility, Description, Start Date,
             Start Time, End Date, End Time, Recurrence, Type.

    Pass 2 — ModalitySchedulingPopup.aspx?frequencyId=<id>
             &isOccurrence=false (the per-rule detail popup).
             Required only for Daily/Weekly/Monthly recurrences to
             pull the day-of-week / day-of-month mask + the
             repeat_every interval + any "End On" date override.
             None-recurrence rules skip pass-2 entirely.

Pass 2 is the expensive one (one HTTP round-trip per recurring rule)
so it runs in a ThreadPoolExecutor with a small worker count. Total
wall-clock is dominated by the count of recurring rules across all
(facility, modality) pairs.

Iteration shape
---------------
For each `is_client = true` facility in pc1.facilities for the active
tenant:
    1. Match facility_name → NovaRIS facilitiesDD value (live dropdown).
    2. Fresh GET ModalityScheduling.aspx (resets __VIEWSTATE).
    3. POST facility-change → server returns refreshed modality
       dropdown options for that facility.
    4. Parse the modality dropdown options (each option is a machine).
    5. For each option, match the machine name → pc1.modalities.id.
       Skip with a WARN if the name isn't in pc1.modalities.
    6. For each matched (modality_dd_value, modality_id):
        a. POST modality-change → grid HTML for this (facility, modality).
        b. Pass-2: parallel popup fetches for Weekly/Monthly/Daily rows.
        c. Build pc1.scheduleexceptions records.
    7. After all modalities for the facility are scraped, run
       write_delta or write_initial_load scoped to (client_id,
       facility_id).

Schema mapping (pc1.scheduleexceptions)
---------------------------------------
    client_id           ← --client-id (resolve_client_id)
    facility_id         ← pc1.facilities lookup by (client_id, facility_name)
    modality_id         ← pc1.modalities lookup by (facility_id, modality_machine)
    ris_system          ← 'NovaRIS' (hardcoded)
    ris_sync_status     ← 'synced' (hardcoded — set on every write)
    modality_type       ← grid cell (denormalized snapshot)
    description         ← grid cell
    start_date          ← grid cell, parsed to ISO date
    start_time          ← grid cell, parsed to HH:MM:SS
    end_date            ← grid cell, parsed to ISO date
                          (popup "End On" override wins when present)
    end_time            ← grid cell, parsed to HH:MM:SS
    recurrence          ← grid cell ('None'/'Daily'/'Weekly'/'Monthly')
    type                ← grid cell ('Hard'/'Soft')
    repeat_every        ← popup "every N weeks/months" (default 1)
    weekdays_only       ← popup checkbox (Daily recurrence only)
    is_sunday..is_saturday ← popup checkboxes (Weekly recurrence only)
    day_1..day_31       ← popup Telerik combo box (Monthly recurrence only)
    source_record_key   ← NovaRIS frequencyId from setActionSource(...)
    content_hash        ← SHA-256 of business fields (see HASHED_FIELDS)
    ris_metadata        ← {writer, novaris_modality_dropdown_id, novaris_facility_id, ...}
    is_active           ← True for scraped rows; flipped False on soft-delete
    created_by / updated_by ← NULL (bigint FK to user_profiles — scraper has no user)
    created_at          ← set on insert; preserved on update
    updated_at          ← now() on every write
    synced_at           ← now() on every write
    ris_last_synced_at  ← now() on every write

Modes
-----
    --initial-load   For each scraped facility, DELETE FROM
                     pc1.scheduleexceptions WHERE client_id=? AND
                     facility_id=? AND source_record_key IS NOT NULL,
                     then bulk insert. Manually-inserted rows
                     (source_record_key NULL) are preserved.
    (default)        Delta sync per facility:
                       * one paginated SELECT of existing rows
                       * skip rows whose content_hash matches
                       * INSERT new rows in batches
                       * UPDATE changed rows per-row (PostgREST upsert
                         can't target the partial unique index)
                       * UPDATE soft-deletes for keys missing from the
                         scrape (scoped to this facility)

Flags
-----
    --dry-run             Parse only; no Supabase writes.
    --facility=NAME       Limit to one facility (e.g. 'Inview-Fremont').
    --modality=NAME       Limit to one machine name (e.g. 'FRE-MRI').
                          Case-insensitive substring match against the
                          NovaRIS modality dropdown labels.
    --limit=N             At most N rules per (facility, modality).
    --workers=N           Parallel popup fetches (default 6).
    --quiet               Suppress per-modality progress chatter.
    --client-id=NNNN      Override the active tenant.
    --save-grid=FILE      Save the first grid HTML response (debugging).
    --save-popup=FILE     Save the first popup HTML response (debugging).

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
    _parse_updatepanel_segments,
    extract_all_form_fields,
    extract_form_state_from_html,
    login,
    make_session,
    update_form_state_from_updatepanel,
)

MODALITY_SCHEDULING_URL = f"{BASE_URL}/ModalityScheduling.aspx"
POPUP_URL               = f"{BASE_URL}/ModalitySchedulingPopup.aspx"

# Identifier recorded in ris_metadata.writer for traceability.
WRITER_NAME = "novaRIS_exception_scraper.py"

# Source RIS value written to the ris_system column.
RIS_SYSTEM = "NovaRIS"

# Schema-qualified Supabase access — pc1 schema must be exposed via
# PostgREST `db-schemas` for these calls to reach the DB.
PC1_SCHEMA = "pc1"

# NovaRIS encodes the recurrence dropdown as integers; the grid shows
# them as text. The text values are what we store (and the CHECK
# constraint on pc1.scheduleexceptions.recurrence pins them).
RECURRENCE_BY_VALUE = {"-1": "None", "0": "Daily", "1": "Weekly", "2": "Monthly"}

# Same idea for the type dropdown.
TYPE_BY_VALUE = {"0": "Hard", "1": "Soft"}

# Grid <tr> onclick callback:
#   setActionSource('<modalityID>', '<frequencyId>', '<rowIdx>')
GRID_ROW_RE = re.compile(
    r"setActionSource\(\s*['\"]([^'\"]*)['\"]\s*,"
    r"\s*['\"]([^'\"]*)['\"]\s*,"
    r"\s*['\"]([^'\"]*)['\"]\s*\)"
)

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
    """Write `html` to `path`, creating the parent directory if needed."""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")


# ── Date / time parsing ────────────────────────────────────────────────────

def parse_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time().strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


# ── Page / form helpers ────────────────────────────────────────────────────

def find_dropdown_name(soup: BeautifulSoup, key: str) -> str:
    """Find the form `name` of a dropdown whose id/name contains `key`."""
    for sel in soup.find_all("select"):
        attrs = (sel.get("id") or "") + " " + (sel.get("name") or "")
        if key.lower() in attrs.lower():
            return sel.get("name", "")
    return ""


def find_script_manager_name(soup: BeautifulSoup) -> str:
    """Find the ScriptManager hidden field name (e.g. 'ctl00$ScriptMgr')."""
    init = soup.find(string=re.compile(r"PageRequestManager\._initialize"))
    if init:
        m = re.search(r"_initialize\(\s*['\"]([^'\"]+)['\"]", str(init))
        if m:
            return m.group(1)
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "") or ""
        if "ScriptMgr" in name or "ScriptManager" in name:
            return name
    return "ctl00$ScriptMgr"


def find_update_panel_id(soup: BeautifulSoup) -> str:
    """
    Extract the first UpdatePanel UniqueID from the page's
    Sys.WebForms.PageRequestManager._initialize(...) call.

    The third argument of _initialize is an array of UpdatePanel IDs
    prefixed with 't', e.g. 'tctl00$ContentPlaceHolder1$UpdatePanel1'.
    We strip the 't' to get the actual UniqueID we send in the
    ScriptManager field.
    """
    init = soup.find(string=re.compile(r"PageRequestManager\._initialize"))
    if not init:
        return ""
    m = re.search(r"_initialize\([^[]*?\[(.*?)\]", str(init), re.DOTALL)
    if not m:
        return ""
    array_content = m.group(1)
    # Skip ErrorPanel and similar non-grid panels.
    for found in re.findall(r"['\"]t(ctl00\$[^'\"]+)['\"]", array_content):
        if "Error" in found:
            continue
        return found
    found = re.findall(r"['\"]t(ctl00\$[^'\"]+)['\"]", array_content)
    return found[0] if found else ""


def parse_dropdown_options(html_or_soup, key: str) -> list:
    """
    Return [{'id', 'name'}] for the <select> whose name/id contains `key`.
    Accepts either a full HTML string or a BeautifulSoup tree.
    """
    soup = (html_or_soup if isinstance(html_or_soup, BeautifulSoup)
            else BeautifulSoup(html_or_soup, "html.parser"))
    select = None
    for s in soup.find_all("select"):
        attrs = (s.get("name") or "") + " " + (s.get("id") or "")
        if key.lower() in attrs.lower():
            select = s
            break
    if not select:
        return []
    options = []
    for opt in select.find_all("option"):
        val = (opt.get("value") or "").strip()
        label = opt.get_text(strip=True)
        if val:
            options.append({"id": val, "name": label})
    return options


def _extract_updatepanel_html(response_text: str) -> str:
    """
    Concatenate every UpdatePanel HTML segment from a partial-postback
    response. These segments hold the rendered grid HTML.
    """
    segments = _parse_updatepanel_segments(response_text)
    parts = [content for seg_type, _seg_id, content in segments
             if seg_type == "updatePanel"]
    return "\n".join(parts) if parts else response_text


def _post_dropdown_change(session, form_state, base_fields, dd_name, dd_value,
                          script_manager, update_panel_id,
                          extra_overrides=None):
    """
    Submit a dropdown-change postback as an AJAX UpdatePanel partial
    postback. Returns (extracted_panel_html, refreshed_base_fields).

    The response is pipe-delimited (length|type|id|content|...). We
    extract only the updatePanel HTML segments and merge any refreshed
    form fields back into base_fields, while also refreshing
    __VIEWSTATE / __EVENTVALIDATION / AntiXsrToken in form_state.
    """
    payload = dict(base_fields)
    payload.update({
        "__EVENTTARGET":        dd_name,
        "__EVENTARGUMENT":      "",
        "__LASTFOCUS":          "",
        "__VIEWSTATE":          form_state.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": form_state.get("__VIEWSTATEGENERATOR", ""),
        "__EVENTVALIDATION":    form_state.get("__EVENTVALIDATION", ""),
        "__ASYNCPOST":          "true",
    })
    payload[dd_name] = dd_value
    if script_manager:
        # ScriptManager field encodes which UpdatePanel this postback
        # targets: "<UpdatePanelUniqueID>|<EventTargetUniqueID>".
        panel = update_panel_id or ""
        payload[script_manager] = f"{panel}|{dd_name}"
    if extra_overrides:
        payload.update(extra_overrides)

    r = session.post(
        MODALITY_SCHEDULING_URL,
        data=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=60,
    )
    r.raise_for_status()

    # Pull VIEWSTATE / VIEWSTATEGENERATOR / AntiXsrToken from the
    # UpdatePanel hiddenField segments.
    update_form_state_from_updatepanel(r.text, form_state)

    # The shared helper doesn't catch __EVENTVALIDATION; pull it
    # explicitly so subsequent postbacks don't fail with "Invalid
    # postback or callback argument".
    for m in re.finditer(
        r'\d+\|hiddenField\|(__EVENTVALIDATION)\|([^|]*)\|', r.text
    ):
        form_state[m.group(1)] = m.group(2)

    if "|pageRedirect|" in r.text and "DefaultErrorPage" in r.text:
        raise RuntimeError(
            "Server returned pageRedirect to DefaultErrorPage. Postback "
            "rejected (likely __EVENTVALIDATION mismatch)."
        )

    panel_html = _extract_updatepanel_html(r.text)
    panel_fields = extract_all_form_fields(panel_html) if panel_html else {}
    merged = dict(base_fields)
    merged.update(panel_fields)
    return panel_html, merged


# ── Pass 1: grid parsing ───────────────────────────────────────────────────

def parse_grid(html: str) -> list:
    """
    Parse rendered ModalityScheduling.aspx grid rows.

    Returns list of dicts with: frequency_id (source_record_key),
    novaris_modality_id, name (machine), modality_type, facility,
    description, start_date, start_time, end_date, end_time,
    recurrence, type.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        oc = tr.get("onclick") or ""
        if "setActionSource" not in oc:
            continue
        m = GRID_ROW_RE.search(oc)
        if not m:
            continue
        novaris_modality_id, frequency_id, _row_idx = m.groups()

        tds = tr.find_all("td")
        if len(tds) < 10:
            continue
        cells = [td.get_text(strip=True) for td in tds[:10]]
        name, modality_type, facility, description, sd, st, ed, et, recurrence, typ = cells

        rows.append({
            "source_record_key":   frequency_id,
            "novaris_modality_id": novaris_modality_id,
            "name":                name,
            "modality_type":       modality_type or None,
            "facility":            facility,
            "description":         description or None,
            "start_date":          parse_date(sd),
            "start_time":          parse_time(st),
            "end_date":            parse_date(ed),
            "end_time":            parse_time(et),
            "recurrence":          recurrence or None,
            "type":                typ or None,
        })
    return rows


# ── Pass 2: per-rule popup fetch + parse ───────────────────────────────────

def fetch_popup(session, frequency_id: str, timeout: int = 30,
                max_retries: int = 3) -> str:
    """
    GET the ModalitySchedulingPopup for a given NovaRIS frequencyId.

    Retries with exponential backoff (0.5s, 1s, 2s) on transient HTTP
    errors. Three attempts total — anything that fails all three is a
    real problem worth surfacing.
    """
    params = {
        "modalityID":  "",
        "StartTime":   "",
        "EndTime":     "",
        "modalityType": "",
        "frequencyId": frequency_id,
        "isOccurrence": "false",
    }
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = session.get(POPUP_URL, params=params, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise last_exc


def _checkbox_checked(soup, css_id: str) -> bool:
    inp = soup.find("input", {"id": css_id})
    return bool(inp and inp.get("checked") is not None)


def _input_value(soup, css_id: str, default: str = "") -> str:
    inp = soup.find("input", {"id": css_id})
    return inp.get("value", default) if inp else default


def _selected_option_value(soup, css_id: str, default: str = "") -> str:
    sel = soup.find("select", {"id": css_id})
    if not sel:
        return default
    opt = sel.find("option", selected=True) or sel.find("option")
    return opt.get("value", default) if opt else default


def parse_popup(html: str, recurrence_from_grid: str) -> dict:
    """
    Extract recurrence detail from ModalitySchedulingPopup.aspx HTML.

    Returns dict with: repeat_every, weekdays_only,
    is_sunday..is_saturday, day_1..day_31, end_date_override (date
    string or None — set only when popup explicitly shows "End On"
    with a date that overrides the grid's end_date).
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {
        "repeat_every":      1,
        "weekdays_only":     False,
        "is_sunday":         False, "is_monday":   False,
        "is_tuesday":        False, "is_wednesday": False,
        "is_thursday":       False, "is_friday":   False,
        "is_saturday":       False,
        "end_date_override": None,
    }
    for d in range(1, 32):
        out[f"day_{d}"] = False

    rec_value = _selected_option_value(
        soup, "ctl00_ContentPlaceHolder1_repeatDD", default="-1"
    )
    rec_text = RECURRENCE_BY_VALUE.get(rec_value, recurrence_from_grid)

    if rec_text == "Weekly":
        rev = _input_value(soup, "ctl00_ContentPlaceHolder1_repeatEveryWeek", "1")
        try:
            out["repeat_every"] = int(rev) if rev else 1
        except ValueError:
            out["repeat_every"] = 1
        day_keys = ["is_sunday", "is_monday", "is_tuesday", "is_wednesday",
                    "is_thursday", "is_friday", "is_saturday"]
        for i, key in enumerate(day_keys):
            out[key] = _checkbox_checked(
                soup, f"ctl00_ContentPlaceHolder1_weeklyCheckboxlist_{i}"
            )

    elif rec_text == "Monthly":
        rev = _input_value(soup, "ctl00_ContentPlaceHolder1_repeatEveryMonth", "1")
        try:
            out["repeat_every"] = int(rev) if rev else 1
        except ValueError:
            out["repeat_every"] = 1
        # Monthly days live in a Telerik RadComboBox JS init block:
        # `"itemData":[{"value":"1","checked":false}, ...]`
        m = re.search(r'"itemData"\s*:\s*(\[.*?\])', html, re.DOTALL)
        if m:
            try:
                items = json.loads(m.group(1))
                for it in items:
                    if it.get("checked"):
                        try:
                            d = int(it.get("value", "0"))
                        except (TypeError, ValueError):
                            continue
                        if 1 <= d <= 31:
                            out[f"day_{d}"] = True
            except json.JSONDecodeError:
                pass

    elif rec_text == "Daily":
        # Look for a checkbox associated with a label mentioning "Weekday".
        for label in soup.find_all("label"):
            text = label.get_text(strip=True).lower()
            if "weekday" in text:
                target_id = label.get("for")
                if target_id:
                    out["weekdays_only"] = _checkbox_checked(soup, target_id)
                else:
                    inp = label.find("input", {"type": "checkbox"})
                    if inp and inp.get("checked") is not None:
                        out["weekdays_only"] = True
                break
        if not out["weekdays_only"]:
            for inp in soup.find_all("input", {"type": "checkbox"}):
                attrs = ((inp.get("id") or "") + " "
                         + (inp.get("name") or "")).lower()
                if "weekday" in attrs and inp.get("checked") is not None:
                    out["weekdays_only"] = True
                    break

    # End-date override: only when "End On" radio is checked.
    if _checkbox_checked(soup, "ctl00_ContentPlaceHolder1_repeatUntilRadiobutton"):
        ed = _input_value(soup, "ctl00_ContentPlaceHolder1_repeatUntilDate")
        out["end_date_override"] = parse_date(ed)

    return out


# ── Content hash + record building ─────────────────────────────────────────

# Business fields only. client_id, audit timestamps, is_active, and the
# two _by FKs are excluded — see docs/scheduleexceptions.md for the
# rationale.
HASHED_FIELDS = (
    "facility_id", "modality_id",
    "modality_type", "description",
    "start_date", "start_time", "end_date", "end_time",
    "recurrence", "type",
    "repeat_every", "weekdays_only",
    "is_sunday", "is_monday", "is_tuesday", "is_wednesday",
    "is_thursday", "is_friday", "is_saturday",
    *[f"day_{d}" for d in range(1, 32)],
)


def compute_hash(record: dict) -> str:
    payload = json.dumps(
        {k: record.get(k) for k in HASHED_FIELDS},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_db_record(grid_row: dict, popup_detail: dict, client_id: int,
                    facility_id, modality_id, novaris_facility_id: str,
                    now_iso: str) -> dict:
    """Merge a grid row + its popup detail into the pc1.scheduleexceptions shape."""
    rec = {
        "client_id":          client_id,
        "facility_id":        facility_id,
        "modality_id":        modality_id,
        "modality_type":      grid_row.get("modality_type"),
        "description":        grid_row.get("description"),
        "start_date":         grid_row.get("start_date"),
        "start_time":         grid_row.get("start_time"),
        # Popup "End On" override wins when present; otherwise the grid's end_date.
        "end_date":           popup_detail.get("end_date_override")
                              or grid_row.get("end_date"),
        "end_time":           grid_row.get("end_time"),
        "recurrence":         grid_row.get("recurrence"),
        "type":               grid_row.get("type"),
        "repeat_every":       int(popup_detail.get("repeat_every") or 1),
        "weekdays_only":      bool(popup_detail.get("weekdays_only")),
        "source_record_key":  grid_row["source_record_key"],
        "is_active":          True,
        "ris_system":         RIS_SYSTEM,
        "ris_sync_status":    "synced",
        "ris_metadata": {
            "writer":                       WRITER_NAME,
            "novaris_facility_id":          novaris_facility_id,
            "novaris_modality_dropdown_id": grid_row.get("novaris_modality_id"),
            "novaris_machine_name":         grid_row.get("name"),
            "novaris_facility_label":       grid_row.get("facility"),
        },
        "updated_at":         now_iso,
        "synced_at":          now_iso,
        "ris_last_synced_at": now_iso,
        "created_by":         None,
        "updated_by":         None,
    }
    for key in ("is_sunday", "is_monday", "is_tuesday", "is_wednesday",
                "is_thursday", "is_friday", "is_saturday"):
        rec[key] = bool(popup_detail.get(key))
    for d in range(1, 32):
        rec[f"day_{d}"] = bool(popup_detail.get(f"day_{d}"))

    rec["content_hash"] = compute_hash(rec)
    return rec


# ── Facility / modality lookup ─────────────────────────────────────────────

_FACILITY_ID_CACHE: dict = {}


def fetch_client_facility_names(supabase, client_id: int) -> list:
    """Return sorted list of pc1.facilities.facility_name where is_client=true."""
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
    """Look up pc1.facilities.id for (client_id, facility_name). Hard-fail if missing."""
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
            f"facility_name={facility_label!r}."
        )
    _FACILITY_ID_CACHE[cache_key] = row["id"]
    return row["id"]


def fetch_modality_machine_map(supabase, client_id: int, facility_id) -> dict:
    """
    Return {modality_machine: id} for active modalities at this facility.

    The exception scraper writes rows tied to specific machines; we
    look them up by machine name (the field common to NovaRIS's
    modality dropdown text and pc1.modalities.modality_machine).
    Filtered to is_active=true so soft-deleted machines don't capture
    new exception rows.
    """
    rows = (
        _table(supabase, "modalities")
        .select("id, modality_machine")
        .eq("client_id", client_id)
        .eq("facility_id", facility_id)
        .eq("is_active", True)
        .execute()
        .data or []
    )
    return {r["modality_machine"]: r["id"]
            for r in rows if r.get("modality_machine")}


# ── Supabase IO ────────────────────────────────────────────────────────────

def _fetch_existing_for_facility(supabase, client_id: int, facility_id) -> dict:
    """
    Paginated SELECT of every scraper-managed row for (client, facility) —
    rows where source_record_key IS NOT NULL. Manually-inserted rows
    (NULL source_record_key) are intentionally excluded.

    Returns dict keyed by source_record_key.
    """
    out = {}
    page_size = 1000
    start = 0
    while True:
        end = start + page_size - 1
        page = (
            _table(supabase, "scheduleexceptions")
            .select("id, source_record_key, content_hash, is_active, created_at")
            .eq("client_id", client_id)
            .eq("facility_id", facility_id)
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


def write_initial_load(supabase, facility_label: str, client_id: int,
                       facility_id, records: list, dry_run: bool):
    """
    Wipe scraper-managed rows for (client, facility), then bulk insert.
    Manually-inserted rows (source_record_key NULL) are preserved.
    """
    print(f"  [initial-load] {facility_label}: {len(records)} records",
          end="", flush=True)
    if dry_run:
        print(" (dry run, not saved).")
        return

    (
        _table(supabase, "scheduleexceptions")
        .delete()
        .eq("client_id", client_id)
        .eq("facility_id", facility_id)
        .not_.is_("source_record_key", "null")
        .execute()
    )

    for batch in _chunked(records, 200):
        for r in batch:
            r["created_at"] = r["updated_at"]
        _table(supabase, "scheduleexceptions").insert(batch).execute()

    print(" → wiped + inserted.")


def write_delta(supabase, facility_label: str, client_id: int, facility_id,
                records: list, dry_run: bool):
    """
    Delta sync per facility.

        - skip rows whose content_hash matches the DB (unchanged)
        - INSERT new rows in batches
        - UPDATE changed rows per-row (PostgREST upsert can't target
          our partial unique index)
        - UPDATE soft-deletes for keys missing from the scrape

    created_at is preserved on UPDATE (we copy it from the existing
    row into the payload). For INSERT it's set to now via the DEFAULT
    on the column.
    """
    by_key = _fetch_existing_for_facility(supabase, client_id, facility_id)

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
    print(f"  [delta] {facility_label}: {len(records)} scraped records  "
          f"insert={len(to_insert)}  update={updates} "
          f"(reactivated={reactivated})  unchanged={unchanged}"
          f"  deactivate={len(to_deactivate)}", end="")

    if dry_run:
        print(" (dry run, not applied).")
        return

    for batch in _chunked(to_insert, 200):
        _table(supabase, "scheduleexceptions").insert(batch).execute()

    for rid, payload in to_update:
        _table(supabase, "scheduleexceptions") \
            .update(payload) \
            .eq("id", rid) \
            .execute()

    now_iso = datetime.now(timezone.utc).isoformat()
    for batch in _chunked(to_deactivate, 500):
        _table(supabase, "scheduleexceptions").update(
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


# ── Driver ─────────────────────────────────────────────────────────────────

def scrape(args):
    global _QUIET
    _QUIET = args.quiet

    started_at = time.time()
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
    if USERNAME:
        if _QUIET:
            with contextlib.redirect_stdout(io.StringIO()):
                ok = login(session)
        else:
            ok = login(session)
        if not ok:
            print("ERROR: NovaRIS login failed.")
            sys.exit(1)

    vprint(f"GET {MODALITY_SCHEDULING_URL} ...")
    r = session.get(MODALITY_SCHEDULING_URL, timeout=30)
    r.raise_for_status()
    soup_initial = BeautifulSoup(r.text, "html.parser")

    facility_dd = (find_dropdown_name(soup_initial, "facilitiesDD")
                   or find_dropdown_name(soup_initial, "facilityDD")
                   or find_dropdown_name(soup_initial, "facilit"))
    modality_dd = (find_dropdown_name(soup_initial, "modalitiesDD")
                   or find_dropdown_name(soup_initial, "modalityDD")
                   or find_dropdown_name(soup_initial, "modalit"))
    if not facility_dd or not modality_dd:
        print(f"ERROR: could not find dropdowns. "
              f"facility={facility_dd!r}  modality={modality_dd!r}")
        sys.exit(1)

    script_manager = find_script_manager_name(soup_initial)
    update_panel_id = find_update_panel_id(soup_initial)
    vprint(f"  facility dropdown: {facility_dd!r}")
    vprint(f"  modality dropdown: {modality_dd!r}")
    vprint(f"  script manager:    {script_manager!r}")
    vprint(f"  update panel:      {update_panel_id!r}")

    runtime_facility_map = {f["name"]: f["id"]
                            for f in parse_dropdown_options(
                                soup_initial, "facilitiesDD")}
    if not runtime_facility_map:
        print(f"ERROR: NovaRIS facility dropdown {facility_dd!r} returned "
              f"no usable options.")
        sys.exit(1)
    vprint(f"  facility dropdown options: {len(runtime_facility_map)}")

    saved_grid = saved_popup = False
    missing_facilities: list = []
    unknown_machines_overall: list = []   # (facility, machine_name)
    # Single-element list so the pass-2 _fetch_one closures can
    # increment without needing nonlocal across the for-loop boundary.
    failed_popups_box = [0]
    grand_total = 0

    for facility_label in facilities:
        novaris_facility_id = runtime_facility_map.get(facility_label)
        if novaris_facility_id is None:
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

        facility_id = resolve_facility_id(supabase, client_id, facility_label)
        machine_map = fetch_modality_machine_map(supabase, client_id, facility_id)
        if not machine_map:
            print(f"  [{facility_label}] no active pc1.modalities rows — "
                  f"skipping.")
            continue

        # FRESH GET per facility to reset __VIEWSTATE — same VIEWSTATE-bloat
        # mitigation that PR #3 added to the procedures scraper. Without it,
        # accumulated grid state in __VIEWSTATE turns each subsequent POST
        # payload into megabytes and NovaRIS takes minutes to deserialize.
        fresh = session.get(MODALITY_SCHEDULING_URL, timeout=30)
        fresh.raise_for_status()
        form_state = extract_form_state_from_html(fresh.text)
        base_fields = extract_all_form_fields(fresh.text)

        vprint(f"  [{facility_label}] selecting facility "
               f"(NovaRIS id={novaris_facility_id}) ...", end=" ", flush=True)
        try:
            panel_html, base_fields = _post_dropdown_change(
                session, form_state, base_fields, facility_dd,
                novaris_facility_id, script_manager, update_panel_id,
            )
        except Exception as exc:
            print(f"ERROR selecting facility: {exc}")
            continue
        vprint("ok.")

        # Modality options now reflect the per-facility list. The legacy
        # exception scraper found the dropdown options inside the
        # UpdatePanel partial response, but if the panel doesn't include
        # the modality select, we fall back to a fresh page parse.
        modality_options = (parse_dropdown_options(panel_html, "modalitiesDD")
                            or parse_dropdown_options(panel_html, "modalit"))
        if not modality_options:
            # Some NovaRIS versions render the modality dropdown outside
            # the UpdatePanel; re-GET the page to grab it.
            r2 = session.get(MODALITY_SCHEDULING_URL, timeout=30)
            modality_options = (
                parse_dropdown_options(r2.text, "modalitiesDD")
                or parse_dropdown_options(r2.text, "modalit"))
        vprint(f"  [{facility_label}] modality options: {len(modality_options)}")

        # Filter modality options:
        # - drop names not present in pc1.modalities (skipped silently
        #   into unknown_machines_overall)
        # - if --modality is specified, restrict to substring match
        wanted_modality = args.modality.lower() if args.modality else ""
        scrape_targets = []
        unknown_for_this_facility: list = []
        for opt in modality_options:
            machine_name = opt["name"]
            if machine_name not in machine_map:
                unknown_for_this_facility.append(machine_name)
                continue
            if wanted_modality and wanted_modality not in machine_name.lower():
                continue
            scrape_targets.append({
                "novaris_modality_id": opt["id"],
                "machine_name":        machine_name,
                "modality_id":         machine_map[machine_name],
            })

        if unknown_for_this_facility:
            unknown_machines_overall.extend(
                (facility_label, m) for m in unknown_for_this_facility
            )
            vprint(f"  [{facility_label}] [debug] unknown machine names "
                   f"(not in pc1.modalities): "
                   f"{', '.join(unknown_for_this_facility)}")

        if not scrape_targets:
            print(f"  [{facility_label}] no machines to scrape "
                  f"(after pc1.modalities filter / --modality). Skipping.")
            continue

        # Pass 1 + Pass 2, per (facility, modality)
        all_records_for_facility = []
        for tgt in scrape_targets:
            label = f"{facility_label}/{tgt['machine_name']}"
            vprint(f"  [pass1] {label} "
                   f"(NovaRIS modID={tgt['novaris_modality_id']}) ...",
                   end=" ", flush=True)
            try:
                grid_html, base_fields = _post_dropdown_change(
                    session, form_state, base_fields, modality_dd,
                    tgt["novaris_modality_id"],
                    script_manager, update_panel_id,
                    # Re-send the facility value so the server keeps both
                    # dropdowns consistent on the modality postback.
                    extra_overrides={facility_dd: novaris_facility_id},
                )
            except Exception as exc:
                print(f"ERROR fetching grid: {exc}")
                continue

            if args.save_grid and not saved_grid:
                _write_debug_html(args.save_grid, grid_html)
                vprint(f"\n    [debug] grid saved to {args.save_grid!r}",
                       end="")
                saved_grid = True

            grid_rows = parse_grid(grid_html)
            # Defensive filter — the grid sometimes echoes rows from a
            # previous selection during partial postback transitions.
            grid_rows = [g for g in grid_rows
                         if g.get("name") == tgt["machine_name"]]
            if args.limit:
                grid_rows = grid_rows[:args.limit]
            vprint(f"{len(grid_rows)} rows")

            # Pass 2: parallel popup fetches for recurring rules only.
            popup_rows = [g for g in grid_rows
                          if g.get("recurrence") in ("Weekly", "Monthly", "Daily")]
            popups: dict = {}
            if popup_rows:
                workers = max(1, args.workers)

                def _fetch_one(fid):
                    try:
                        return fid, fetch_popup(session, fid)
                    except Exception as exc:
                        return fid, f"__ERROR__:{exc}"

                with ThreadPoolExecutor(max_workers=workers) as ex:
                    for fid, html_text in ex.map(
                        _fetch_one,
                        (g["source_record_key"] for g in popup_rows)
                    ):
                        if isinstance(html_text, str) and html_text.startswith("__ERROR__:"):
                            print(f"\n    WARN popup {fid}: {html_text[10:]}")
                            popups[fid] = ""
                            failed_popups_box[0] += 1
                        else:
                            popups[fid] = html_text
                            if args.save_popup and not saved_popup:
                                _write_debug_html(args.save_popup, html_text)
                                vprint(f"\n    [debug] popup saved to "
                                       f"{args.save_popup!r}", end="")
                                saved_popup = True

            now_iso = datetime.now(timezone.utc).isoformat()
            for g in grid_rows:
                if g.get("recurrence") in ("Weekly", "Monthly", "Daily"):
                    detail = parse_popup(
                        popups.get(g["source_record_key"], ""),
                        g.get("recurrence") or "")
                else:
                    detail = parse_popup("", g.get("recurrence") or "")
                all_records_for_facility.append(build_db_record(
                    g, detail, client_id, facility_id, tgt["modality_id"],
                    novaris_facility_id, now_iso,
                ))

        # Dedupe by source_record_key within the facility (defensive).
        unique = {}
        for rec in all_records_for_facility:
            unique[rec["source_record_key"]] = rec
        records = list(unique.values())

        if not records:
            print(f"  [{facility_label}] no records — skipping write.")
            continue

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
          f"Elapsed: {mm}m {ss}s  Rate: {rate:.1f}/s")

    if unknown_machines_overall:
        print(f"\nNOTE: {len(unknown_machines_overall)} NovaRIS machine "
              f"name(s) were not found in pc1.modalities — they were "
              f"skipped without writing exception rows:")
        for fac, mname in unknown_machines_overall:
            print(f"  - [{fac}] {mname!r}")

    failed_popups_total = failed_popups_box[0]
    partial = bool(missing_facilities) or failed_popups_total > 0

    if missing_facilities:
        print(
            f"\nWARNING: {len(missing_facilities)} facility(ies) skipped — "
            f"no match in NovaRIS dropdown: "
            f"{', '.join(missing_facilities)}. Update "
            f"pc1.facilities.facility_name for each, then re-run."
        )
    if failed_popups_total:
        print(f"WARNING: {failed_popups_total} popup fetches failed — "
              f"those rules were written with grid-only data (recurrence "
              f"mask defaults).")

    if partial:
        sys.exit(3)


DESCRIPTION = (
    "Scrape scheduling-exception rules from NovaRIS "
    "ModalityScheduling.aspx into pc1.scheduleexceptions. Modes: "
    "--initial-load (wipe scraper-managed rows per facility + bulk "
    "insert) or default delta (content-hash skip + insert/update + "
    "soft-delete)."
)


def main():
    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument("--initial-load", action="store_true",
                   help="For each scraped facility, wipe scraper-managed "
                        "rows (source_record_key IS NOT NULL) then bulk "
                        "insert. Manually-inserted rows are preserved.")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse only; do not write to Supabase. Still reads "
                        "existing rows for the delta breakdown.")
    p.add_argument("--facility", default="",
                   help="Limit to one facility (e.g. 'Inview-Fremont'). "
                        "Must match a pc1.facilities.facility_name AND "
                        "the live NovaRIS dropdown text exactly. Default "
                        "iterates every is_client=true facility.")
    p.add_argument("--modality", default="",
                   help="Limit to one machine. Case-insensitive substring "
                        "match against NovaRIS modality dropdown labels "
                        "(e.g. 'FRE-MRI', 'US').")
    p.add_argument("--limit", type=int, default=0,
                   help="At most N rules per (facility, modality). Use with "
                        "--dry-run for fast end-to-end testing.")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel popup fetches in pass 2 (default 6). "
                        "NovaRIS is single-threaded behind the popup "
                        "endpoint — pushing past ~8 workers triggers "
                        "timeouts without speeding up the run.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-modality progress chatter.")
    p.add_argument("--save-grid", default="",
                   help="Save the first grid HTML to FILE. Recommended "
                        "path: debug/<filename>.html (gitignored).")
    p.add_argument("--save-popup", default="",
                   help="Save the first popup HTML to FILE. Recommended "
                        "path: debug/<filename>.html (gitignored).")
    add_client_id_arg(p)
    args = p.parse_args()
    scrape(args)


if __name__ == "__main__":
    main()
