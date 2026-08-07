"""
Per-machine procedure-estimate resolver (Phase 3.6 pc1 port).

Translates the multi-row pc1.orders_v shape (one row per matching
proceduresestimate shape) into the structures the scheduler needs:

    - resolve_per_machine_slots(...)  -> {modality_type: {modality_id: total_slots}}
    - has_per_machine_variation(...)  -> bool gate for the per-machine path
    - derive_machines_from_machineschedule(...) -> {modality_type: [modality_id]}
    - enumerate_machine_combinations(...) -> combinations sorted by total slots

pc1 changes vs the legacy bundle
--------------------------------
The candidate-machine identity is `modality_id` (the int FK), NOT the
`modality_machine` name. pc1.orders_v exposes the per-machine override pin as
`modality_id` and the per-facility tier as `pe_facility_id` (both int FKs), so
every tier comparison is ID == ID. The machine inventory is likewise keyed on
`modality_id` (from machineschedule_v). The human-readable machine name
(`modality_machine`) is carried on the slot rows for display only and never
used as a key here.

Pure / side-effect free / no DB access.
"""

from collections import defaultdict
from itertools import product


# ── Resolution ────────────────────────────────────

# 4-tier precedence over a single orders_v row, for one candidate machine
# (identified by modality_id). Lower index = more specific = preferred.
#   tier 1 -> pe.facility_id == order.facility_id AND pe.modality_id == candidate
#   tier 2 -> pe.facility_id == order.facility_id AND pe.modality_id IS NULL
#   tier 3 -> pe.facility_id IS NULL              AND pe.modality_id == candidate
#   tier 4 -> pe.facility_id IS NULL              AND pe.modality_id IS NULL
def _row_tier(row, order_facility_id, candidate_modality_id):
    row_facility_match = row.get("pe_facility_id") == order_facility_id
    row_global = row.get("pe_facility_id") is None
    row_pinned_to_machine = row.get("modality_id") == candidate_modality_id
    row_machine_null = row.get("modality_id") is None

    if row_facility_match and row_pinned_to_machine:
        return 1
    if row_facility_match and row_machine_null:
        return 2
    if row_global and row_pinned_to_machine:
        return 3
    if row_global and row_machine_null:
        return 4
    return None  # row doesn't apply to this (facility, machine)


def _required_slots_for(rows_for_order, order_facility_id, candidate_modality_id):
    """Resolve required_slots for one (order, candidate machine) via the most
    specific applicable tier, or None if no shape applies (data-quality gap).
    """
    best_tier = None
    best_slots = None
    for row in rows_for_order:
        tier = _row_tier(row, order_facility_id, candidate_modality_id)
        if tier is None:
            continue
        if best_tier is None or tier < best_tier:
            best_tier = tier
            best_slots = row.get("required_slots")
    return best_slots


def resolve_per_machine_slots(view_rows, machines_by_modality):
    """Compute per-(modality, machine) total required_slots.

    Args:
        view_rows: list of pc1.orders_v dicts (multi-row shape). Keys used:
            order_id, facility_id, modality_type, modality_id (the pe pin;
            may be None), pe_facility_id (may be None), required_slots.
        machines_by_modality: {modality_type: [modality_id, ...]} from
            derive_machines_from_machineschedule.

    Returns (per_machine_slots, unresolvable):
        per_machine_slots: {modality_type: {modality_id: total_slots}}
        unresolvable: [(modality_type, modality_id, order_id), ...] machines
            that cannot serve some order (excluded from combinations).
    """
    rows_by_order = defaultdict(list)
    order_facility = {}
    order_modality = {}
    for row in view_rows:
        oid = row["order_id"]
        rows_by_order[oid].append(row)
        order_facility[oid] = row.get("facility_id")
        order_modality[oid] = row.get("modality_type")

    orders_by_modality = defaultdict(list)
    for oid, modality in order_modality.items():
        orders_by_modality[modality].append(oid)

    per_machine_slots = {}
    unresolvable = []

    for modality, candidate_machines in machines_by_modality.items():
        modality_orders = orders_by_modality.get(modality, [])
        if not modality_orders:
            continue  # patient has no orders for this modality

        modality_totals = {}
        for machine in candidate_machines:
            total = 0
            failed_order = None
            for oid in modality_orders:
                slots = _required_slots_for(
                    rows_by_order[oid], order_facility[oid], machine
                )
                if slots is None:
                    failed_order = oid
                    break
                total += slots

            if failed_order is not None:
                unresolvable.append((modality, machine, failed_order))
                continue

            modality_totals[machine] = total

        if modality_totals:
            per_machine_slots[modality] = modality_totals

    return per_machine_slots, unresolvable


# ── Detection ────────────────────────────────────

def has_per_machine_variation(per_machine_slots):
    """True if any modality has heterogeneous slot counts across machines."""
    for machine_totals in per_machine_slots.values():
        if len(set(machine_totals.values())) > 1:
            return True
    return False


# ── Machine inventory ───────────────────────────

def derive_machines_from_machineschedule(schedule_rows):
    """Return {modality_type: sorted [modality_id, ...]}.

    Derived from machineschedule_v rows already loaded by main.py. Keyed on
    modality_id (the int FK) so the resolver's tier comparison matches the
    orders_v override pin. Any modality/machine present in the loaded window
    is a valid candidate (the generator only emits slots for active machines).
    """
    machines = defaultdict(set)
    for row in schedule_rows:
        modality = row.get("modality_type")
        machine = row.get("modality_id")
        if modality is None or machine is None:
            continue
        machines[modality].add(machine)
    return {mod: sorted(m) for mod, m in machines.items()}


# ── Enumeration ────────────────────────────────

def enumerate_machine_combinations(per_machine_slots):
    """Yield each (modality -> modality_id) combination with anchor metadata,
    sorted by ascending total slots (most efficient flow first).

    Anchor = the (modality, machine) with the highest required_slots. Ties in
    modality_order broken alphabetically on modality name for determinism.

    Combination dict shape:
        {assignment: {modality: modality_id}, slots: {modality: n},
         total_slots, anchor_modality, anchor_machine, modality_order}
    """
    modalities = sorted(per_machine_slots.keys())
    machine_choices = [list(per_machine_slots[m].keys()) for m in modalities]

    combos = []
    for choice in product(*machine_choices):
        assignment = dict(zip(modalities, choice))
        slots = {m: per_machine_slots[m][choice[i]]
                 for i, m in enumerate(modalities)}
        total = sum(slots.values())

        modality_order = sorted(
            modalities,
            key=lambda m: (-slots[m], m),
        )
        anchor_modality = modality_order[0]
        combos.append({
            "assignment": assignment,
            "slots": slots,
            "total_slots": total,
            "anchor_modality": anchor_modality,
            "anchor_machine": assignment[anchor_modality],
            "modality_order": modality_order,
        })

    combos.sort(key=lambda c: (
        c["total_slots"],
        tuple(c["assignment"][m] for m in modalities),
    ))
    return combos
