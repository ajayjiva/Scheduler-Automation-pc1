"""
Cumulative open-slot helpers (Phase 3.6 pc1 port).

`compute_cumm_open_slots_below` / `compute_cumm_open_slots_above` walk
consecutive `availability=1` rows within one machine-day and record the run
length on each row, so the scheduler can find contiguous blocks of the
required length without re-walking.

Grouping is by (modality_machine, facility-local date). Because slot instants
are stored UTC (`date_and_time_utc`), we convert to the facility TZ before
extracting the date so late-evening slots aren't mis-grouped into the next
UTC day.

Results are written to in-memory `calc_cumm_below` / `calc_cumm_above` fields.

pc1 change vs legacy: the slot instant is read from `date_and_time_utc`.
"""

from tz_helpers import slot_local_date_str


def compute_cumm_open_slots_below(rows, facility_tz):
    """Consecutive open slots BELOW (forward in time) for each slot, stopping
    at the first unavailable slot. Groups by (modality_machine, local-date).
    Writes 'calc_cumm_below'.
    """
    groups = {}
    for r in rows:
        day = slot_local_date_str(r, facility_tz)
        key = (r["modality_machine"], day)
        groups.setdefault(key, []).append(r)

    for key, group_rows in groups.items():
        group_rows.sort(key=lambda r: r["date_and_time_utc"])
        n = len(group_rows)

        for i in reversed(range(n)):
            if group_rows[i]["availability"] == 0:
                group_rows[i]["calc_cumm_below"] = 0
            else:
                if i < n - 1 and group_rows[i + 1]["availability"] == 1:
                    group_rows[i]["calc_cumm_below"] = (
                        group_rows[i + 1]["calc_cumm_below"] + 1
                    )
                else:
                    group_rows[i]["calc_cumm_below"] = 1

    return rows


def compute_cumm_open_slots_above(rows, facility_tz):
    """Consecutive open slots ABOVE (backward in time) for each slot, stopping
    at the first unavailable slot. Groups by (modality_machine, local-date).
    Writes 'calc_cumm_above'.
    """
    groups = {}
    for r in rows:
        day = slot_local_date_str(r, facility_tz)
        key = (r["modality_machine"], day)
        groups.setdefault(key, []).append(r)

    for key, group_rows in groups.items():
        group_rows.sort(key=lambda r: r["date_and_time_utc"])
        n = len(group_rows)

        for i in range(n):
            if group_rows[i]["availability"] == 0:
                group_rows[i]["calc_cumm_above"] = 0
            else:
                if i > 0 and group_rows[i - 1]["availability"] == 1:
                    group_rows[i]["calc_cumm_above"] = (
                        group_rows[i - 1]["calc_cumm_above"] + 1
                    )
                else:
                    group_rows[i]["calc_cumm_above"] = 1

    return rows
