"""
novaRIS_common.py

Shared module for everything specific to talking to NovaRIS at the
HTTP level. This is the "how do we communicate with NovaRIS" toolkit
that every NovaRIS scraper imports.

Two categories of content live here:

  1. NovaRIS protocol metadata
     - BASE_URL, USERNAME, PASSWORD, LOGIN_URL
     - MODALITY_MAP            (NovaRIS modality ID → modality_type, machine_name)
     - FACILITY_MODALITIES     (which modality IDs apply per facility)

  Note: there is intentionally NO FACILITY_MAP here. Each scraper looks
  facility IDs up from the live NovaRIS page (e.g. the
  ViewModalities.aspx facility dropdown) at run time and matches them
  to `pc1.facilities.facility_name` by exact string. That keeps PC1
  RIS-agnostic and means onboarding a facility is a single INSERT into
  `pc1.facilities` — no code change.

  2. ASP.NET WebForms helpers
     - login(session)
     - extract_form_state_from_html(html)
     - extract_all_form_fields(html)
     - update_form_state_from_updatepanel(response_text, state)
     - _parse_updatepanel_segments(text)

Why this is its own module
--------------------------
Originally these lived in `novaRIS_scraper.py`, which doubled as both
the slot/calendar scraper AND the shared-helpers home. That made the
filename misleading (other scrapers were importing from a "scraper"
file just to get login + config) and it tied the helpers' lifecycle
to a scraper that turned out to be non-functional. Splitting them out:

  - Working scrapers (exception, modalities, standardprocedure) import
    from `novaRIS_common.py`.
  - The (non-functional) scheduling scraper lives separately as
    `novaRIS_scheduling_scraper.py` with an EXPERIMENTAL banner — and
    also imports from this module rather than re-exporting helpers.

NovaRIS-specific by design
--------------------------
The constants here are NovaRIS protocol metadata, not business data
about your facilities or equipment. (Facility names and machines
themselves live in the `clients` and `modalitymachine` Supabase
tables.) If a future tenant uses a different RIS, they'd get their
own analog: `<theirRIS>_common.py` with their connection details and
internal IDs.
"""

from dotenv import load_dotenv
load_dotenv()

import os
import re

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ── NovaRIS connection / endpoint config ────────────────────────────────────

BASE_URL  = os.getenv("NOVARISURL", "https://pacs2.inviewimaging.com/NovaRIS").rstrip("/")
USERNAME  = os.getenv("NOVARISUSER", "")
PASSWORD  = os.getenv("NOVARISPASSWORD", "")
LOGIN_URL = f"{BASE_URL}/Login.aspx"


# ── NovaRIS modality lookup tables ──────────────────────────────────────────

# MODALITY_MAP:  NovaRIS modalitiesDD value  →  (modality_type, machine_name)
#
# modality_type must match what your orders use (MRI, CT, US, DX, MG, OT, …).
# Each entry corresponds to one physical machine across all facilities.
# IDs are machine-specific in NovaRIS — same modality type has different IDs
# per facility (e.g. MRI-L at Lafayette=4, FRE-MRI at Fremont=1).
MODALITY_MAP = {
    # ── Inview-Lafayette ──────────────────────────────────────────────────────
    "4":   ("MRI", "MRI-L"),
    "394": ("US",  "US-LAF 2"),
    "6":   ("CR",  "CR-L"),
    "401": ("DX",  "DX"),
    "7":   ("MG",  "MG-L"),
    "8":   ("OT",  "Dexa-L"),
    "5":   ("US",  "US-L"),

    # ── Inview-Fremont (IDs confirmed via --discover initial page) ────────────
    "1":    ("MRI", "FRE-MRI"),
    "2":    ("US",  "US1-F"),
    "3":    ("CT",  "FRE-CT"),
    "42":   ("DX",  "DX-FRE"),
    "43":   ("MG",  "MG-FRE"),
    "94":   ("OT",  "Dexa-F"),
    "1183": ("US",  "US2-F"),
    "1202": ("DX",  "DX-2"),    # second DX machine at Fremont

    # ── Inview-Oakland ────────────────────────────────────────────────────────
    "366": ("CT",  "CT-O"),
    "424": ("OT",  "DEXA"),
    "12":  ("OT",  "Dexa-O"),
    "11":  ("MG",  "MG-O"),
    "39":  ("US",  "US 1"),
    "10":  ("US",  "US 2"),
    "9":   ("DX",  "XR-O"),

    # ── Inview-Concord ────────────────────────────────────────────────────────
    "475": ("MRI", "MR-CONCORD"),
    "474": ("US",  "US-CONCORD"),

    # ── Antioch Medical Imaging ───────────────────────────────────────────────
    "21":  ("CR",  "CR-AMI"),
    "23":  ("CT",  "CT-AMI"),
    "24":  ("OT",  "Dexa-AMI"),
    "256": ("DX",  "DX-AMI"),
    "18":  ("MG",  "MG-AMI"),
    "22":  ("MRI", "MR-AMI"),
    "386": ("PT",  "PET-CT"),
    "19":  ("US",  "US1-AMI"),
    "20":  ("US",  "US2-AMI"),

    # ── CA-IMI Facility ───────────────────────────────────────────────────────
    "33":  ("MG",  "MG"),

    # ── PET IMAGING OF SJ ─────────────────────────────────────────────────────
    "429": ("CT",  "CT"),
    "435": ("NM",  "NM"),
    "434": ("PT",  "PET-CT"),

    # ── San Ramon Bay Radiology ───────────────────────────────────────────────
    "37":  ("MRI", "MR-SR"),
    "81":  ("US",  "US-SR"),
    "104": ("DX",  "XR-SR"),
}

# Which modality IDs are available at each facility.
# Trimmed to the machines actually carrying scheduling data — UNKNOWN-named
# entries and inactive duplicates from --discover are intentionally omitted.
FACILITY_MODALITIES = {
    "Inview-Lafayette":         ["4", "394", "6", "401", "7", "8", "5"],
    "Inview-Fremont":           ["1", "2", "3", "42", "43", "94", "1183", "1202"],
    "Inview-Oakland":           ["366", "424", "12", "11", "39", "10", "9"],
    "Inview-Concord":           ["475", "474"],
    "Antioch Medical Imaging":  ["21", "23", "24", "256", "18", "22", "386", "19", "20"],
    "CA-IMI Facility":          ["33"],
    "PET IMAGING OF SJ":        ["429", "435", "434"],
    "San Ramon Bay Radiology":  ["37", "81", "104"],
}


# ── HTML / form-state helpers ───────────────────────────────────────────────

def _hidden(soup, name: str) -> str:
    tag = soup.find("input", {"name": name})
    return tag["value"] if tag and tag.get("value") else ""


def extract_all_form_fields(html: str) -> dict:
    """
    Extract every submittable form field from a full HTML page.

    Mirrors exactly what the browser sends on a form submit — including
    all hidden inputs, checked checkboxes, and selected dropdown values.
    Needed because some NovaRIS handlers (e.g. the auto-refresh timer)
    read several less-obvious hidden fields and throw an ASP.NET
    exception if any are missing from a postback.
    """
    soup = BeautifulSoup(html, "html.parser")
    fields = {}

    for inp in soup.find_all("input"):
        name = inp.get("name", "")
        if not name:
            continue
        t = (inp.get("type") or "text").lower()
        if t == "submit":
            continue
        if t == "checkbox":
            if inp.get("checked") is not None:
                fields[name] = inp.get("value", "on")
            # unchecked checkboxes are not submitted
        elif t == "radio":
            if inp.get("checked") is not None:
                fields[name] = inp.get("value", "")
        else:
            fields[name] = inp.get("value", "")

    for sel in soup.find_all("select"):
        name = sel.get("name", "")
        if not name:
            continue
        selected = sel.find("option", {"selected": True})
        if not selected:
            selected = sel.find("option")
        fields[name] = selected.get("value", "") if selected else ""

    return fields


def extract_form_state_from_html(html: str) -> dict:
    """Parse VIEWSTATE and Anti-XSRF token from a full HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    return {
        "__VIEWSTATE":          _hidden(soup, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _hidden(soup, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION":    _hidden(soup, "__EVENTVALIDATION"),
        "ctl00$AntiXsrToken":   _hidden(soup, "ctl00$AntiXsrToken"),
    }


def update_form_state_from_updatepanel(response_text: str, state: dict) -> None:
    """
    Mutate `state` with fresh VIEWSTATE / AntiXsrToken pulled from an
    ASP.NET UpdatePanel partial-postback response.

    UpdatePanel format:  length|type|id|value|…
    hiddenField segments carry updated hidden-field values.
    """
    for m in re.finditer(
        r'\d+\|hiddenField\|'
        r'(__VIEWSTATE(?:GENERATOR)?|ctl00\$AntiXsrToken)'
        r'\|([^|]*)\|',
        response_text,
    ):
        state[m.group(1)] = m.group(2)


def _parse_updatepanel_segments(text: str) -> list:
    """
    Parse an ASP.NET UpdatePanel partial-postback response into
    (type, id, content) tuples.

    The format is:  N|type|id|content|N|type|id|content|...
    where N is the character count of `content`. Because content can
    itself contain pipe characters, we MUST use the length prefix —
    not splitting on '|' — to locate segment boundaries.
    """
    segments = []
    pos = 0
    while pos < len(text):
        pipe1 = text.find("|", pos)
        if pipe1 == -1:
            break
        try:
            length = int(text[pos:pipe1])
        except ValueError:
            break

        pipe2 = text.find("|", pipe1 + 1)
        pipe3 = text.find("|", pipe2 + 1)
        if pipe2 == -1 or pipe3 == -1:
            break

        seg_type = text[pipe1 + 1 : pipe2]
        seg_id   = text[pipe2 + 1 : pipe3]

        content_start = pipe3 + 1
        content_end   = content_start + length
        content       = text[content_start:content_end]

        segments.append((seg_type, seg_id, content))
        pos = content_end + 1   # skip trailing '|' separator

    return segments


# ── Session factory ─────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    """Build a `requests.Session` with retry on transient HTTP errors.

    Retries up to 3 times with exponential backoff on 502 / 503 / 504
    and on connection-reset errors — the failure modes we've seen
    against NovaRIS under intermittent load. POST is retried
    because NovaRIS's login + facility-switch postbacks are
    idempotent at the application layer (re-sending the same payload
    just re-renders the same page).
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ── Authentication ──────────────────────────────────────────────────────────

def login(session: requests.Session) -> bool:
    """Form-based login to NovaRIS. Returns True on success."""
    r = session.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    state = extract_form_state_from_html(r.text)

    soup = BeautifulSoup(r.text, "html.parser")
    # Find the login button name — it varies across NovaRIS versions
    btn = soup.find("input", {"type": "submit"})
    btn_name  = btn["name"]  if btn else "ctl00$ContentPlaceHolder1$LoginBtn"
    btn_value = btn["value"] if btn else "Login"

    # Find username / password field names
    user_field = "ctl00$ContentPlaceHolder1$UserNameTxt"
    pass_field = "ctl00$ContentPlaceHolder1$PasswordTxt"
    for inp in soup.find_all("input", {"type": ["text", "email"]}):
        if "user" in (inp.get("name") or "").lower() or "user" in (inp.get("id") or "").lower():
            user_field = inp["name"]
    for inp in soup.find_all("input", {"type": "password"}):
        pass_field = inp["name"]

    payload = {
        "__VIEWSTATE":          state["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": state["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION":    state.get("__EVENTVALIDATION", ""),
        user_field:             USERNAME,
        pass_field:             PASSWORD,
        btn_name:               btn_value,
    }

    r = session.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
    # NovaRIS redirects to an app page on success and back to
    # Login.aspx on failure. Anything that ISN'T Login.aspx means
    # we landed in the app — covers Scheduling.aspx, Default.aspx,
    # Home.aspx, and any future landing page.
    if "Login" in r.url:
        print("ERROR: Login failed — check NOVARISUSER / NOVARISPASSWORD in .env")
        return False
    print("Login successful.")
    return True
