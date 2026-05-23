"""
client_context.py

Resolves the active tenant (`client_id`) for every script in the project
and exposes the corresponding row from the `clientparameters` table.

Design
------
- The DEFAULT client_id is 1001 (Inview Imagine — see
  migrations/add_multi_tenant_foundation.sql). Future tenants get
  1002, 1003, ...
- Three ways to set the active client (in priority order):

      1. Highest priority — CLI flag `--client-id=NNNN`
         Use this for one-off cross-tenant work without editing .env.

      2. Env variable CLIENT_ID=NNNN in .env
         The normal everyday source. Each deployed environment sets
         this once and forgets it.

      3. Fallback — DEFAULT_CLIENT_ID = 1001
         Used only if neither CLI nor env supplies a value. Keeps
         existing single-tenant deployments running transparently.

- Every script in the repo should call `resolve_client_id()` once at
  startup, store the result, and pass it explicitly to every Supabase
  read/write. This is the single point of "which tenant am I?".

- For business-rule parameters (wait_threshold, slot_size, etc.),
  call `get_client_parameters(supabase, client_id, facility)` and pass
  the returned dict around. Cached per (client_id, facility) to avoid
  repeated DB hits.

  Per-facility overrides
  ----------------------
  `clientparameters` stores at most one default row per client
  (facility IS NULL, all params NOT NULL) plus zero or more override
  rows (facility = '<name>', any subset of params populated). The
  helper fetches both rows in a single query and merges them column-
  by-column: the override's non-NULL value wins, otherwise the
  default's value is used. Pass `facility=None` (or omit it) to get
  just the default row.

  Identity vs. configuration
  --------------------------
  `client_name` lives in a separate `clients` table (it's the FK
  target for every tenant table — see
  migrations/add_facility_overrides_to_clientparameters.sql for why).
  The merged dict returned by `get_client_parameters()` includes
  `client_name` from `clients` so callers don't need to know about
  the split.

CLI helper
----------
Scripts that use argparse can wire up the `--client-id` flag with one
call:

    parser = argparse.ArgumentParser(...)
    add_client_id_arg(parser)
    args = parser.parse_args()
    client_id = resolve_client_id(args)

Other scripts (no argparse) can use:

    client_id = resolve_client_id()        # env or default
"""

from dotenv import load_dotenv
load_dotenv()

import os
import threading

DEFAULT_CLIENT_ID = 1001
ENV_VAR = "CLIENT_ID"

# Cache of merged clientparameters dicts keyed by (client_id, facility).
# Populated lazily by get_client_parameters(). Thread-safe via a lock so
# parallel scrapers (e.g. wizard-fetch ThreadPoolExecutors) all share
# one read. `facility` is normalized to None when omitted, so the
# default row gets cached under (client_id, None).
_PARAM_CACHE: dict = {}
_PARAM_LOCK = threading.Lock()

# Identifier / audit columns that should NOT participate in the
# default+override merge. The override row's values for these are
# meaningless to callers (they ask "what's slot_size for this
# facility?", not "what's the override row's PK?"). The default row
# wins for these so callers always see the canonical client metadata.
_NON_MERGEABLE_COLUMNS = frozenset({
    "id", "client_id", "facility", "client_name",
    "creation_date", "last_update_date",
})


# ── CLI integration ──────────────────────────────────────────────────────

def add_client_id_arg(parser) -> None:
    """
    Add the `--client-id` flag to an argparse.ArgumentParser. Optional;
    when omitted the script falls back to env var, then DEFAULT_CLIENT_ID.
    """
    parser.add_argument(
        "--client-id",
        type=int,
        default=None,
        help=(
            f"Tenant client_id (default: ${ENV_VAR} env var, then "
            f"{DEFAULT_CLIENT_ID})."
        ),
    )


def resolve_client_id(args=None) -> int:
    """
    Effective client_id for this script run.

    Priority:
        1. args.client_id if it's set (CLI flag)
        2. CLIENT_ID env var
        3. DEFAULT_CLIENT_ID
    """
    if args is not None and getattr(args, "client_id", None) is not None:
        return int(args.client_id)
    raw = os.getenv(ENV_VAR)
    if raw:
        return int(raw)
    return DEFAULT_CLIENT_ID


# ── clientparameters lookup ─────────────────────────────────────────────

def _merge_default_and_override(default_row: dict, override_row: dict) -> dict:
    """
    Combine the default row (facility IS NULL) with a facility-specific
    override row. For every parameter column the override wins when its
    value is non-NULL; otherwise the default's value is used. Identifier
    and audit columns (see _NON_MERGEABLE_COLUMNS) always come from the
    default — callers that need the override row's PK / facility name
    should query directly.
    """
    merged = dict(default_row)
    for key, val in override_row.items():
        if key in _NON_MERGEABLE_COLUMNS:
            continue
        if val is not None:
            merged[key] = val
    return merged


def get_client_parameters(supabase, client_id: int, facility: str | None = None) -> dict:
    """
    Return the effective clientparameters dict for `(client_id, facility)`.

    Always reads the default row (facility IS NULL). When `facility` is
    given, also reads the override row for that facility (if any) and
    merges per-column: override's non-NULL values win, default fills in
    the rest. When `facility` is None, returns just the default row.

    Cached by `(client_id, facility)`. Raises RuntimeError if the
    default row is missing (likely missing migration or wrong
    client_id).
    """
    cache_key = (client_id, facility)
    with _PARAM_LOCK:
        if cache_key in _PARAM_CACHE:
            return _PARAM_CACHE[cache_key]

        # Tenant identity (client_name) lives in `clients` — separate
        # from configuration so the FK target stays unique while
        # clientparameters holds multiple rows per client. See
        # migrations/add_facility_overrides_to_clientparameters.sql.
        client_row = (
            supabase.table("clients")
            .select("client_id,client_name")
            .eq("client_id", client_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
            .data or [None]
        )[0]
        if client_row is None:
            raise RuntimeError(
                f"No clients row for client_id={client_id}. Register "
                f"the tenant in `clients` before its first "
                f"clientparameters row. See "
                f"migrations/add_facility_overrides_to_clientparameters.sql "
                f"for the onboarding sequence."
            )

        # Two simple queries — once per (client, facility) per process,
        # so the extra round-trip is negligible and we avoid PostgREST
        # OR-filter escaping issues if a facility name ever contains
        # commas, quotes, or parens.
        default_row = (
            supabase.table("clientparameters")
            .select("*")
            .eq("client_id", client_id)
            .is_("facility", "null")
            .eq("is_active", True)
            .limit(1)
            .execute()
            .data or [None]
        )[0]
        if default_row is None:
            raise RuntimeError(
                f"No default clientparameters row (facility IS NULL) "
                f"found for client_id={client_id}. After registering "
                f"the tenant in `clients`, insert a default-parameters "
                f"row with all params populated."
            )

        override_row = None
        if facility is not None:
            override_row = (
                supabase.table("clientparameters")
                .select("*")
                .eq("client_id", client_id)
                .eq("facility", facility)
                .eq("is_active", True)
                .limit(1)
                .execute()
                .data or [None]
            )[0]

        merged = (
            _merge_default_and_override(default_row, override_row)
            if override_row is not None
            else dict(default_row)
        )
        # Surface client_name so callers (e.g. main.py debug print)
        # don't need to know about the clients table.
        merged["client_name"] = client_row.get("client_name")

        _PARAM_CACHE[cache_key] = merged
        return merged


def get_param(supabase, client_id: int, key: str, default=None,
              facility: str | None = None):
    """
    Convenience: read one column from the merged clientparameters dict
    for `(client_id, facility)`. Returns `default` if the column is
    null or missing after the default+override merge.
    """
    row = get_client_parameters(supabase, client_id, facility)
    val = row.get(key)
    return default if val is None else val
