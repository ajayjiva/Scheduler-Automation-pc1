"""
Post-processing filters for appointment options + slot-row pre-filters
(Phase 3.6 pc1 port).

Option filters (dedupe_options, pareto_prune, filter_by_time_of_day,
filter_blocks_by_tod) operate on option/block dicts and are unchanged.

Slot-row filters (filter_rows_by_day_of_week, filter_rows_by_month) read the
slot instant; pc1 change: they read `date_and_time_utc` and convert to
facility-local before extracting weekday / month.
"""

from datetime import datetime

from tz_helpers import parse_slot_dt, to_local, SLOT_UTC_KEY


_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_weekdays(spec: str | None) -> set[int] | None:
    """Parse a comma-separated weekday spec into Python weekday ints (Mon=0)."""
    if spec is None or not spec.strip():
        return None
    result: set[int] = set()
    for raw_token in spec.split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token.isdigit():
            num = int(token)
            if not (0 <= num <= 6):
                raise ValueError(
                    f"Weekday number out of range (0-6, Mon=0): {token!r}")
            result.add(num)
            continue
        if token in _WEEKDAY_NAMES:
            result.add(_WEEKDAY_NAMES[token])
            continue
        raise ValueError(
            f"Unrecognized weekday token: {raw_token!r}. Use mon/tue/.../sun or 0-6.")
    return result or None


def parse_months(spec: str | None) -> set[int] | None:
    """Parse a comma-separated month spec into month ints (1..12)."""
    if spec is None or not spec.strip():
        return None
    result: set[int] = set()
    for raw_token in spec.split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token.isdigit():
            num = int(token)
            if not (1 <= num <= 12):
                raise ValueError(f"Month number out of range (1-12): {token!r}")
            result.add(num)
            continue
        if token in _MONTH_NAMES:
            result.add(_MONTH_NAMES[token])
            continue
        raise ValueError(
            f"Unrecognized month token: {raw_token!r}. Use jan/feb/.../dec or 1-12.")
    return result or None


def filter_rows_by_day_of_week(rows, weekdays: set[int] | None, facility_tz):
    """Drop machineschedule rows whose facility-local weekday isn't in
    `weekdays`. Pass-through when None. Converts date_and_time_utc -> local.
    """
    if not weekdays:
        return rows
    out = []
    for r in rows:
        local_dt = to_local(parse_slot_dt(r[SLOT_UTC_KEY]), facility_tz)
        if local_dt.weekday() in weekdays:
            out.append(r)
    return out


def filter_rows_by_month(rows, months: set[int] | None, facility_tz):
    """Drop machineschedule rows whose facility-local month isn't in `months`.
    Pass-through when None. Converts date_and_time_utc -> local.
    """
    if not months:
        return rows
    out = []
    for r in rows:
        local_dt = to_local(parse_slot_dt(r[SLOT_UTC_KEY]), facility_tz)
        if local_dt.month in months:
            out.append(r)
    return out


def _tod_is_morning(tod: str | None) -> bool | None:
    """Normalize --time-of-day to True (morning) / False (afternoon) / None."""
    if tod is None or tod == "any":
        return None
    if tod not in ("morning", "afternoon"):
        raise ValueError(
            f"time-of-day must be 'morning', 'afternoon', or 'any'; got {tod!r}")
    return tod == "morning"


def filter_blocks_by_tod(blocks, tod: str | None, facility_tz):
    """Drop first-modality block rows whose start hour doesn't match `tod`.
    Equivalent to the option-level filter but cheaper (trims anchors before
    chain growth). `appointment_start_time` is a facility-local ISO string.
    """
    is_morning = _tod_is_morning(tod)
    if is_morning is None:
        return blocks
    out = []
    for row in blocks:
        start_dt = parse_slot_dt(row["appointment_start_time"]).astimezone(facility_tz)
        if (start_dt.hour < 12) == is_morning:
            out.append(row)
    return out


def filter_by_time_of_day(options, tod: str | None, facility_tz):
    """Keep options whose start hour matches the requested half. 'morning' =
    start < 12:00 local; 'afternoon' = >= 12:00; 'any'/None pass-through.
    """
    is_morning = _tod_is_morning(tod)
    if is_morning is None:
        return options
    out = []
    for opt in options:
        start_dt = parse_slot_dt(opt["start"]).astimezone(facility_tz)
        if (start_dt.hour < 12) == is_morning:
            out.append(opt)
    return out


def _modality_set(option):
    """Frozen set of modalities covered by an option (only same-set options
    can dominate each other)."""
    chain = option.get("chain") or {}
    return frozenset(chain.keys())


def dedupe_options(options):
    """Keep the BEST option per (start, end) window — fewest patient wait
    slots wins; tiebreak keeps first occurrence. Stable in first-seen order.
    """
    best_by_window = {}
    first_seen_order = []
    for opt in options:
        key = (opt["start"], opt["end"])
        if key not in best_by_window:
            best_by_window[key] = opt
            first_seen_order.append(key)
        elif (opt.get("total_wait_slots", 0)
              < best_by_window[key].get("total_wait_slots", 0)):
            best_by_window[key] = opt
    return [best_by_window[k] for k in first_seen_order]


def pareto_prune(options):
    """Drop options whose appointment window is dominated. Y dominates X when
    Y.start >= X.start AND Y.end <= X.end (>=1 strict) AND same modality set —
    the tighter window is never worse for the same procedures. O(n log n);
    stable in input order.
    """
    if not options:
        return []

    groups: dict = {}
    for idx, opt in enumerate(options):
        ms = _modality_set(opt)
        groups.setdefault(ms, []).append((idx, opt))

    keep = [True] * len(options)

    for group in groups.values():
        best_per_start: dict = {}
        for idx, opt in group:
            s = datetime.fromisoformat(opt["start"])
            e = datetime.fromisoformat(opt["end"])
            cur = best_per_start.get(s)
            if cur is None:
                best_per_start[s] = (e, [idx])
            elif e < cur[0]:
                for old_idx in cur[1]:
                    keep[old_idx] = False
                best_per_start[s] = (e, [idx])
            elif e > cur[0]:
                keep[idx] = False
            else:
                cur[1].append(idx)

        min_end_seen = None
        for s in sorted(best_per_start.keys(), reverse=True):
            e, idxs = best_per_start[s]
            if min_end_seen is not None and min_end_seen <= e:
                for idx in idxs:
                    keep[idx] = False
            if min_end_seen is None or e < min_end_seen:
                min_end_seen = e

    return [opt for i, opt in enumerate(options) if keep[i]]
