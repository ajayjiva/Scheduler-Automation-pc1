# resource_scheduler.py (Phase 3.6 pc1 port)
#
# Technician-calendar resolver. Only exercised when a tenant sets
# use_technician_calendar=true. Inview Imaging uses false (availability is
# folded into machineschedule by the reconciler), so this path is NOT
# exercised by the pc1 test data and reads a technicianschedule_v that pc1
# does not yet define. Ported for completeness / future tenants; carried
# forward as-is except that slot dates now come from date_and_time_utc via
# tz_helpers.slot_local_date_str.

from collections import defaultdict

from tz_helpers import slot_local_date_str


def _get_skill_column_for_modality(modality_type: str) -> str:
    """Map modality_type to the skill column in the technician view."""
    mt = (modality_type or "").strip().lower()
    if mt == "mri":
        return "mri"
    if mt == "ct":
        return "ct"
    if mt in ("dx", "xr", "xray", "x-ray"):
        return "dx"
    if mt == "us":
        return "us"
    return mt


def _has_skill(resource_row, skill_col):
    """Normalize the view's Y / NULL skill flags to a boolean."""
    val = resource_row.get(skill_col)
    return val in (1, "1", True, "Y", "y", "YES", "yes", "Yes")


def find_resource_for_block(
    all_resource_rows,
    modality_type,
    date_str,
    slot_seq_start,
    slot_seq_end,
    facility_tz=None,
):
    """Find ONE resource_code available for every slot in the block on the
    facility-local `date_str`, with the correct skill flag. Returns
    {resource_code, resource_slot_ids} or None.
    """
    if facility_tz is None:
        raise ValueError(
            "facility_tz is required so technician rows can be compared "
            "against the facility-local block date."
        )

    skill_col = _get_skill_column_for_modality(modality_type)

    by_resource = defaultdict(list)
    for r in all_resource_rows:
        r_date = slot_local_date_str(r, facility_tz)
        if r_date == date_str:
            by_resource[r["resource_code"]].append(r)

    for resource_code, rows in by_resource.items():
        by_slot = {r["slot_seq"]: r for r in rows}
        slot_ids = []
        ok = True
        for slot_seq in range(slot_seq_start, slot_seq_end + 1):
            r = by_slot.get(slot_seq)
            if not r:
                ok = False
                break
            if r.get("availability", 0) != 1:
                ok = False
                break
            if not _has_skill(r, skill_col):
                ok = False
                break
            slot_ids.append(r["id"])
        if ok:
            return {"resource_code": resource_code, "resource_slot_ids": slot_ids}

    return None
