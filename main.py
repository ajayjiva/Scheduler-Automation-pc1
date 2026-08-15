"""
main.py — multi-modality scheduling-options engine (Phase 3.6 pc1 port).

Reads a patient's orders from pc1.orders_v, the slot calendar from
pc1.machineschedule_v, and per-facility settings merged from
pc1.facilities <- pc1.clients, then prints combined appointment options.

Key pc1 adaptations vs the legacy bundle
----------------------------------------
- All DB access is schema-qualified (`.schema("pc1")`).
- Settings come from facility_settings.get_facility_settings() (pc1.clients /
  pc1.facilities), not a clientparameters table.
- The order's facility is an int `facility_id`; the slot calendar is filtered
  by facility_id and read from machineschedule_v (which rejoins modality_type
  / modality_machine and exposes date_and_time_utc).
- The per-machine identity is `modality_id` (int FK) throughout; the machine
  name (modality_machine) is display-only.
- stat_order / machine_skill / contrast_skill are deferred in pc1.orders_v;
  they're read with .get() (None) and remain inert in the scheduler.

Not yet ported (deliberate follow-up): orders_v.requesting_date as a
start-floor override (documented in docs/orders.md). This first port
reproduces the legacy start-floor (now + 1 slot / --start-date). The seed's
P06 carries a requesting_date to validate that behavior when it's added.
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import itertools
from datetime import date, datetime, time, timedelta, timezone

from supabase_client import get_supabase
from client_context import add_client_id_arg, resolve_client_id
from facility_settings import get_facility_settings
from get_orders import get_summary_list
from first_modalitytype_scheduler import get_first_modality_block

from cumulative_open_slots import (
    compute_cumm_open_slots_below,
    compute_cumm_open_slots_above,
)

from all_modality_scheduler import (
    build_all_chains,
    print_patient_options,
)

from per_machine_resolver import (
    derive_machines_from_machineschedule,
    enumerate_machine_combinations,
    has_per_machine_variation,
    resolve_per_machine_slots,
)

from option_filters import (
    dedupe_options,
    filter_blocks_by_tod,
    filter_by_time_of_day,
    filter_rows_by_day_of_week,
    filter_rows_by_month,
    parse_months,
    parse_weekdays,
    pareto_prune,
)

from tz_helpers import (
    now_local as now_in_tz,
    parse_clock_time,
    resolve_facility_tz,
)

DEBUG = False

# Safety ceiling for the per-machine combination loop; above this we fall back
# to the legacy single-total path so the patient still gets options.
MAX_COMBINATIONS = 1000

# Default cap on patient-facing options (0 = no cap; --max-options overrides).
DEFAULT_MAX_OPTIONS = 50


def _fetch_all_pages(query_factory, page_size: int = 1000):
    """Paginate a Supabase select past PostgREST's ~1000-row cap. Rebuilds the
    query per page and applies .range(). Returns concatenated rows.
    """
    all_rows = []
    offset = 0
    while True:
        response = (
            query_factory()
            .range(offset, offset + page_size - 1)
            .execute()
        )
        chunk = response.data or []
        all_rows.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
    return all_rows


def get_first_order_for_modality(rows, modality_type):
    for row in rows:
        if row["modality_type"] == modality_type:
            return row
    return None


def _round_up_to_slot(dt: datetime, slot_size_minutes: int) -> datetime:
    """Earliest datetime >= dt aligned to a slot boundary from midnight."""
    snapped = dt.replace(second=0, microsecond=0)
    if snapped < dt:
        snapped += timedelta(minutes=1)
    minutes_into_day = snapped.hour * 60 + snapped.minute
    remainder = minutes_into_day % slot_size_minutes
    if remainder == 0:
        return snapped
    return snapped + timedelta(minutes=(slot_size_minutes - remainder))


def _compute_default_start_floor(now_local: datetime, slot_size_minutes: int) -> datetime:
    """Default start floor: earliest slot boundary at or after now + slot_size."""
    return _round_up_to_slot(
        now_local + timedelta(minutes=slot_size_minutes),
        slot_size_minutes,
    )


def _parse_cli_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def _build_options_per_machine(
    combinations,
    rows,
    all_schedule_rows,
    all_resource_rows,
    use_technician_calendar,
    slot_size_minutes,
    v_wait_threshold,
    closing_time,
    facility_tz,
    time_of_day: str = "any",
):
    """Run the inner scheduler once per (machine combination, modality
    permutation), memoizing block computation by (modality, modality_id,
    slots). Cross-combination dedupe + Pareto-prune happen incrementally.
    """
    block_cache = {}

    def get_blocks(modality, machine, slots, order_row):
        key = (modality, machine, slots)
        if key not in block_cache:
            block_cache[key] = get_first_modality_block(
                all_schedule_rows=all_schedule_rows,
                all_resource_rows=all_resource_rows,
                modality_type=modality,
                required_slots=slots,
                stat_order=order_row.get("stat_order"),
                machine_skill=order_row.get("machine_skill"),
                contrast_skill=order_row.get("contrast_skill"),
                use_technician_calendar=use_technician_calendar,
                slot_size_minutes=slot_size_minutes,
                restrict_to_machine=machine,   # modality_id (int)
                closing_time=closing_time,
                facility_tz=facility_tz,
                debug=DEBUG,
            )
        return block_cache[key]

    all_options = []

    for combo_idx, combo in enumerate(combinations, start=1):
        modality_order = combo["modality_order"]
        slots = combo["slots"]
        assignment = combo["assignment"]
        total_required_slots = sum(slots.values())

        if DEBUG:
            print(f"\n=== COMBINATION {combo_idx}/{len(combinations)}: "
                  f"{assignment} | totals {slots} "
                  f"| anchor {combo['anchor_modality']}/{combo['anchor_machine']} ===")

        modality_blocks = {}
        for modality in modality_order:
            order_row = get_first_order_for_modality(rows, modality)
            if order_row is None:
                modality_blocks[modality] = []
                continue
            modality_blocks[modality] = get_blocks(
                modality,
                assignment[modality],
                slots[modality],
                order_row,
            )

        first_modality = modality_order[0]
        first_block = modality_blocks.get(first_modality, [])
        first_block = filter_blocks_by_tod(first_block, time_of_day, facility_tz)
        if not first_block:
            if DEBUG:
                print(f"  (no blocks for anchor {first_modality}/"
                      f"{assignment[first_modality]}; skipping combo)")
            continue

        remaining_modalities = modality_order[1:]

        for perm in itertools.permutations(remaining_modalities):
            permuted_order = [first_modality] + list(perm)
            if DEBUG and remaining_modalities:
                print(f"  trying modality order: {permuted_order}")

            options = build_all_chains(
                first_block=first_block,
                modality_order=permuted_order,
                modality_blocks=modality_blocks,
                v_wait_threshold=v_wait_threshold,
                total_required_slots=total_required_slots,
                debug=DEBUG,
            )
            all_options.extend(options)
            all_options = pareto_prune(dedupe_options(all_options))

    if DEBUG:
        print(f"\nBlock-cache stats: {len(block_cache)} unique "
              f"(modality, modality_id, slots) keys computed")

    return all_options


def main():
    parser = argparse.ArgumentParser(description="Multi-modality scheduler (pc1).")
    add_client_id_arg(parser)
    parser.add_argument("--patient-id", type=int, default=10001,
                        help="Patient ID to schedule (pc1.patients.id).")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Earliest date (YYYY-MM-DD). Default: dynamic "
                             "(now + 1 slot) rounded up.")
    parser.add_argument("--end-date", type=str, default=None,
                        help="Latest date (YYYY-MM-DD, inclusive). Default: "
                             "today + advance_booking_days (or +180).")
    parser.add_argument("--day-of-week", type=str, default=None,
                        help="Comma-sep weekdays (mon..sun or 0..6). Default: all.")
    parser.add_argument("--month", type=str, default=None,
                        help="Comma-sep months (jan..dec or 1..12). Default: all.")
    parser.add_argument("--time-of-day", type=str, default="any",
                        choices=["morning", "afternoon", "any"],
                        help="Restrict START time. Default: any.")
    parser.add_argument("--max-options", type=int, default=DEFAULT_MAX_OPTIONS,
                        help=f"Max options to display (default {DEFAULT_MAX_OPTIONS}; "
                             f"0 = all).")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose internal state.")
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    weekdays_filter = parse_weekdays(args.day_of_week)
    months_filter = parse_months(args.month)
    cli_start_date = _parse_cli_date(args.start_date)
    cli_end_date = _parse_cli_date(args.end_date)

    client_id = resolve_client_id(args)
    supabase = get_supabase()
    patient_id = args.patient_id

    # Orders first — they tell us the facility, which selects the settings.
    summary_list, rows, facility_id = get_summary_list(
        supabase, patient_id=patient_id, client_id=client_id
    )
    if not summary_list:
        print("No orders found for patient:", patient_id)
        return
    if not facility_id:
        print("No facility found for patient orders:", patient_id)
        return

    # Per-facility settings (facility overrides client default).
    cp = get_facility_settings(supabase, client_id, facility_id)
    v_wait_threshold = int(cp.get("wait_threshold") or 0)
    use_technician_calendar = bool(cp.get("use_technician_calendar"))
    slot_size_minutes = int(cp.get("slot_size") or 15)
    opening_time = parse_clock_time(cp.get("opening_time"))
    closing_time = parse_clock_time(cp.get("closing_time"))
    facility_tz = resolve_facility_tz(cp)
    advance_booking_days = int(cp.get("advance_booking_days") or 180)

    if DEBUG:
        print(f"\nCLIENT: {cp.get('client_name')!r} (id={client_id})  "
              f"facility_id={facility_id}")
        print(f"  wait_threshold:          {v_wait_threshold}")
        print(f"  use_technician_calendar: {use_technician_calendar}")
        print(f"  slot_size_minutes:       {slot_size_minutes}")
        print(f"  opening_time:            {opening_time}")
        print(f"  closing_time:            {closing_time}")
        print(f"  advance_booking_days:    {advance_booking_days}")

    modality_order = [item["modality_type"] for item in summary_list]

    if DEBUG:
        print("\n--- ORDERS SUMMARY ---")
        for item in summary_list:
            print(item)
        print("\nFACILITY_ID:", facility_id)
        print("MODALITY ORDER:", modality_order)

    modality_types = sorted({row["modality_type"] for row in rows})

    now_local = now_in_tz(facility_tz).replace(microsecond=0)
    if cli_start_date is not None:
        start_floor_local = datetime.combine(
            cli_start_date, time(0, 0, 0)
        ).replace(tzinfo=facility_tz)
    else:
        start_floor_local = _compute_default_start_floor(
            now_local, slot_size_minutes
        )

    if cli_end_date is not None:
        end_ceiling_date = cli_end_date
    else:
        end_ceiling_date = now_local.date() + timedelta(days=advance_booking_days)
    end_ceiling_local = datetime.combine(
        end_ceiling_date, time(23, 59, 59)
    ).replace(tzinfo=facility_tz)

    # DB filter strings are UTC ISO so PostgREST hits the true-UTC
    # date_and_time_utc column correctly.
    start_date = start_floor_local.astimezone(timezone.utc).isoformat()
    end_date = end_ceiling_local.astimezone(timezone.utc).isoformat()

    if DEBUG:
        print(f"\n--- DATE RANGE ---")
        print(f"  facility_tz:       {facility_tz.key}")
        print(f"  now_local:         {now_local.isoformat()}")
        print(f"  start_floor local: {start_floor_local.isoformat()}  "
              f"(default-dynamic={cli_start_date is None})")
        print(f"  end_ceiling local: {end_ceiling_local.isoformat()}  "
              f"(default-from-advance_booking_days={cli_end_date is None})")
        print(f"  start_floor UTC:   {start_date}")
        print(f"  end_ceiling UTC:   {end_date}")
        print(f"  time-of-day filter: {args.time_of_day} (facility-local)")

    # Slot calendar from machineschedule_v — paginated past the 1000-row cap.
    # Business-hours filter pushed to the DB via start_time (local TIME).
    def _build_schedule_query():
        q = (
            supabase
            .schema("pc1")
            .table("machineschedule_v")
            .select("*")
            .eq("client_id", client_id)
            .eq("facility_id", facility_id)
            .in_("modality_type", modality_types)
            .gte("date_and_time_utc", start_date)
            .lte("date_and_time_utc", end_date)
        )
        if opening_time is not None:
            q = q.gte("start_time", opening_time.strftime("%H:%M:%S"))
        if closing_time is not None:
            q = q.lt("start_time", closing_time.strftime("%H:%M:%S"))
        return (
            q.order("date_and_time_utc", desc=False)
             .order("modality_type", desc=False)
             .order("modality_machine", desc=False)
        )

    all_schedule_rows = _fetch_all_pages(_build_schedule_query)

    all_schedule_rows = filter_rows_by_day_of_week(
        all_schedule_rows, weekdays_filter, facility_tz
    )
    all_schedule_rows = filter_rows_by_month(
        all_schedule_rows, months_filter, facility_tz
    )

    # Technician calendar — skipped for tenants without one (Inview). pc1 does
    # not yet define technicianschedule_v; this branch is here for parity and
    # only runs when use_technician_calendar=true.
    if use_technician_calendar:
        all_resource_rows = _fetch_all_pages(
            lambda: (
                supabase
                .schema("pc1")
                .table("technicianschedule_v")
                .select("*")
                .eq("client_id", client_id)
                .eq("facility_id", facility_id)
                .gte("date_and_time_utc", start_date)
                .lte("date_and_time_utc", end_date)
                .order("date_and_time_utc", desc=False)
                .order("resource_code", desc=False)
            )
        )
    else:
        if DEBUG:
            print("  (skipping technicianschedule_v — use_technician_calendar=false.)")
        all_resource_rows = []

    compute_cumm_open_slots_below(all_schedule_rows, facility_tz)
    compute_cumm_open_slots_above(all_schedule_rows, facility_tz)

    # Per-machine resolution from the multi-row orders_v rows (4-tier
    # precedence), keyed on modality_id.
    machines_by_modality = derive_machines_from_machineschedule(all_schedule_rows)
    per_machine_slots, unresolvable = resolve_per_machine_slots(
        rows, machines_by_modality
    )

    if DEBUG:
        print("\n--- MACHINES AT FACILITY (modality_type -> [modality_id]) ---")
        for modality, machines in machines_by_modality.items():
            print(f"  {modality}: {machines}")
        print("\n--- PER-MACHINE TOTAL SLOTS ---")
        for modality, totals in per_machine_slots.items():
            print(f"  {modality}: {totals}")

    if unresolvable:
        print("\n--- DATA-QUALITY ISSUES "
              "(some (modality, machine) pairs have no fallback shape) ---")
        for modality, machine_id, order_id in unresolvable:
            print(f"  Order {order_id} ({modality}): no proceduresestimate "
                  f"shape resolvable for modality_id {machine_id}; that "
                  f"machine is excluded. Fix: add a (facility, modality_id=NULL) "
                  f"or (facility, modality_id={machine_id}) row for this procedure.")

    needed_modalities = set(modality_types)
    resolved_modalities = set(per_machine_slots.keys())
    missing = needed_modalities - resolved_modalities
    if missing:
        print(f"\nERROR: no schedulable machines for modality/modalities: "
              f"{sorted(missing)}.")
        print("Every candidate machine for these modalities was excluded "
              "(data-quality issues above, or no slots in the window). Aborting.")
        return

    use_per_machine = has_per_machine_variation(per_machine_slots)

    if use_per_machine:
        combinations = enumerate_machine_combinations(per_machine_slots)
        if DEBUG:
            print(f"\n=== PER-MACHINE PATH: {len(combinations)} combination(s) ===")
            for c in combinations:
                print(f"  {c['assignment']} -> totals {c['slots']} "
                      f"(sum={c['total_slots']}, "
                      f"anchor={c['anchor_modality']}/{c['anchor_machine']})")

        if len(combinations) > MAX_COMBINATIONS:
            print(f"\nWARNING: {len(combinations)} combinations exceeds ceiling "
                  f"({MAX_COMBINATIONS}). Falling back to legacy single-total path.")
            use_per_machine = False

    if use_per_machine:
        all_options = _build_options_per_machine(
            combinations=combinations,
            rows=rows,
            all_schedule_rows=all_schedule_rows,
            all_resource_rows=all_resource_rows,
            use_technician_calendar=use_technician_calendar,
            slot_size_minutes=slot_size_minutes,
            v_wait_threshold=v_wait_threshold,
            closing_time=closing_time,
            facility_tz=facility_tz,
            time_of_day=args.time_of_day,
        )
    else:
        # Legacy single-total path: one block list per modality across all its
        # machines, then permute and chain. Reached when no per-machine
        # variation exists OR the safety ceiling tripped.
        if DEBUG:
            print("\n=== LEGACY PATH (no per-machine variation) ===")

        total_required_slots = sum(item["total_slots"] for item in summary_list)

        modality_blocks = {}
        for idx, modality in enumerate(modality_order):
            order_row = get_first_order_for_modality(rows, modality)
            required_slots = summary_list[idx]["total_slots"]

            if DEBUG:
                print(f"\n=== COMPUTING AVAILABLE BLOCKS FOR: {modality} ===")

            modality_blocks[modality] = get_first_modality_block(
                all_schedule_rows=all_schedule_rows,
                all_resource_rows=all_resource_rows,
                modality_type=modality,
                required_slots=required_slots,
                stat_order=order_row.get("stat_order"),
                machine_skill=order_row.get("machine_skill"),
                contrast_skill=order_row.get("contrast_skill"),
                use_technician_calendar=use_technician_calendar,
                slot_size_minutes=slot_size_minutes,
                closing_time=closing_time,
                facility_tz=facility_tz,
                debug=DEBUG,
            )

        first_modality = modality_order[0]
        first_block = modality_blocks[first_modality]
        first_block = filter_blocks_by_tod(first_block, args.time_of_day, facility_tz)

        if not first_block:
            print("\nNo available blocks for first modality:", first_modality)
            return

        remaining_modalities = modality_order[1:]
        all_options = []

        for perm in itertools.permutations(remaining_modalities):
            permuted_order = [first_modality] + list(perm)

            if DEBUG:
                print(f"\n=== TRYING MODALITY ORDER: {permuted_order} ===")

            options = build_all_chains(
                first_block=first_block,
                modality_order=permuted_order,
                modality_blocks=modality_blocks,
                v_wait_threshold=v_wait_threshold,
                total_required_slots=total_required_slots,
                debug=DEBUG,
            )
            all_options.extend(options)
            all_options = pareto_prune(dedupe_options(all_options))

    # Final dedupe + Pareto-prune (safety net; incremental prune did most).
    all_options = dedupe_options(all_options)
    all_options = pareto_prune(all_options)

    all_options = filter_by_time_of_day(all_options, args.time_of_day, facility_tz)

    all_options.sort(key=lambda o: (o["start"][:10], o["duration_minutes"], o["start"]))

    total_generated = len(all_options)
    truncated = False
    if args.max_options > 0 and total_generated > args.max_options:
        all_options = all_options[:args.max_options]
        truncated = True

    print_patient_options(
        all_options, modality_order,
        facility_tz=facility_tz, now_local=now_local,
    )

    if truncated:
        extra = total_generated - args.max_options
        print(
            f"\n... and {extra} more option{'s' if extra != 1 else ''} "
            f"not shown (total: {total_generated}). Narrow your search "
            f"(--day-of-week, --time-of-day, --month) or re-run with "
            f"--max-options={total_generated} (or 0 to disable the cap)."
        )


if __name__ == "__main__":
    main()
