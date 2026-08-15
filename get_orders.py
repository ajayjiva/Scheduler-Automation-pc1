"""
get_orders.py — read a patient's orders from pc1.orders_v (Phase 3.6 port).

pc1 changes vs the legacy bundle
--------------------------------
- Reads the schema-qualified `pc1.orders_v` (PostgREST default schema is
  public; pc1 must be requested explicitly).
- Column names follow pc1.orders_v (migration 0009), not the legacy text
  names:
      module_type  -> modality_type
      module       -> modality_id     (int FK: per-machine override pin)
      pe_facility  -> pe_facility_id   (int FK: per-facility override tier)
      facility     -> facility_id      (int FK: the order's facility)
  The 4-tier precedence therefore compares ID == ID.
- `facility` returned to the caller is now `facility_id` (int).
"""

from collections import defaultdict


# 4-tier precedence for picking the "most-specific" proceduresestimate shape
# when the multi-row orders_v returns several rows for one (order, modality).
# Lower number = more specific.
#
#   tier 1 -> pe.facility_id == order.facility_id AND pe.modality_id NOT NULL
#   tier 2 -> pe.facility_id == order.facility_id AND pe.modality_id IS NULL
#   tier 3 -> pe.facility_id IS NULL              AND pe.modality_id NOT NULL
#   tier 4 -> pe.facility_id IS NULL              AND pe.modality_id IS NULL
def _row_tier(row):
    pe_facility = row.get("pe_facility_id")
    order_facility = row.get("facility_id")
    modality = row.get("modality_id")

    facility_match = pe_facility == order_facility and pe_facility is not None
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
    return 99  # impossible given the view's WHERE clause


def _pick_most_specific_per_order_modality(rows):
    """Keep one row per (order_id, modality_type) — the most-specific tier.

    Ties within a tier (e.g. two tier-1 per-machine rows for the same order)
    are broken by input iteration order — matching the legacy DISTINCT ON
    arbitrariness.
    """
    best = {}
    for row in rows:
        key = (row["order_id"], row["modality_type"])
        tier = _row_tier(row)
        if key not in best or tier < best[key][0]:
            best[key] = (tier, row)
    return [v[1] for v in best.values()]


def get_summary_list(supabase, patient_id, client_id):
    """
    1) Read all matching proceduresestimate shapes for (client, patient) from
       pc1.orders_v (multi-row: N rows per order when override shapes exist).
    2) Reduce to one row per (order, modality) via 4-tier precedence.
    3) Aggregate required_slots per modality (legacy summary_list).
    4) Return:
       - summary_list: [{modality_type, total_slots}, ...] sorted by slots desc
       - rows: raw orders_v rows (multi-row; used by per_machine_resolver.py)
       - facility_id: the order facility (int) for this patient's orders
    """
    response = (
        supabase
        .schema("pc1")
        .table("orders_v")
        .select("*")
        .eq("client_id", client_id)
        .eq("patient_id", patient_id)
        .execute()
    )

    rows = response.data or []

    most_specific = _pick_most_specific_per_order_modality(rows)

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

    print("\n--- ORDERS SUMMARY ---")
    for item in summary_list:
        print(item)

    facility_id = rows[0].get("facility_id") if rows else None

    # Debug: one line per (order, modality) — the picked most-specific row.
    print("\n--- RAW ORDERS (DEBUG, most-specific per order/modality) ---")
    for row in most_specific:
        print(
            row.get("patient_id"),
            row.get("preferred_language"),
            row.get("facility_id"),
            row.get("modality_type"),
            row.get("modality_id"),
            row.get("pe_facility_id"),
            row.get("required_slots"),
            row.get("procedure_description"),
        )

    return summary_list, rows, facility_id
