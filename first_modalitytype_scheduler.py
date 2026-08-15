from datetime import time, timedelta

from resource_scheduler import find_resource_for_block
from tz_helpers import parse_slot_dt, to_local

# pc1 port notes vs the legacy bundle:
#   * `restrict_to_machine` is a modality_id (int FK), matched against
#     r["modality_id"] -- not the modality_machine name. Keeps the machine
#     identity consistent with the per-machine resolver / orders_v pin.
#   * slot instants are read from r["date_and_time_utc"].
#   * stat_order / machine_skill / contrast_skill are accepted for signature
#     stability but currently inert (debug-only); pc1.orders_v defers them and
#     main.py passes None.


def remove_duplicate_times(rows):
    best_rows = {}
    for row in rows:
        dt = row["date_and_time_utc"]
        machine = row["modality_machine"]
        if dt not in best_rows:
            best_rows[dt] = row
        else:
            if machine < best_rows[dt]["modality_machine"]:
                best_rows[dt] = row
    return list(best_rows.values())


def get_first_modality_block(
    all_schedule_rows,
    all_resource_rows,
    modality_type,
    required_slots,
    stat_order,
    machine_skill,
    contrast_skill,
    use_technician_calendar: bool = True,
    slot_size_minutes: int = 15,
    restrict_to_machine=None,          # modality_id (int) or None
    closing_time: time | None = None,
    facility_tz=None,
    debug: bool = False,
):
    """Compute eligible block-start slot rows for one modality."""
    if debug:
        print("\n--- FIRST MODALITY DEBUG INFO ---")
        print("modality_type:", modality_type)
        print("required_slots:", required_slots)
        print("use_technician_calendar:", use_technician_calendar)
        print("slot_size_minutes:", slot_size_minutes)
        print("restrict_to_machine (modality_id):", restrict_to_machine)
        print("closing_time:", closing_time)
        if facility_tz is not None:
            print("facility_tz:", facility_tz.key)

    if facility_tz is None:
        raise ValueError(
            "facility_tz is required. Pass the facility's ZoneInfo from "
            "tz_helpers.resolve_facility_tz(settings)."
        )

    eligible = [
        r for r in all_schedule_rows
        if r["modality_type"] == modality_type
        and r["calc_cumm_below"] >= required_slots
        and (restrict_to_machine is None
             or r["modality_id"] == restrict_to_machine)
    ]

    unique = remove_duplicate_times(eligible)

    enriched = []
    for row in unique:
        # Parse the stored UTC slot timestamp, convert to facility-local for
        # wall-clock reasoning (business-hours check + patient-facing string).
        start_dt = to_local(parse_slot_dt(row["date_and_time_utc"]), facility_tz)
        end_dt = start_dt + timedelta(minutes=required_slots * slot_size_minutes)

        # Business-hours block-end safety net: a slot whose start is in-hours
        # but whose block-end runs past closing must be rejected. Cross-midnight
        # check first (end_dt.time() rolls back past midnight).
        if closing_time is not None:
            crosses_midnight = end_dt.date() > start_dt.date()
            if crosses_midnight or end_dt.time() > closing_time:
                continue

        row = dict(row)
        # Facility-local ISO strings; option["start"][:10] gives the
        # patient-visible date downstream.
        row["appointment_start_time"] = start_dt.isoformat()
        row["appointment_end_time"] = end_dt.isoformat()

        row["slot_seq_start"] = row["slot_seq"]
        row["slot_seq_end"] = row["slot_seq"] + required_slots - 1

        # Technician-calendar tenants only. Inview has use_technician_calendar
        # false, so availability is already folded into machineschedule and
        # this branch is skipped.
        if use_technician_calendar:
            date_str = start_dt.date().isoformat()
            res_match = find_resource_for_block(
                all_resource_rows=all_resource_rows,
                modality_type=modality_type,
                date_str=date_str,
                slot_seq_start=row["slot_seq_start"],
                slot_seq_end=row["slot_seq_end"],
                facility_tz=facility_tz,
            )
            if not res_match:
                continue
            row["resource_code"] = res_match["resource_code"]
            row["resource_slot_ids"] = res_match["resource_slot_ids"]
        else:
            row["resource_code"] = None
            row["resource_slot_ids"] = None

        enriched.append(row)

    if debug:
        print("\n--- FIRST MODALITY RESULTS ---")
        for r in enriched:
            print(
                r["seq"], r["slot_seq"], r["date_and_time_utc"],
                r["modality_type"], r["modality_machine"], r["modality_id"],
                r["calc_cumm_below"], r["appointment_start_time"],
                r["appointment_end_time"], r["slot_seq_start"], r["slot_seq_end"],
            )

    return enriched
