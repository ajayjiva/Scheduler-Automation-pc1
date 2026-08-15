"""
tz_helpers.py — UTC <-> facility-local conversion helpers (pc1 port).

pc1.machineschedule stores every slot instant as true UTC in the column
`date_and_time_utc` (migrations 0004/0005); `start_time` / `end_time` are
facility-local TIME values; `seq` is the facility-local YYYYMMDDHHMMSS bigint.
Readers convert UTC -> facility-local for any wall-clock-based comparison or
display (day-of-week, time-of-day, business hours, same-day floor, patient
print).

pc1 notes vs the legacy bundle
------------------------------
- Slot timestamps are read from `row["date_and_time_utc"]` (legacy read the
  un-suffixed `date_and_time`).
- The facility timezone comes from the merged settings dict produced by
  `facility_settings.get_facility_settings()` (legacy read a clientparameters
  dict). The key is still `"timezone"`.
"""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The machineschedule_v / machineschedule column holding the true-UTC slot
# instant. Centralized so a future rename touches one line.
SLOT_UTC_KEY = "date_and_time_utc"


def parse_clock_time(value) -> time | None:
    """Coerce a TIME-column value ('HH:MM:SS' or a time) into a time.

    Returns None on empty / NULL input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        return time.fromisoformat(value)
    raise ValueError(f"Unsupported clock-time value: {value!r}")


def parse_slot_dt(value) -> datetime:
    """Parse a slot UTC value into a TZ-aware UTC datetime.

    Accepts an ISO string (PostgREST surfaces timestamptz with a +00:00
    suffix) or a datetime. Naive values are labeled UTC for back-compat with
    test fixtures.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_local(utc_dt: datetime, facility_tz: ZoneInfo) -> datetime:
    """Convert a TZ-aware datetime to the facility's wall-clock zone."""
    if utc_dt.tzinfo is None:
        raise ValueError(
            "to_local() requires a TZ-aware input. Use parse_slot_dt() first."
        )
    return utc_dt.astimezone(facility_tz)


def slot_local_dt(row, facility_tz: ZoneInfo) -> datetime:
    """Parse `row[SLOT_UTC_KEY]` (UTC) and return facility-local TZ-aware."""
    return to_local(parse_slot_dt(row[SLOT_UTC_KEY]), facility_tz)


def slot_local_date_str(row, facility_tz: ZoneInfo) -> str:
    """Facility-local date of a slot row as 'YYYY-MM-DD'."""
    return slot_local_dt(row, facility_tz).date().isoformat()


def now_local(facility_tz: ZoneInfo) -> datetime:
    """Current time in the facility's TZ, host-independent (built from UTC)."""
    return datetime.now(timezone.utc).astimezone(facility_tz)


def resolve_facility_tz(settings: dict) -> ZoneInfo:
    """Get the facility's ZoneInfo from a merged settings dict.

    `settings` is the dict from `facility_settings.get_facility_settings()`.
    pc1.facilities.timezone is NOT NULL, so the value should always be present
    for a real facility. Raises RuntimeError on a missing / invalid IANA name.
    """
    tz_name = settings.get("timezone")
    if not tz_name:
        raise RuntimeError(
            "No `timezone` on the merged settings for this (client, "
            "facility). pc1.facilities.timezone should be a NOT NULL IANA "
            "name (e.g. 'America/Los_Angeles')."
        )
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Invalid IANA timezone {tz_name!r}: {exc}. On Windows, run "
            f"`pip install tzdata` so zoneinfo has the IANA database."
        ) from exc
