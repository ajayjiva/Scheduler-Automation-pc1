"""
facility_settings.py

Resolve the per-(client, facility) business settings the scheduling engine
needs, from pc1.clients (tenant default) merged with pc1.facilities
(per-facility override).

WHY THIS EXISTS
---------------
The legacy engine read these from a `clientparameters` table via
`client_context.get_client_parameters()`. pc1 has NO clientparameters table
-- settings live as columns on pc1.clients and pc1.facilities, with the
facility value overriding the client default when non-NULL (the same merge
`generate_machineschedule.py` does). This module is the engine-side reader
of that shape.

Settings returned (merged dict), keyed for the engine:
    slot_size               facilities -> clients
    opening_time            facilities -> clients   ('HH:MM:SS' string)
    closing_time            facilities -> clients   ('HH:MM:SS' string)
    advance_booking_days    facilities -> clients
    timezone                facilities -> clients   (IANA name; NOT NULL on facilities)
    client_name             clients
    wait_threshold          merged, default 0       (column may not exist in pc1)
    use_technician_calendar merged, default False   (column may not exist in pc1)

`wait_threshold` and `use_technician_calendar` are read defensively: no pc1
script writes them today and they may be absent from the schema, so we
SELECT '*' and `.get()` them with safe defaults rather than naming them in
the projection (which would 400 if the column doesn't exist).

Schema-qualified: pc1 is not PostgREST's default schema, so every call goes
through `supabase.schema("pc1")`.
"""

PC1_SCHEMA = "pc1"

# Parameter columns merged facility-over-client. Identity/audit columns are
# never merged -- client_name is sourced from the client row explicitly.
_MERGE_KEYS = (
    "slot_size",
    "opening_time",
    "closing_time",
    "advance_booking_days",
    "timezone",
    "wait_threshold",
    "use_technician_calendar",
)


def _table(supabase, name):
    return supabase.schema(PC1_SCHEMA).table(name)


def get_facility_settings(supabase, client_id: int, facility_id: int) -> dict:
    """Return the merged settings dict for one (client, facility).

    Reads the full pc1.clients row (by id) and the full pc1.facilities row
    (by id), then for each parameter key takes the facility value when it is
    non-NULL, else the client value. `client_name` always comes from the
    client row.

    Raises RuntimeError if either row is missing (wrong id / unseeded tenant).
    """
    client_row = (
        _table(supabase, "clients")
        .select("*")
        .eq("id", client_id)
        .limit(1)
        .execute()
        .data or [None]
    )[0]
    if client_row is None:
        raise RuntimeError(
            f"No pc1.clients row for id={client_id}. Register the tenant "
            f"before scheduling."
        )

    facility_row = (
        _table(supabase, "facilities")
        .select("*")
        .eq("id", facility_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
        .data or [None]
    )[0]
    if facility_row is None:
        raise RuntimeError(
            f"No pc1.facilities row for id={facility_id} under "
            f"client_id={client_id}. The patient's orders reference a "
            f"facility that isn't seeded."
        )

    merged = {}
    for key in _MERGE_KEYS:
        fac_val = facility_row.get(key)
        merged[key] = fac_val if fac_val is not None else client_row.get(key)

    # Safe defaults for the optional behavior flags (columns may be absent
    # in pc1 -> .get() returned None above).
    if merged.get("wait_threshold") is None:
        merged["wait_threshold"] = 0
    merged["use_technician_calendar"] = bool(merged.get("use_technician_calendar"))

    merged["client_name"] = client_row.get("client_name")
    merged["facility_id"] = facility_id
    return merged
