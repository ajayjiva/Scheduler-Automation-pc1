"""
get_studies.py — resolve a caller-supplied array of study rows into the
scheduler's per-machine input (replaces pc1.orders_v as main.py's input
source; production does not use pc1.orders_v).

The caller supplies one row per **study** (= one procedure), not per
order — a doctor's single order for two procedures now arrives as two
study rows. Each row carries its facility and CPT code(s); `duration`
(minutes) or `required_slots` are optional per-row overrides that always
take precedence over the catalog's standard time for that procedure.

`procedure_code` is mandatory on every row, override or not: it is the
*only* way modality_type is determined (see below), so even a row that
overrides its duration still needs its CPT code to know what kind of
machine to search.

Row shape produced (identical to what pc1.orders_v returned, keyed by
`study_id` instead of `order_id`) so per_machine_resolver.py and
everything downstream needs no further changes:
    study_id, facility_id, modality_type, modality_id (nullable pin),
    pe_facility_id (nullable pin), required_slots, procedure_description

modality_type is always catalog-derived, never caller-supplied
-----------------------------------------------------------
Earlier versions of this module accepted a caller-supplied modality_type
and only warned on a mismatch against the catalog. That was found (via a
live test) to be a real footgun: a caller-declared value can be wrong
(e.g. "XR" typed for a procedure the catalog stores as "CR"), and trusting
it caused correct catalog data to be silently ignored. modality_type is
now taken *only* from the matched pc1.proceduresestimate row(s) -- there
is no input field for it at all. If procedure_code doesn't match anything
in the catalog, there's no way to know the modality, so that study is
skipped with an ERROR (see get_summary_list) rather than guessed at.

Catalog lookup (procedure_code set-equality)
--------------------------------------------
pc1.orders_v matched pc1.proceduresestimate by exact procedure_desc text.
Studies carry CPT codes directly, so this module matches by procedure_code
(text[]) instead: order-insensitive set equality, not the GIN index's raw
`.overlaps()` containment (which would over-match a combo procedure against
a catalog row for just one of its codes). Nothing about how procedure_code
is derived/stored on pc1.proceduresestimate changes -- only the lookup key.
This lookup runs for EVERY study, including ones with a duration/
required_slots override -- the override only replaces the catalog's timing
value, never the modality lookup. Queries are cached per distinct CPT-code
set for the whole run (see _fetch_catalog_matches), so this costs at most
one indexed query per distinct procedure_code combination, not per study.

No facility filter is applied in the query, same as orders_v (migrations/
0009): the Python 4-tier resolver, not SQL, owns facility/machine
precedence, so every matching shape (global + per-facility + per-machine)
must reach it.
"""

from collections import defaultdict


# required_slots_from_minutes is duplicated from
# novaRIS_standardprocedure_scraper.py (not imported -- that module pulls
# in bs4/novaRIS_common at import time, too heavy for a 3-line pure
# function). Keep in sync with that copy if the rounding rule ever changes.
def required_slots_from_minutes(minutes, slot_minutes: int) -> int:
    """ceil(minutes / slot_minutes). Returns 0 for missing/invalid input."""
    if not minutes or int(minutes) <= 0:
        return 0
    return (int(minutes) + slot_minutes - 1) // slot_minutes


# 4-tier precedence over a single candidate row -- ported verbatim from
# get_orders.py's copy, order_id -> study_id.
#   tier 1 -> pe.facility_id == study.facility_id AND pe.modality_id NOT NULL
#   tier 2 -> pe.facility_id == study.facility_id AND pe.modality_id IS NULL
#   tier 3 -> pe.facility_id IS NULL              AND pe.modality_id NOT NULL
#   tier 4 -> pe.facility_id IS NULL              AND pe.modality_id IS NULL
def _row_tier(row):
    pe_facility = row.get("pe_facility_id")
    study_facility = row.get("facility_id")
    modality = row.get("modality_id")

    facility_match = pe_facility == study_facility and pe_facility is not None
    is_global = pe_facility is None
    pinned = modality is not None

    if facility_match and pinned:
        return 1
    if facility_match and not pinned:
        return 2
    if is_global and pinned:
        return 3
    if is_global and not pinned:
        return 4
    return 99  # impossible given how rows are constructed below


def _pick_most_specific_per_study_modality(rows):
    """Keep one row per (study_id, modality_type) -- the most-specific tier.
    Only used to build summary_list; the caller still gets the full
    multi-row set back for per_machine_resolver.py's own tier resolution.
    """
    best = {}
    for row in rows:
        key = (row["study_id"], row["modality_type"])
        tier = _row_tier(row)
        if key not in best or tier < best[key][0]:
            best[key] = (tier, row)
    return [v[1] for v in best.values()]


def _fetch_catalog_matches(supabase, client_id, cache, codes):
    """Fetch pc1.proceduresestimate rows whose procedure_code set exactly
    equals `codes` (order-insensitive), for one client. `cache` is a dict
    shared across the whole run, keyed by frozenset(codes), so repeated
    codes across study rows cost one query.
    """
    key = frozenset(codes)
    if key in cache:
        return cache[key]

    resp = (
        supabase
        .schema("pc1")
        .table("proceduresestimate")
        .select("*")
        .eq("client_id", client_id)
        .eq("is_active", True)
        .overlaps("procedure_code", list(codes))
        .execute()
    )
    matches = [
        r for r in (resp.data or [])
        if set(r.get("procedure_code") or []) == set(codes)
    ]
    cache[key] = matches
    return matches


def get_summary_list(supabase, study_rows, client_id, slot_size_minutes):
    """
    1) Validate all study_rows share one facility_id (hard error -- the
       scheduler can only run against one facility per invocation) and warn
       (not fail) if they don't share one patient_id.
    2) For each study row, look up pc1.proceduresestimate by CPT-code
       set-equality -- always, override or not, since this is the only
       source of modality_type. No catalog match at all -> that study is
       skipped with an ERROR; the rest of the run continues.
    3) If the study also has a duration/required_slots override, replace
       the catalog's required_slots with it and emit a single synthetic
       global-tier row (applies uniformly to every candidate machine,
       ignoring per-machine catalog variation). Otherwise emit one row per
       matching catalog shape (global / per-facility / per-machine / both).
    4) Reduce to one row per (study_id, modality_type) via 4-tier
       precedence, for summary_list only.
    5) Aggregate required_slots per modality (summary_list, legacy shape).

    Returns:
        summary_list: [{modality_type, total_slots}, ...] sorted by slots desc
        rows: the full multi-row set (used by per_machine_resolver.py)
        facility_id: the single validated facility for this run
    """
    facility_ids = {r["facility_id"] for r in study_rows}
    if len(facility_ids) != 1:
        raise ValueError(
            f"studies input must contain exactly one facility_id; "
            f"found {sorted(facility_ids)}"
        )
    facility_id = facility_ids.pop()

    patient_ids = {r["patient_id"] for r in study_rows}
    if len(patient_ids) != 1:
        print(
            f"WARNING: studies input contains {len(patient_ids)} distinct "
            f"patient_ids: {sorted(patient_ids)} -- expected studies for "
            f"exactly one patient."
        )

    codes_cache = {}
    rows = []

    for study in study_rows:
        study_id = study["study_id"]
        duration = study.get("duration")
        required_slots = study.get("required_slots")
        codes = study.get("procedure_code") or []

        matches = _fetch_catalog_matches(supabase, client_id, codes_cache, codes)

        if not matches:
            print(
                f"  ERROR: study {study_id}: procedure_code {codes} has no "
                f"matching pc1.proceduresestimate row (no catalog entry at "
                f"all -- add one, even a bare global row, before this study "
                f"can be scheduled); study excluded."
            )
            continue

        if required_slots is not None or duration is not None:
            slots = (
                required_slots if required_slots is not None
                else required_slots_from_minutes(duration, slot_size_minutes)
            )
            rows.append({
                "study_id": study_id,
                "facility_id": facility_id,
                "modality_type": matches[0].get("modality_type"),
                "modality_id": None,
                "pe_facility_id": None,
                "required_slots": slots,
                "procedure_description": f"OVERRIDE({codes})",
            })
            continue

        for pe in matches:
            rows.append({
                "study_id": study_id,
                "facility_id": facility_id,
                "modality_type": pe.get("modality_type"),
                "modality_id": pe.get("modality_id"),
                "pe_facility_id": pe.get("facility_id"),
                "required_slots": pe.get("required_slots"),
                "procedure_description": pe.get("procedure_desc"),
            })

    most_specific = _pick_most_specific_per_study_modality(rows)

    slot_summary = defaultdict(int)
    for row in most_specific:
        slot_summary[row["modality_type"]] += row["required_slots"]

    summary_list = sorted(
        [
            {"modality_type": m, "total_slots": s}
            for m, s in slot_summary.items()
        ],
        key=lambda x: (-x["total_slots"], x["modality_type"])
    )

    print("\n--- STUDIES SUMMARY ---")
    for item in summary_list:
        print(item)

    print("\n--- RAW STUDIES (DEBUG, most-specific per study/modality) ---")
    for row in most_specific:
        print(
            row.get("study_id"),
            row.get("facility_id"),
            row.get("modality_type"),
            row.get("modality_id"),
            row.get("pe_facility_id"),
            row.get("required_slots"),
            row.get("procedure_description"),
        )

    return summary_list, rows, facility_id
