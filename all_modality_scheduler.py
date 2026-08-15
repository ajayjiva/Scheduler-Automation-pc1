"""
all_modality_scheduler.py (Phase 3.6 pc1 port).

Unchanged from the legacy bundle: this module operates purely on the block
dicts produced by first_modalitytype_scheduler (seq / slot_seq_start /
slot_seq_end / appointment_start_time / appointment_end_time / modality_machine),
none of which are pc1-renamed columns. Kept here so the engine is
self-contained in the pc1 repo.
"""

from datetime import datetime


def _format_for_patient(iso_str: str, facility_tz) -> str:
    """Render an option ISO timestamp into the patient-facing format, e.g.
    'Thu May 14, 2026 5:00 AM PT'. Collapses seasonal DST labels (PDT/PST ->
    PT, etc.) to a single year-round label; the underlying offset stays
    correct. `%-I`/`%#I` aren't cross-platform so hour/day digits are built
    manually.
    """
    dt = datetime.fromisoformat(iso_str).astimezone(facility_tz)
    hour_12 = dt.hour % 12 or 12
    zname = dt.strftime("%Z")
    if len(zname) == 3 and zname[1:] in ("DT", "ST"):
        zname = zname[0] + "T"
    return (
        f"{dt:%a} {dt:%b} {dt.day}, {dt.year} "
        f"{hour_12}:{dt:%M %p} {zname}"
    )


def find_adjacent_for_combined(blocks_by_day, combined_anchor, v_wait_threshold=0):
    """Return the day's blocks adjacent to the combined anchor window (same
    day only): before-candidates (closest first) then after-candidates.
    """
    if not blocks_by_day or not combined_anchor:
        return []

    day = str(combined_anchor["seq"])[:8]
    day_blocks = blocks_by_day.get(day)
    if not day_blocks:
        return []

    start_slot = combined_anchor["slot_seq_start"]
    end_slot = combined_anchor["slot_seq_end"]

    before_candidates = []
    after_candidates = []

    for block in day_blocks:
        if block["slot_seq_end"] < start_slot:
            gap = start_slot - block["slot_seq_end"]
            if gap <= (1 + v_wait_threshold):
                before_candidates.append((gap, block))

        if block["slot_seq_start"] > end_slot:
            gap = block["slot_seq_start"] - end_slot
            if gap <= (1 + v_wait_threshold):
                after_candidates.append((gap, block))

    before_candidates.sort(key=lambda x: x[0])
    after_candidates.sort(key=lambda x: x[0])

    return [b for _, b in before_candidates] + [b for _, b in after_candidates]


def build_chain_for_anchor(
    first_modality,
    anchor_row,
    modality_order,
    modality_blocks_by_day,
    v_wait_threshold=0,
    debug=False,
):
    """Recursively enumerate every valid chain starting from anchor_row. The
    combined window (union of placed modality slots) expands at each step and
    the next modality is searched against it, discovering chains like
    US->CT->MRI where US is adjacent to CT rather than MRI.
    """
    initial_chain = {first_modality: anchor_row}
    initial_combined = {
        "seq": anchor_row["seq"],
        "slot_seq_start": anchor_row["slot_seq_start"],
        "slot_seq_end": anchor_row["slot_seq_end"],
    }

    def recurse(chain, combined, remaining_modalities):
        if not remaining_modalities:
            return [chain]

        modality = remaining_modalities[0]
        blocks_by_day = modality_blocks_by_day.get(modality, {})
        candidates = find_adjacent_for_combined(
            blocks_by_day, combined, v_wait_threshold
        )

        if not candidates:
            return []

        results = []
        seen_slots = set()
        for next_block in candidates:
            slot_key = (next_block["slot_seq_start"], next_block["slot_seq_end"])
            if slot_key in seen_slots:
                continue
            seen_slots.add(slot_key)

            new_chain = dict(chain)
            new_chain[modality] = next_block
            new_combined = {
                "seq": combined["seq"],
                "slot_seq_start": min(combined["slot_seq_start"], next_block["slot_seq_start"]),
                "slot_seq_end": max(combined["slot_seq_end"], next_block["slot_seq_end"]),
            }
            results.extend(recurse(new_chain, new_combined, remaining_modalities[1:]))

        return results

    return recurse(initial_chain, initial_combined, modality_order[1:])


def compute_total_wait_slots(chain_rows):
    """Total wait slots across gaps between consecutive modality blocks.
    gap=1 means consecutive (no wait); gap=2 means 1 wait slot, etc.
    """
    blocks = sorted(chain_rows.values(), key=lambda r: r["slot_seq_start"])
    total_wait = 0
    for i in range(1, len(blocks)):
        gap = blocks[i]["slot_seq_start"] - blocks[i - 1]["slot_seq_end"]
        total_wait += max(0, gap - 1)
    return total_wait


def build_all_chains(
    first_block,
    modality_order,
    modality_blocks,
    v_wait_threshold=0,
    total_required_slots=None,
    debug=False,
):
    if not first_block:
        return []

    first_modality = modality_order[0]
    options = []
    min_duration = None

    if total_required_slots is not None:
        min_duration = total_required_slots * 15

    # Pre-index every modality's blocks by facility-local day (str(seq)[:8]).
    modality_blocks_by_day = {}
    for modality, blocks in modality_blocks.items():
        by_day = {}
        for block in blocks:
            day = str(block["seq"])[:8]
            by_day.setdefault(day, []).append(block)
        modality_blocks_by_day[modality] = by_day

    for anchor_row in first_block:
        chains = build_chain_for_anchor(
            first_modality=first_modality,
            anchor_row=anchor_row,
            modality_order=modality_order,
            modality_blocks_by_day=modality_blocks_by_day,
            v_wait_threshold=v_wait_threshold,
            debug=debug,
        )

        for chain_rows in chains:
            start_times = [
                datetime.fromisoformat(r["appointment_start_time"])
                for r in chain_rows.values()
            ]
            end_times = [
                datetime.fromisoformat(r["appointment_end_time"])
                for r in chain_rows.values()
            ]

            overall_start = min(start_times)
            overall_end = max(end_times)
            duration_minutes = int((overall_end - overall_start).total_seconds() // 60)

            if min_duration is not None and duration_minutes < min_duration:
                continue

            total_wait_slots = compute_total_wait_slots(chain_rows)

            modality_sequence_list = []
            for modality, row in chain_rows.items():
                modality_sequence_list.append(
                    (modality, datetime.fromisoformat(row["appointment_start_time"]))
                )

            modality_sequence_list.sort(key=lambda x: x[1])
            modality_sequence_list = [m[0] for m in modality_sequence_list]

            option = {
                "start": overall_start.isoformat(),
                "end": overall_end.isoformat(),
                "duration_minutes": duration_minutes,
                "total_wait_slots": total_wait_slots,
                "chain": chain_rows,
                "modality_sequence": modality_sequence_list,
            }
            options.append(option)

    unique = []
    seen = set()
    for opt in options:
        key = (opt["start"], opt["end"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(opt)

    unique.sort(key=lambda o: (o["start"][:10], o["duration_minutes"], o["start"]))
    return unique


def print_patient_options(
    options, modality_order, facility_tz=None, now_local=None
):
    """Render the patient-facing option list. When facility_tz is given, each
    option's start/end is shown patient-readable; falls back to raw ISO. When
    now_local is given, same-day options get an inline short-notice warning.
    """
    today_iso = (
        now_local.date().isoformat() if now_local is not None else None
    )
    if not options:
        print("\nA single combined appointment for all your exams is not available.")
        print("Would you like to split them into multiple visits?")
        return

    print("\n=== AVAILABLE APPOINTMENT OPTIONS ===")
    for idx, opt in enumerate(options, start=1):
        total_minutes = opt["duration_minutes"]
        hours = total_minutes // 60
        minutes = total_minutes % 60

        modality_sequence = ", ".join(opt["modality_sequence"])
        chain = opt["chain"]

        machine_parts = []
        for modality in opt["modality_sequence"]:
            row = chain.get(modality, {})
            machine = row.get("modality_machine") or "?"
            machine_parts.append(machine)
        machines_str = " >> ".join(machine_parts) if machine_parts else "N/A"

        resource_parts = []
        for modality in opt["modality_sequence"]:
            row = chain.get(modality, {})
            res = row.get("resource_code")
            if res:
                resource_parts.append(f"{modality}: {res}")
        resources_str = ", ".join(resource_parts) if resource_parts else "N/A"

        wait_slots = opt.get("total_wait_slots", 0)
        wait_str = (
            f"{wait_slots} slot(s) = {wait_slots * 15} min patient wait"
            if wait_slots > 0 else "No wait"
        )

        if facility_tz is not None:
            start_display = _format_for_patient(opt["start"], facility_tz)
            end_display = _format_for_patient(opt["end"], facility_tz)
        else:
            start_display = opt["start"]
            end_display = opt["end"]

        print(f"\nOption {idx}:")
        print(f"  Appointment for: {modality_sequence}")
        print(f"  On Machine:      {machines_str}")
        print(f"  Resources:       {resources_str}")
        print(f"  Appointment Start: {start_display}")
        print(f"  Appointment End:   {end_display}")
        print(f"  Total Duration:    {hours} hrs {minutes} mins")
        print(f"  Patient Wait:      {wait_str}")

        if today_iso is not None and opt["start"][:10] == today_iso:
            print(
                "  SHORT NOTICE: this is a same-day appointment. "
                "Please arrive at least 10 minutes early and allow "
                "extra time for travel."
            )
