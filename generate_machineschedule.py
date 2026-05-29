"""
generate_machineschedule.py

Generates blank slot rows in pc1.machineschedule for one or more
facilities over a rolling window. Each row represents one bookable
time slot for one machine at one facility for one tenant at one UTC
instant.

This script is the Phase 2 writer for pc1.machineschedule (see
docs/machineschedule.md for the schema). The Phase 3 reconciler reads
these rows and mutates `availability`, `exceptions`, `exception_ids`
based on active pc1.scheduleexceptions rules. The Phase 4 scheduling
engine reads them when assembling patient appointment options.

Schema mapping
--------------
For each (active modality, facility-local date in window, slot start
in 24-hour day):

    client_id          <- --client-id (resolve_client_id)
    facility_id        <- pc1.facilities.id (lookup by facility_name)
    modality_id        <- pc1.modalities.id (active rows for the facility)
    seq                <- facility-local YYYYMMDDHHMMSS bigint of slot start
                          (engine extracts the local DATE via str(seq)[:8])
    slot_seq           <- 1-based ordinal within the facility-local day
    date_and_time_utc  <- UTC instant (facility-local wall clock converted
                          via zoneinfo)
    start_time         <- facility-local time-of-day
    end_time           <- start_time + slot_size (wraps to 00:00 at the
                          end of the day when slot_size divides 24h cleanly)
    capacity       <- 1
    availability   <- 1 if opening_time <= start_time < closing_time
                      (in business hours), else 0 (out-of-hours, never
                      offered to patients). Reconciler may flip
                      in-hours slots to 0 for Hard exceptions later.
    scheduled      <- NULL (reserved)
    exceptions     <- {} (reconciler-maintained)
    exception_ids  <- {} (reconciler-maintained)
    order_id       <- NULL
    created_at     <- now()
    updated_at     <- now()

Modality filter
---------------
Generates slots only for pc1.modalities rows where
`is_active = true AND status IN ('Active', NULL)`. Rows with
status='Inactive' or status='UNKNOWN' are placeholder NovaRIS junk
(see docs/modalities.md) and never get blank slots.

Facility scope
--------------
When --facility is NOT passed, iterates every row in pc1.facilities
for the active tenant that has `is_client = true`. Pass
--facility=NAME to bypass this filter for one-off testing.

Window resolution
-----------------
    start date:     --start-date (default = today facility-local)
    end date:       start + --days-ahead (default = facility's
                     advance_booking_days)
    slot duration:  pc1.facilities.slot_size
    business hours: pc1.facilities.opening_time / closing_time
    timezone:       pc1.facilities.timezone (IANA name)

pc1.facilities currently carries NOT NULL versions of slot_size,
advance_booking_days, opening_time, closing_time, and timezone, so
the resolution degenerates to "use the facility value." pc1.clients
holds the tenant-level defaults and would act as a fallback if any
of those columns ever became nullable on the facility row.

Idempotency
-----------
INSERT ... ON CONFLICT (client_id, facility_id, modality_id,
date_and_time_utc) DO NOTHING. Re-running over an already-generated
window is a safe no-op. Use the SQL command below if you need to wipe
slots before regenerating:

    DELETE FROM pc1.machineschedule
     WHERE client_id         = <ID>
       AND facility_id       = <ID>
       AND date_and_time_utc >= '<ISO>'::timestamptz;

DST behavior
------------
The generator iterates 24 wall-clock slot-starts per day. On the
spring-forward day, slots in the skipped hour (typically 02:00-03:00
local) round-trip to the same UTC instant as the next hour's slots;
ON CONFLICT DO NOTHING silently drops the duplicates so that day ends
up with 23 unique UTC slots for that hour (which matches reality).
On the fall-back day, only the first fold of the doubled hour is
emitted, so the day has 24 slots instead of 25. Operationally
invisible -- DST transitions happen at 02:00, well before business
hours.

CLI flags
---------
    --facility=NAME           Limit to one facility. Default iterates
                              all is_client=true facilities for the tenant.
    --start-date=YYYY-MM-DD   Window start (default: today facility-local).
    --days-ahead=N            Window length (default: resolved
                              advance_booking_days).
    --limit=N                 Cap rows per facility (smoke testing).
    --dry-run                 Compute rows without writing to Supabase.
    --client-id=NNNN          Override active tenant for this run.
    --quiet                   Suppress per-facility progress chatter.

Exit codes
----------
    0  clean
    1  resolution failure (no facilities, no modalities, bad date)
    3  partial (some facilities written, some skipped)

Required .env
-------------
    SUPABASE_URL, SUPABASE_KEY
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import sys
import time as _time_module
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from supabase_client import get_supabase
from client_context import add_client_id_arg, resolve_client_id

# Identifier used in log output and (when this table eventually grows a
# ris_metadata column) for writer attribution.
WRITER_NAME = "generate_machineschedule.py"

# Schema-qualified Supabase access. pc1 is not the default schema;
# PostgREST must expose it in db-schemas for these calls to reach the DB.
PC1_SCHEMA = "pc1"

# PostgREST tolerates large insert batches but rate-limits past a
# few thousand rows per request. 1000 is the established convention
# across the existing scrapers.
INSERT_BATCH_SIZE = 1000

# How many minutes are in one calendar day. The generator emits
# (1440 // slot_size) slot-starts per day, ignoring any remainder; in
# practice slot_size is always a divisor of 1440 (15, 30, 60).
MINUTES_PER_DAY = 24 * 60

_QUIET = False


def vprint(*args, **kwargs):
    if not _QUIET:
        print(*args, **kwargs)


def _table(supabase, name: str):
    return supabase.schema(PC1_SCHEMA).table(name)


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── Settings resolution (facility overrides client) ─────────────────────────

def _coerce_time(value) -> time:
    """
    PostgREST returns Postgres `time` columns as 'HH:MM:SS' strings.
    Accept either a string or an already-coerced `time` instance and
    return a `time`. Raises TypeError / ValueError on anything else
    so the caller can flag the facility as skippable.
    """
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        return time.fromisoformat(value)
    raise TypeError(f"Cannot coerce {value!r} to time")


def _resolve_setting(facility_row: dict, client_row: dict, key: str):
    """
    Facility value wins when non-NULL; otherwise fall back to client.
    Today every relevant column is NOT NULL on pc1.facilities so the
    fallback is dead code -- preserved for forward compatibility in
    case any of these become nullable later (per the override design
    documented in docs/machineschedule.md).
    """
    fac_val = facility_row.get(key)
    if fac_val is not None:
        return fac_val
    return client_row.get(key)


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
            f"No pc1.clients row for id={client_id}. Register the tenant "
            f"before running the generator."
        )
    return row


def _fetch_facility_rows(supabase, client_id: int,
                         facility_name: str | None) -> list:
    """
    Return facility rows for this client. If facility_name is given,
    return just that one row (regardless of is_client). Otherwise
    return every is_client=true row.
    """
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


def _fetch_active_modalities(supabase, client_id: int,
                             facility_id: int) -> list:
    """
    Return pc1.modalities rows for this facility that should get blank
    slots: is_active=true AND status IN ('Active', NULL). Inactive and
    UNKNOWN rows are NovaRIS placeholder junk -- see docs/modalities.md.

    PostgREST's `.or_()` is the cleanest way to express
    "status='Active' OR status IS NULL".
    """
    rows = (
        _table(supabase, "modalities")
        .select("id, modality_machine, modality_type, status")
        .eq("client_id", client_id)
        .eq("facility_id", facility_id)
        .eq("is_active", True)
        .or_("status.eq.Active,status.is.null")
        .execute()
        .data or []
    )
    return rows


# ── Slot iteration ──────────────────────────────────────────────────────────

def _iter_slot_times(slot_size: int):
    """
    Yield (slot_seq, start_time, end_time) for every slot in a
    24-hour day. slot_seq is 1-based. end_time wraps to 00:00:00
    after the last slot when slot_size divides 1440 cleanly.
    """
    slots_per_day = MINUTES_PER_DAY // slot_size
    for idx in range(slots_per_day):
        start_minutes = idx * slot_size
        end_minutes   = start_minutes + slot_size
        sh, sm = divmod(start_minutes, 60)
        eh, em = divmod(end_minutes,   60)
        start_t = time(sh, sm)
        # 24:00 isn't a valid `time` value -- wrap to 00:00 for the
        # final slot of a 1440-divisible day. The engine treats
        # end_time < start_time as "ends at midnight next day."
        end_t = time(0, 0) if eh >= 24 else time(eh, em)
        yield idx + 1, start_t, end_t


def _build_records_for_modality(
    client_id: int, facility_id: int, modality_id: int,
    start_date: date, end_date: date, slot_size: int,
    tz: ZoneInfo, opening_time: time, closing_time: time,
    now_iso: str,
) -> list:
    """
    Generate one record per slot for one modality over the inclusive
    date range. Facility-local wall clock is converted to UTC at write
    time via zoneinfo (DST handled automatically; see module
    docstring).

    Slots whose facility-local start_time falls in the half-open
    interval [opening_time, closing_time) are initialized with
    availability=1 (free). All other slots get availability=0 so the
    scheduling engine never offers an out-of-business-hours slot to
    a patient. The Phase 3 reconciler must preserve this distinction
    when expanding exceptions -- see docs/machineschedule_generator.md
    -> "Initial availability and business hours" for the full rule.
    """
    records = []
    slot_times = list(_iter_slot_times(slot_size))
    d = start_date
    while d <= end_date:
        for slot_seq, start_t, end_t in slot_times:
            local_dt = datetime.combine(d, start_t).replace(tzinfo=tz)
            utc_dt   = local_dt.astimezone(timezone.utc)
            # `seq` encodes the slot's FACILITY-LOCAL start instant as
            # a YYYYMMDDHHMMSS bigint. The scheduling engine extracts
            # the local DATE via `str(seq)[:8]` (see legacy
            # next_modalitytype_scheduler.same_day() and
            # all_modality_scheduler.build_chain_for_anchor()), so the
            # value MUST be facility-local -- a UTC-based seq would
            # mis-group every evening slot whose UTC date is the next
            # local day.
            seq_local = int(local_dt.strftime("%Y%m%d%H%M%S"))
            # Half-open business-hours window: [opening, closing).
            # Matches the legacy engine's `start_time >= opening
            # AND start_time < closing` DB filter.
            in_hours = opening_time <= start_t < closing_time
            records.append({
                "client_id":         client_id,
                "facility_id":       facility_id,
                "modality_id":       modality_id,
                "seq":               seq_local,
                "slot_seq":          slot_seq,
                "date_and_time_utc": utc_dt.isoformat(),
                "start_time":        start_t.isoformat(),
                "end_time":          end_t.isoformat(),
                "capacity":          1,
                "availability":      1 if in_hours else 0,
                "exceptions":        [],
                "exception_ids":     [],
                "created_at":        now_iso,
                "updated_at":        now_iso,
            })
        d += timedelta(days=1)
    return records


# ── Supabase write ──────────────────────────────────────────────────────────

def _insert_with_skip_dupes(supabase, records: list) -> int:
    """
    Batched insert with ON CONFLICT (business key) DO NOTHING.
    Returns the count of records we attempted to write (PostgREST does
    not surface a per-row inserted/skipped split for ignore_duplicates).
    """
    total = 0
    for batch in _chunked(records, INSERT_BATCH_SIZE):
        _table(supabase, "machineschedule").upsert(
            batch,
            on_conflict="client_id,facility_id,modality_id,date_and_time_utc",
            ignore_duplicates=True,
        ).execute()
        total += len(batch)
    return total


def _count_existing_in_range(supabase, client_id: int, facility_id: int,
                             utc_start_iso: str, utc_end_iso: str) -> int:
    """
    Count rows already present in pc1.machineschedule for this
    facility within the UTC window. Used only for the post-write
    summary so the operator can see "inserted N new + skipped M
    existing" without parsing per-row results.
    """
    res = (
        _table(supabase, "machineschedule")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .eq("facility_id", facility_id)
        .gte("date_and_time_utc", utc_start_iso)
        .lt("date_and_time_utc", utc_end_iso)
        .limit(1)
        .execute()
    )
    return res.count or 0


# ── Driver ──────────────────────────────────────────────────────────────────

def generate(args):
    global _QUIET
    _QUIET = args.quiet

    client_id = resolve_client_id(args)
    supabase  = get_supabase()

    client_row = _fetch_client_row(supabase, client_id)
    facilities = _fetch_facility_rows(supabase, client_id, args.facility or None)

    if not facilities:
        if args.facility:
            print(f"ERROR: no pc1.facilities row found for "
                  f"client_id={client_id}, facility_name={args.facility!r}.")
        else:
            print(f"ERROR: no is_client=true pc1.facilities rows found "
                  f"for client_id={client_id}. Nothing to generate.")
        sys.exit(1)

    vprint(f"client_id={client_id} ({client_row.get('client_name')})  "
           f"facilities={len(facilities)}  "
           f"{'DRY RUN' if args.dry_run else 'WRITE'}")

    started_at      = _time_module.time()
    grand_attempted = 0
    skipped_facilities: list = []

    for fac in facilities:
        facility_id   = fac["id"]
        facility_name = fac["facility_name"]

        slot_size = int(_resolve_setting(fac, client_row, "slot_size"))
        if MINUTES_PER_DAY % slot_size != 0:
            print(f"  [{facility_name}] WARNING: slot_size={slot_size} does "
                  f"not divide {MINUTES_PER_DAY} cleanly; emitting "
                  f"{MINUTES_PER_DAY // slot_size} slots/day and dropping "
                  f"the remainder.")
        tz_name = _resolve_setting(fac, client_row, "timezone")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            print(f"  [{facility_name}] ERROR: timezone={tz_name!r} is not "
                  f"a recognized IANA name. Skipping facility.")
            skipped_facilities.append(facility_name)
            continue

        # Business hours drive the initial availability of each slot.
        # Postgres time columns come back as 'HH:MM:SS' strings via
        # PostgREST; normalize to time objects so the in-hours check
        # against start_t inside _build_records_for_modality is a
        # plain time-vs-time comparison.
        try:
            opening_time = _coerce_time(_resolve_setting(
                fac, client_row, "opening_time"
            ))
            closing_time = _coerce_time(_resolve_setting(
                fac, client_row, "closing_time"
            ))
        except (TypeError, ValueError) as exc:
            print(f"  [{facility_name}] ERROR: opening_time / closing_time "
                  f"could not be parsed ({exc}). Skipping facility.")
            skipped_facilities.append(facility_name)
            continue
        if opening_time >= closing_time:
            print(f"  [{facility_name}] ERROR: opening_time={opening_time} "
                  f">= closing_time={closing_time}. Skipping facility.")
            skipped_facilities.append(facility_name)
            continue

        # --days-ahead overrides advance_booking_days. Default = the
        # facility's resolved value (override over client default).
        if args.days_ahead is not None:
            days_ahead = int(args.days_ahead)
        else:
            days_ahead = int(_resolve_setting(
                fac, client_row, "advance_booking_days"
            ))
        if days_ahead <= 0:
            print(f"  [{facility_name}] ERROR: days_ahead={days_ahead} <= 0. "
                  f"Skipping facility.")
            skipped_facilities.append(facility_name)
            continue

        # --start-date is in the facility's local calendar. Default is
        # today facility-local (which may differ from the operator's
        # today if they're running from a different time zone).
        if args.start_date:
            try:
                start_local = date.fromisoformat(args.start_date)
            except ValueError:
                print(f"  [{facility_name}] ERROR: --start-date={args.start_date!r} "
                      f"is not YYYY-MM-DD. Skipping facility.")
                skipped_facilities.append(facility_name)
                continue
        else:
            start_local = datetime.now(tz).date()
        end_local = start_local + timedelta(days=days_ahead)

        modalities = _fetch_active_modalities(supabase, client_id, facility_id)
        if not modalities:
            print(f"  [{facility_name}] no active modalities "
                  f"(is_active=true AND status IN ('Active', NULL)). "
                  f"Skipping.")
            skipped_facilities.append(facility_name)
            continue

        slots_per_day  = MINUTES_PER_DAY // slot_size
        days_in_window = (end_local - start_local).days + 1
        expected_rows  = len(modalities) * days_in_window * slots_per_day

        # Pre-compute expected in-hours slots per day so the per-facility
        # log line shows the split between bookable and out-of-hours slots
        # before any DB write. Same arithmetic the row builder uses.
        in_hours_per_day = sum(
            1 for _, st, _ in _iter_slot_times(slot_size)
            if opening_time <= st < closing_time
        )
        expected_in_hours = len(modalities) * days_in_window * in_hours_per_day

        vprint(f"  [{facility_name}] slot_size={slot_size}min  "
               f"tz={tz_name}  hours=[{opening_time}, {closing_time})  "
               f"window={start_local} -> {end_local} "
               f"({days_in_window} days)  modalities={len(modalities)}  "
               f"-> {expected_rows:,} candidate slots "
               f"({expected_in_hours:,} availability=1 / "
               f"{expected_rows - expected_in_hours:,} availability=0)")

        now_iso = datetime.now(timezone.utc).isoformat()
        records: list = []
        for m in modalities:
            records.extend(_build_records_for_modality(
                client_id    = client_id,
                facility_id  = facility_id,
                modality_id  = m["id"],
                start_date   = start_local,
                end_date     = end_local,
                slot_size    = slot_size,
                tz           = tz,
                opening_time = opening_time,
                closing_time = closing_time,
                now_iso      = now_iso,
            ))

        if args.limit is not None and args.limit > 0:
            records = records[: args.limit]
            vprint(f"  [{facility_name}] --limit={args.limit} applied; "
                   f"trimmed to {len(records):,} records.")

        if args.dry_run:
            print(f"  [{facility_name}] DRY RUN: would attempt "
                  f"{len(records):,} inserts (duplicates would be "
                  f"silently skipped).")
            grand_attempted += len(records)
            continue

        # Count what's already present in the UTC window so the operator
        # can see new-vs-skipped after the run. Two boundaries are the
        # UTC equivalents of [start_local 00:00, end_local+1 00:00).
        utc_start = datetime.combine(start_local, time(0)).replace(
            tzinfo=tz).astimezone(timezone.utc).isoformat()
        utc_end   = datetime.combine(end_local + timedelta(days=1),
                                     time(0)).replace(
            tzinfo=tz).astimezone(timezone.utc).isoformat()
        existing_before = _count_existing_in_range(
            supabase, client_id, facility_id, utc_start, utc_end
        )

        attempted = _insert_with_skip_dupes(supabase, records)

        existing_after = _count_existing_in_range(
            supabase, client_id, facility_id, utc_start, utc_end
        )
        inserted = existing_after - existing_before
        skipped  = attempted - inserted

        print(f"  [{facility_name}] attempted={attempted:,}  "
              f"inserted={inserted:,}  skipped_existing={skipped:,}")
        grand_attempted += attempted

    elapsed = _time_module.time() - started_at
    mm, ss  = divmod(int(elapsed), 60)
    rate    = grand_attempted / elapsed if elapsed > 0 else 0
    print(f"\nDone. Total slots attempted: {grand_attempted:,}  "
          f"Elapsed: {mm}m {ss}s  Rate: {rate:,.0f} rows/sec")

    if skipped_facilities:
        print(f"WARNING: {len(skipped_facilities)} facility(ies) skipped: "
              f"{', '.join(skipped_facilities)}")
        sys.exit(3)


DESCRIPTION = (
    "Generate blank pc1.machineschedule rows for one or more facilities "
    "over a rolling window. Default iterates every is_client=true "
    "facility; --facility=NAME limits to one. Idempotent (ON CONFLICT "
    "DO NOTHING)."
)


def main():
    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument("--facility", default="",
                   help="Limit to one facility (e.g. 'Inview-Fremont'). "
                        "Must match a pc1.facilities.facility_name exactly. "
                        "Default iterates every is_client=true facility "
                        "for the tenant.")
    p.add_argument("--start-date", default="",
                   help="Inclusive window start in YYYY-MM-DD, "
                        "facility-local. Default: today.")
    p.add_argument("--days-ahead", type=int, default=None,
                   help="Window length in days. Default: facility's "
                        "advance_booking_days (override over client).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap rows per facility (smoke testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute rows; do not write to Supabase.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-facility progress chatter.")
    add_client_id_arg(p)
    args = p.parse_args()
    generate(args)


if __name__ == "__main__":
    main()
