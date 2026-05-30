"""
reconcile_exceptions.py

Phase 3 writer for pc1.machineschedule. Reads active rules from
pc1.scheduleexceptions and reconciles them into the slot calendar:

    pc1.machineschedule.exceptions      = ['<desc> (H/S)', ...]
    pc1.machineschedule.exception_ids   = ['<source_record_key>', ...]
    pc1.machineschedule.availability    = updated per the rule below

The two text[] columns stay paired positionally (index N in
`exceptions` corresponds to index N in `exception_ids`). Both arrays
are sorted ascending by source_record_key for determinism, which
makes idempotent re-runs cheap to detect: rows whose target state
already matches are skipped server-side (no UPDATE issued).

Availability rule (pc1-specific)
--------------------------------
For each slot:

    if  start_time is OUTSIDE [opening_time, closing_time):
        # Slot is intrinsically out-of-business-hours.
        # Reconciler does NOT touch availability -- the generator
        # owns the out-of-hours invariant.
        availability remains whatever the row currently shows
    else:
        # Slot is in business hours.
        availability = 0 if (any Hard exception OR order_id NOT NULL)
                          else 1

`order_id IS NOT NULL` is treated as another blocker because a
slot held by an order isn't available for new bookings, full stop.
Without this rule, a future booking workflow that flips
availability=0 alongside order_id=N would silently get reverted on
the next reconciler run -- the reconciler would see "in-hours, no
Hard, availability != 1" and "fix" it. The Phase 4 booking
workstream depends on this.

This separation -- generator owns out-of-hours, reconciler owns
in-hours (exception + booking) state -- keeps each writer in its
lane and avoids the "did the reconciler accidentally re-open a
22:00 slot?" / "did the reconciler accidentally free up a booking?"
footguns. See docs/machineschedule.md -> "Exception overlay
invariants" for the documented rule.

The exceptions[] / exception_ids[] arrays are populated for EVERY
matching slot regardless of business hours -- so a LUNCH exception
that overlaps a 22:00 row still gets recorded in the array (useful
for the engine's future "why is this slot blocked?" reporting),
even though the slot's availability=0 was already set by the
generator.

Coverage
--------
- Recurrence types: None, Daily (with weekdays_only), Weekly (multi-
  day flags), Monthly (day_1..day_31).
- Exception types: Hard (H) reduces availability to 0 in-hours;
  Soft (S) is display-only and never changes availability.
- Past-dated slots (date_and_time_utc < today operator-local) are
  NEVER touched -- the clamp is applied to range_start.
- Re-runs are idempotent: rows whose target state already matches
  are skipped (no DB writes, no updated_at churn).

Multi-facility behavior
-----------------------
Default iterates every pc1.facilities row for the active tenant
where is_client = true. --facility=NAME restricts to a single
facility (ignoring is_client, same convention as the scrapers and
the generator).

Modality identity
-----------------
Matched by pc1.scheduleexceptions.modality_id -> pc1.machineschedule.modality_id
(bigint FK), not by the legacy text name join. Exception rows whose
modality_id IS NULL are skipped with a per-facility warning -- the
"cross-machine" semantic isn't exercised by the current NovaRIS
scraper, so implementing it is deferred.

Booked-slot Hard-exception conflict
-----------------------------------
When a slot has order_id IS NOT NULL AND the new state would carry
a Hard exception, the reconciler:
  - still writes the update normally (arrays + availability)
  - collects a CONFLICT record for the operator
  - prints a "CONFLICTS" section at the end of the run
  - exits with code 4 (distinct from 0=clean / 1=fatal / 3=partial)
The Phase 4 orders workstream is what populates order_id today, so
this code path is dormant until then. See the operator doc for a
manual SQL-driven test recipe.

Usage
-----
    python reconcile_exceptions.py
    python reconcile_exceptions.py --facility=Inview-Fremont
    python reconcile_exceptions.py --facility=Inview-Fremont --dry-run
    python reconcile_exceptions.py --start-date=2026-06-01 --days-ahead=30
    python reconcile_exceptions.py --workers=10

Exit codes
----------
    0  clean
    1  resolution failure (no client row, bad config)
    3  partial (some facilities skipped due to bad settings)
    4  booked-slot vs Hard-exception conflict detected (operator action
       required; the update was still applied)

Required .env
-------------
    SUPABASE_URL, SUPABASE_KEY
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import sys
import threading
import time as _time_module
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from supabase_client import get_supabase
from client_context import add_client_id_arg, resolve_client_id

# Identifier used only in log output. pc1.machineschedule has no
# ris_metadata column on purpose (slots aren't RIS-sourced), so
# this name is NOT written to the DB on any updated row.
WRITER_NAME = "reconcile_exceptions.py"

PC1_SCHEMA = "pc1"

# Python's date.weekday(): Monday=0 .. Sunday=6
WEEKLY_DAY_FLAGS = [
    "is_monday",     # 0
    "is_tuesday",    # 1
    "is_wednesday",  # 2
    "is_thursday",   # 3
    "is_friday",     # 4
    "is_saturday",   # 5
    "is_sunday",     # 6
]
WEEKDAYS_ONLY_SET = {0, 1, 2, 3, 4}
ALL_DAYS_SET      = {0, 1, 2, 3, 4, 5, 6}

# PostgREST paging / batching defaults. PAGE_SIZE matches the
# generator (1000); UPDATE_BATCH_SIZE matches the legacy reconciler
# (500). Both can be raised if Supabase rate limits aren't hit.
PAGE_SIZE         = 1000
UPDATE_BATCH_SIZE = 500

_QUIET = False


def vprint(*args, **kwargs):
    if not _QUIET:
        print(*args, **kwargs)


def _table(supabase, name: str):
    return supabase.schema(PC1_SCHEMA).table(name)


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── Parse helpers ───────────────────────────────────────────────────────────

def _to_date(s):
    if not s:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    return date.fromisoformat(str(s)[:10])


def _to_time(s):
    """Accept 'HH:MM[:SS]' or a `time` instance; return `time`."""
    if isinstance(s, time):
        return s
    if isinstance(s, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(s, fmt).time()
            except ValueError:
                continue
    raise ValueError(f"Unparseable time: {s!r}")


def _time_to_str(t: time) -> str:
    return t.strftime("%H:%M:%S")


# ── Settings resolution (facility overrides client) ────────────────────────

def _resolve_setting(facility_row: dict, client_row: dict, key: str):
    """Facility wins when non-NULL; otherwise client. Mirrors the
    generator's helper so generator and reconciler always agree on
    every per-slot parameter (especially slot_size)."""
    fac_val = facility_row.get(key)
    if fac_val is not None:
        return fac_val
    return client_row.get(key)


# ── Exception expansion ────────────────────────────────────────────────────

def _exception_dates(exc, range_start: date, range_end: date) -> list:
    """Return the sorted facility-local dates the exception covers in
    the inclusive [range_start, range_end] window. Ported from the
    legacy reconciler; recurrence semantics unchanged."""
    rec = exc.get("recurrence") or "None"
    sd = _to_date(exc.get("start_date"))
    if sd is None:
        return []
    eff_start = max(sd, range_start)
    eff_end = range_end
    exc_end = _to_date(exc.get("end_date"))
    if exc_end is not None:
        eff_end = min(exc_end, range_end)
    if eff_start > eff_end:
        return []

    if rec == "None":
        return [sd] if eff_start <= sd <= eff_end else []

    if rec == "Daily":
        target = WEEKDAYS_ONLY_SET if exc.get("weekdays_only") else ALL_DAYS_SET
        out = []
        d = eff_start
        while d <= eff_end:
            if d.weekday() in target:
                out.append(d)
            d += timedelta(days=1)
        return out

    if rec == "Weekly":
        target = {i for i, flag in enumerate(WEEKLY_DAY_FLAGS) if exc.get(flag)}
        if not target:
            return []
        out = []
        d = eff_start
        while d <= eff_end:
            if d.weekday() in target:
                out.append(d)
            d += timedelta(days=1)
        return out

    if rec == "Monthly":
        target_days = {i for i in range(1, 32) if exc.get(f"day_{i}")}
        if not target_days:
            return []
        out = []
        d = eff_start
        while d <= eff_end:
            if d.day in target_days:
                out.append(d)
            d += timedelta(days=1)
        return out

    return []


def _slot_times(start_t: time, end_t: time, slot_minutes: int) -> list:
    """'HH:MM:SS' slot starts in [start_t, end_t), stepping by
    slot_minutes. Same shape as the legacy helper."""
    out = []
    if start_t >= end_t:
        return out
    base = date(2000, 1, 1)
    cur = datetime.combine(base, start_t)
    end = datetime.combine(base, end_t)
    step = timedelta(minutes=slot_minutes)
    while cur < end:
        out.append(cur.time().strftime("%H:%M:%S"))
        cur += step
    return out


def _exc_type_marker(exc) -> str:
    t = (exc.get("type") or "").strip().lower()
    return "H" if t == "hard" else "S"


def _format_entry(desc: str, marker: str) -> str:
    return f"{(desc or '').strip()} ({marker})".strip()


# ── Supabase reads ─────────────────────────────────────────────────────────

def _fetch_client_row(supabase, client_id: int) -> dict:
    row = (
        _table(supabase, "clients")
        .select("id, client_name, slot_size, opening_time, closing_time, "
                "advance_booking_days, timezone")
        .eq("id", client_id)
        .limit(1)
        .execute()
        .data or [None]
    )[0]
    if row is None:
        raise RuntimeError(
            f"No pc1.clients row for id={client_id}. Register the "
            f"tenant before running the reconciler."
        )
    return row


def _fetch_facility_rows(supabase, client_id: int,
                         facility_name: str | None) -> list:
    q = (
        _table(supabase, "facilities")
        .select("id, facility_name, slot_size, advance_booking_days, "
                "opening_time, closing_time, timezone, is_client")
        .eq("client_id", client_id)
    )
    if facility_name:
        q = q.eq("facility_name", facility_name)
    else:
        q = q.eq("is_client", True)
    rows = q.execute().data or []
    return sorted(rows, key=lambda r: r["facility_name"] or "")


def _fetch_modality_machine_map(supabase, client_id: int,
                                facility_id: int) -> dict:
    """Return {modality_id: modality_machine_text} for log output
    only. The reconciler matches by FK, not by name, so this map is
    purely for human-readable warning messages."""
    rows = (
        _table(supabase, "modalities")
        .select("id, modality_machine")
        .eq("client_id", client_id)
        .eq("facility_id", facility_id)
        .execute()
        .data or []
    )
    return {r["id"]: r.get("modality_machine") for r in rows}


# Columns required by _exception_dates / build_desired_state. Listed
# explicitly so the PostgREST SELECT is tight and we don't pull
# unused fields per row.
_EXC_BASE_COLS = (
    "id, source_record_key, modality_id, modality_type, description, "
    "type, recurrence, start_date, end_date, start_time, end_time, "
    "weekdays_only"
)


def _fetch_active_exceptions(supabase, client_id: int, facility_id: int,
                             range_start: date) -> list:
    """Active exceptions whose effective window may intersect
    [range_start, +inf): end_date IS NULL OR end_date >= range_start.
    Pulls BOTH modality_id IS NOT NULL and IS NULL rows; the caller
    splits and warns on the latter."""
    iso = range_start.isoformat()
    select_cols = (
        _EXC_BASE_COLS + ", "
        + ", ".join(WEEKLY_DAY_FLAGS) + ", "
        + ", ".join(f"day_{i}" for i in range(1, 32))
    )
    rows = (
        _table(supabase, "scheduleexceptions")
        .select(select_cols)
        .eq("client_id", client_id)
        .eq("facility_id", facility_id)
        .eq("is_active", True)
        .or_(f"end_date.is.null,end_date.gte.{iso}")
        .execute()
        .data or []
    )
    return rows


def _fetch_machineschedule_rows(supabase, client_id: int, facility_id: int,
                                range_start: date, range_end: date,
                                facility_tz: ZoneInfo) -> list:
    """Paginate machineschedule rows for [range_start, range_end]
    (facility-local, inclusive). Converts day boundaries to UTC for
    the date_and_time_utc filter so late-evening rows aren't shifted
    by the facility's UTC offset."""
    start_utc = datetime.combine(range_start, time(0)).replace(
        tzinfo=facility_tz).astimezone(timezone.utc).isoformat()
    end_exclusive_utc = datetime.combine(
        range_end + timedelta(days=1), time(0)
    ).replace(tzinfo=facility_tz).astimezone(timezone.utc).isoformat()
    out = []
    offset = 0
    while True:
        page = (
            _table(supabase, "machineschedule")
            .select("id, modality_id, date_and_time_utc, start_time, "
                    "exceptions, exception_ids, availability, order_id")
            .eq("client_id", client_id)
            .eq("facility_id", facility_id)
            .gte("date_and_time_utc", start_utc)
            .lt("date_and_time_utc", end_exclusive_utc)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
            .data or []
        )
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


# ── Desired-state builder ──────────────────────────────────────────────────

def build_desired_state(
    exceptions: list, range_start: date, range_end: date,
    slot_minutes: int,
) -> tuple[dict, dict, list]:
    """For every (modality_id, date, time_str) slot covered by any
    active in-scope exception, record the list of
    (source_record_key, description, marker) tuples.

    Returns:
        desired: {(modality_id, date_obj, "HH:MM:SS"):
                  [(key, desc, marker), ...]}
        per_exc_counts: {<source_record_key>: int slot count}
            -- for the dry-run "expansion summary" output
        skipped_null_modality: list of source_record_key strings
            whose modality_id was NULL (warning, not applied)
    """
    desired = defaultdict(list)
    per_exc_counts: dict = defaultdict(int)
    skipped_null_modality: list = []

    for exc in exceptions:
        modality_id = exc.get("modality_id")
        key = str(exc.get("source_record_key") or exc.get("id") or "")
        if not key:
            continue
        if modality_id is None:
            skipped_null_modality.append(key)
            continue
        try:
            start_t = _to_time(exc["start_time"])
            end_t = _to_time(exc["end_time"])
        except (KeyError, ValueError):
            continue
        slot_times = _slot_times(start_t, end_t, slot_minutes)
        if not slot_times:
            continue
        marker = _exc_type_marker(exc)
        desc = exc.get("description") or ""
        for d in _exception_dates(exc, range_start, range_end):
            for t_str in slot_times:
                desired[(modality_id, d, t_str)].append((key, desc, marker))
                per_exc_counts[key] += 1
    return desired, per_exc_counts, skipped_null_modality


# ── Per-row reconciliation ─────────────────────────────────────────────────

def reconcile_row(
    row: dict, desired_for_slot: list,
    opening_time: time, closing_time: time, slot_start_t: time,
) -> dict | None:
    """Compute the row's target state and return an UPDATE payload if
    anything differs from current, else None.

    Availability rule (pc1):
      * If slot_start_t is outside [opening_time, closing_time),
        availability stays whatever the row currently shows -- the
        reconciler does not own the out-of-hours invariant.
      * Otherwise (in business hours): 0 if EITHER any Hard marker
        is present OR the slot is booked (order_id IS NOT NULL),
        else 1.

    Treating order_id IS NOT NULL as a blocker mirrors the booking
    code's mental model ("this slot is held; it can't be offered")
    and prevents the reconciler from silently freeing up booked
    slots when no Hard exception covers them. Phase 4 booking
    workflow depends on this rule.
    """
    desired_sorted = sorted(desired_for_slot, key=lambda x: x[0])
    new_ids   = [k for (k, _d, _m) in desired_sorted]
    new_excs  = [_format_entry(d, m) for (_k, d, m) in desired_sorted]

    in_hours = opening_time <= slot_start_t < closing_time
    has_hard = any(m == "H" for (_k, _d, m) in desired_sorted)
    has_order = row.get("order_id") is not None
    if in_hours:
        new_avail = 0 if (has_hard or has_order) else 1
    else:
        # Don't touch availability -- generator owns this invariant.
        new_avail = row.get("availability")

    cur_ids   = list(row.get("exception_ids") or [])
    cur_excs  = list(row.get("exceptions") or [])
    cur_avail = row.get("availability")

    if cur_ids == new_ids and cur_excs == new_excs and cur_avail == new_avail:
        return None
    return {
        "id":            row["id"],
        "exceptions":    new_excs,
        "exception_ids": new_ids,
        "availability":  new_avail,
    }


# ── Update apply (thread-pooled, payload-grouped) ─────────────────────────

# Thread-local Supabase clients. The httpx-backed client is NOT safe
# to share across threads (manifests as Win10035 / "ReadError: non-
# blocking socket" with HTTP/2). Each worker thread gets its own
# client on first use.
_thread_local = threading.local()


def _get_thread_client(shared):
    if threading.current_thread() is threading.main_thread():
        return shared
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = get_supabase()
        _thread_local.client = client
    return client


def _execute_with_retry(call, max_retries: int = 3, base_sleep: float = 0.5):
    """Run `call` with exponential-backoff retry on transient errors."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return call()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            _time_module.sleep(base_sleep * (2 ** attempt))
    raise last_exc  # pragma: no cover


def apply_updates(supabase, updates: list, dry_run: bool, workers: int):
    """Group rows by payload, batch IDs into UPDATE_BATCH_SIZE chunks,
    dispatch via parallel workers. Many rows in the same exception's
    coverage share an identical (exceptions, exception_ids,
    availability) payload, so payload-grouping reduces request count
    dramatically.
    """
    if dry_run or not updates:
        return

    groups = defaultdict(list)
    for u in updates:
        rid = u.pop("id")
        key = (
            tuple(u["exceptions"]),
            tuple(u["exception_ids"]),
            u["availability"],
        )
        groups[key].append(rid)

    now_iso = datetime.now(timezone.utc).isoformat()
    batches = []
    for (excs, eids, avail), ids in groups.items():
        # updated_by deliberately left NULL -- pc1 convention for
        # automation writes; the synchronized updated_at across many
        # rows lets an operator identify the reconciler's runs.
        payload = {
            "exceptions":    list(excs),
            "exception_ids": list(eids),
            "availability":  avail,
            "updated_at":    now_iso,
        }
        for chunk_start in range(0, len(ids), UPDATE_BATCH_SIZE):
            batches.append(
                (payload, ids[chunk_start: chunk_start + UPDATE_BATCH_SIZE])
            )

    workers_eff = max(1, min(workers, len(batches)))
    vprint(f"    applying {len(batches)} batched updates "
           f"(across {len(groups)} distinct payloads, "
           f"{workers_eff} parallel workers) ...")

    def _run_batch(args):
        payload, ids = args
        client = _get_thread_client(supabase)
        _execute_with_retry(
            lambda: _table(client, "machineschedule")
                    .update(payload)
                    .in_("id", ids)
                    .execute()
        )

    if workers_eff == 1:
        for b in batches:
            _run_batch(b)
        return
    with ThreadPoolExecutor(max_workers=workers_eff) as ex:
        for _ in ex.map(_run_batch, batches):
            pass


# ── Per-facility driver ────────────────────────────────────────────────────

def reconcile_facility(
    supabase, client_row: dict, facility_row: dict,
    args, conflicts_collector: list,
) -> tuple[int, bool]:
    """Reconcile one facility. Returns (rows_updated, was_skipped).
    Appends booked-slot conflicts to `conflicts_collector`."""
    client_id     = client_row["id"]
    facility_id   = facility_row["id"]
    facility_name = facility_row["facility_name"]

    slot_size = int(_resolve_setting(facility_row, client_row, "slot_size"))
    tz_name   = _resolve_setting(facility_row, client_row, "timezone")
    try:
        facility_tz = ZoneInfo(tz_name)
    except Exception:
        print(f"  [{facility_name}] ERROR: timezone={tz_name!r} is not "
              f"a recognized IANA name. Skipping facility.")
        return 0, True

    try:
        opening_time = _to_time(_resolve_setting(
            facility_row, client_row, "opening_time"
        ))
        closing_time = _to_time(_resolve_setting(
            facility_row, client_row, "closing_time"
        ))
    except (TypeError, ValueError) as exc:
        print(f"  [{facility_name}] ERROR: opening_time / closing_time "
              f"could not be parsed ({exc}). Skipping facility.")
        return 0, True
    if opening_time >= closing_time:
        print(f"  [{facility_name}] ERROR: opening_time={opening_time} "
              f">= closing_time={closing_time}. Skipping facility.")
        return 0, True

    # Window resolution -- same shape as the generator.
    today_local = datetime.now(facility_tz).date()
    if args.start_date:
        try:
            requested_start = date.fromisoformat(args.start_date)
        except ValueError:
            print(f"  [{facility_name}] ERROR: --start-date="
                  f"{args.start_date!r} is not YYYY-MM-DD. Skipping facility.")
            return 0, True
    else:
        requested_start = today_local

    # Past-date clamp -- the reconciler NEVER modifies past slots,
    # regardless of --start-date. The clamp is applied here, not at
    # argparse, so a single --start-date earlier than today still
    # produces a non-empty window for tomorrow onward instead of
    # crashing.
    range_start = max(requested_start, today_local)

    if args.days_ahead is not None:
        days_ahead = int(args.days_ahead)
    else:
        days_ahead = int(_resolve_setting(
            facility_row, client_row, "advance_booking_days"
        ))
    if days_ahead <= 0:
        print(f"  [{facility_name}] ERROR: days_ahead={days_ahead} <= 0. "
              f"Skipping facility.")
        return 0, True
    range_end = range_start + timedelta(days=days_ahead)

    vprint(f"  [{facility_name}] tz={tz_name}  slot_size={slot_size}min  "
           f"hours=[{opening_time}, {closing_time})  "
           f"range={range_start} -> {range_end} "
           f"({(range_end - range_start).days + 1} days)  "
           f"({'DRY RUN' if args.dry_run else 'WRITE'})")

    # Fetch the inputs.
    exceptions = _fetch_active_exceptions(
        supabase, client_id, facility_id, range_start
    )
    desired, per_exc_counts, skipped_null = build_desired_state(
        exceptions, range_start, range_end, slot_size
    )
    if skipped_null:
        print(f"  [{facility_name}] WARNING: skipped "
              f"{len(skipped_null)} exception(s) with modality_id IS NULL "
              f"(cross-machine rules not yet supported). source_record_keys: "
              f"{', '.join(skipped_null[:5])}"
              f"{'...' if len(skipped_null) > 5 else ''}")
    vprint(f"  [{facility_name}]   active exceptions={len(exceptions)} "
           f"(skipped_null_modality={len(skipped_null)})  "
           f"desired-state entries={sum(len(v) for v in desired.values())} "
           f"across {len(desired)} unique slots")

    # Dry-run per-exception expansion summary (the user-requested
    # sanity-check view).
    if args.dry_run and per_exc_counts:
        machine_map = _fetch_modality_machine_map(
            supabase, client_id, facility_id
        )
        exc_label = {}
        for exc in exceptions:
            k = str(exc.get("source_record_key") or exc.get("id") or "")
            if k:
                desc = (exc.get("description") or "").strip() or "<no desc>"
                marker = _exc_type_marker(exc)
                mod_name = machine_map.get(exc.get("modality_id"), "?")
                exc_label[k] = f"{desc} ({marker}) on {mod_name}"
        vprint(f"  [{facility_name}]   DRY-RUN exception expansion:")
        for k in sorted(per_exc_counts, key=lambda x: -per_exc_counts[x]):
            vprint(f"    {exc_label.get(k, k):<60s} -> "
                   f"{per_exc_counts[k]:>6,d} slots affected")

    rows = _fetch_machineschedule_rows(
        supabase, client_id, facility_id, range_start, range_end, facility_tz
    )
    vprint(f"  [{facility_name}]   machineschedule rows in range: {len(rows)}")

    # Per-row reconciliation. We split outputs into add/change vs
    # removal-only batches because legacy ran them sequentially for
    # clearer logging; nothing in the apply path requires the split,
    # but it's a useful operator signal ("did this run mostly add
    # rules, or mostly clean up after a delete?").
    updates_add_or_change: list = []
    updates_remove_only:   list = []
    machine_map_for_warn: dict = {}

    for row in rows:
        try:
            slot_start_t = _to_time(row["start_time"])
        except (KeyError, ValueError):
            continue
        slot_key = (
            row["modality_id"],
            datetime.fromisoformat(
                row["date_and_time_utc"]
            ).astimezone(facility_tz).date(),
            slot_start_t.strftime("%H:%M:%S"),
        )
        desired_for_slot = desired.get(slot_key, [])
        update = reconcile_row(
            row, desired_for_slot,
            opening_time, closing_time, slot_start_t,
        )
        if update is None:
            continue

        # Booked-slot Hard-exception conflict detection. Fires only
        # when a Hard marker is part of the NEW state AND order_id is
        # populated -- both are necessary for the conflict to be
        # meaningful. The update still proceeds; the operator sees the
        # conflict in the summary at the end of the run.
        if row.get("order_id") is not None and any(
            entry.endswith(" (H)") or entry.endswith("(H)")
            for entry in update["exceptions"]
        ):
            if not machine_map_for_warn:
                machine_map_for_warn = _fetch_modality_machine_map(
                    supabase, client_id, facility_id
                )
            conflicts_collector.append({
                "facility_name":     facility_name,
                "slot_id":           row["id"],
                "order_id":          row["order_id"],
                "modality_id":       row["modality_id"],
                "modality_machine":  machine_map_for_warn.get(row["modality_id"]),
                "date_and_time_utc": row["date_and_time_utc"],
                "start_time_local":  row["start_time"],
                "hard_exceptions":   [
                    e for e in update["exceptions"]
                    if e.endswith("(H)") or e.endswith(" (H)")
                ],
            })

        if not desired_for_slot and (row.get("exception_ids") or []):
            updates_remove_only.append(update)
        else:
            updates_add_or_change.append(update)

    total = len(updates_add_or_change) + len(updates_remove_only)
    vprint(f"  [{facility_name}]   updates -- add/change="
           f"{len(updates_add_or_change)}  removal-only="
           f"{len(updates_remove_only)}")
    apply_updates(supabase, updates_add_or_change, args.dry_run, args.workers)
    apply_updates(supabase, updates_remove_only,   args.dry_run, args.workers)
    if args.dry_run:
        print(f"  [{facility_name}] DRY RUN -- would update {total} slots.")
    else:
        print(f"  [{facility_name}] DONE -- {total} slots updated.")
    return total, False


# ── Driver ─────────────────────────────────────────────────────────────────

def reconcile(args):
    global _QUIET
    _QUIET = args.quiet

    client_id = resolve_client_id(args)
    supabase  = get_supabase()

    client_row = _fetch_client_row(supabase, client_id)
    facilities = _fetch_facility_rows(
        supabase, client_id, args.facility or None
    )
    if not facilities:
        if args.facility:
            print(f"ERROR: no pc1.facilities row for client_id={client_id}, "
                  f"facility_name={args.facility!r}.")
        else:
            print(f"ERROR: no is_client=true pc1.facilities rows for "
                  f"client_id={client_id}.")
        sys.exit(1)

    print(f"client_id={client_id} ({client_row.get('client_name')})  "
          f"facilities={len(facilities)}  "
          f"{'DRY RUN' if args.dry_run else 'WRITE'}  "
          f"workers={args.workers}")

    started_at = _time_module.time()
    grand_total = 0
    skipped: list = []
    conflicts: list = []

    for fac in facilities:
        total, was_skipped = reconcile_facility(
            supabase, client_row, fac, args, conflicts
        )
        if was_skipped:
            skipped.append(fac.get("facility_name") or "<unknown>")
        grand_total += total

    elapsed = _time_module.time() - started_at
    mm, ss  = divmod(int(elapsed), 60)
    print(f"\nDone. Total slots updated: {grand_total:,}  "
          f"Elapsed: {mm}m {ss}s")

    # ── CONFLICTS section (booked-slot vs Hard exception) ─────────────
    if conflicts:
        print(f"\nCONFLICTS -- booked slots affected by new Hard exceptions")
        print(  "=========================================================")
        print(f"  {len(conflicts)} slot(s) need manual rescheduling "
              f"before exceptions take effect:")
        for c in conflicts:
            print(f"  - slot_id={c['slot_id']} order_id={c['order_id']} "
                  f"modality_id={c['modality_id']} ({c['modality_machine']}) "
                  f"at {c['date_and_time_utc']} "
                  f"(local {c['start_time_local']}) -- conflicts with: "
                  f"{', '.join(c['hard_exceptions'])}")
        print("\nACTION REQUIRED: re-book these orders to non-conflicting "
              "slots, then re-run the reconciler.")

    exit_code = 0
    if skipped:
        print(f"\nWARNING: {len(skipped)} facility(ies) skipped: "
              f"{', '.join(skipped)}")
        exit_code = 3
    if conflicts:
        exit_code = 4
    sys.exit(exit_code)


DESCRIPTION = (
    "Reconcile pc1.scheduleexceptions into pc1.machineschedule. "
    "Computes per-slot exceptions[] / exception_ids[] / availability, "
    "writes only changed rows. Default iterates all is_client=true "
    "facilities; --facility=NAME limits to one. Idempotent."
)


def main():
    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument("--facility", default="",
                   help="Limit to one facility (e.g. 'Inview-Fremont'). "
                        "Default iterates every is_client=true facility "
                        "for the tenant.")
    p.add_argument("--start-date", default="",
                   help="Inclusive start date YYYY-MM-DD (facility-local). "
                        "Default: today. Clamped to today regardless of "
                        "value -- past slots are never modified.")
    p.add_argument("--days-ahead", type=int, default=None,
                   help="Window length in days. Default: facility's "
                        "advance_booking_days (override over client).")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute updates without writing; print a per-"
                        "exception expansion summary.")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel UPDATE workers (default 6).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-facility progress chatter.")
    add_client_id_arg(p)
    args = p.parse_args()
    reconcile(args)


if __name__ == "__main__":
    main()
