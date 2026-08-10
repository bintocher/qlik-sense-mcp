"""Tools backed by the Repository API: what apps exist and what is in them.

`get_app_details` also reaches into the Engine for the data model, because
"what is in this app" is the one question that spans both APIs.
"""

from typing import Any, Dict, List, Optional

from . import context
from .context import mcp
from .helpers import (
    _check,
    _engine_serialised,
    _ok,
    _timed,
    _to_tribool,
)
from ..config import (
    DEFAULT_APPS_LIMIT,
    MAX_APPS_LIMIT,
)


@mcp.tool()
@_timed
def get_about() -> str:
    """
    Get Qlik Sense server info (version, build, node type) via QRS `/qrs/about` endpoint.

    Use this to verify connectivity and identify the Qlik Sense release running on the server.
    No parameters. Lightweight call, ~200ms.

    Returns:
        JSON with fields: buildVersion, buildDate, databaseProvider, nodeType, sharedPersistence, requiresBootstrap.
    
    Example:
        Call: {}
        Returns: {"tool_call_seconds": 0.21, "buildVersion": "31.56.2.0",
                  "buildDate": "10/24/2025 09:43:09 AM", "nodeType": 1,
                  "sharedPersistence": true, "requiresBootstrap": false}
    """
    e = _check()
    if e:
        return e
    return _ok(context.repo_api.get_about())



@mcp.tool()
@_timed
def get_apps(
    limit: int = DEFAULT_APPS_LIMIT,
    offset: int = 0,
    name: Optional[str] = None,
    stream: Optional[str] = None,
    published: str = "true",
) -> str:
    """
    List Qlik Sense applications from the QRS Repository (no data load — pure metadata).

    Use this as the entry point to discover apps when the user mentions an app by
    fragment of its name. Always returns published apps only by default; pass
    `published="false"` to include drafts from the user's personal sandbox.

    Args:
        limit: Max number of apps to return. Default 25, hard cap 50. Use pagination
            via `offset` for larger result sets instead of bumping this.
        offset: Number of apps to skip for pagination. Default 0.
        name: Case-insensitive substring filter on app name. No wildcards needed —
            a substring search — `"Rev"` matches `"Revenue 2025"`. Omit to list all apps.
        stream: Case-insensitive substring filter on the publication stream name
            (e.g. `"Finance"`). Omit to search across all streams.
        published: Publication state filter as a string. `"true"` (default) — only
            published apps; `"false"` — unpublished; `"both"` (or any other
            value) — no filter at all, published and unpublished together.

    Returns:
        JSON with `apps` (list of {guid, name, description, stream,
        modified_dttm, reload_dttm}) and `pagination` metadata.
    
    Example (find an app by a fragment of its name):
        Call: {"name": "Sales", "limit": 5}
        Returns: {"tool_call_seconds": 0.53,
                  "apps": [{"guid": "a1b2c3d4-1111-2222-3333-444455556666",
                            "name": "Sales Dashboard", "description": "...",
                            "stream": "Finance",
                            "modified_dttm": "2026-07-20T09:15:00.000Z",
                            "reload_dttm": "2026-07-27T03:00:00.000Z"}],
                  "pagination": {"limit": 5, "offset": 0, "returned": 1,
                                 "total_found": 1, "has_more": false,
                                 "next_offset": null}}

    Example (next page of all published apps):
        Call: {"limit": 25, "offset": 25}
        Returns: {"tool_call_seconds": 0.61, "apps": ["...25 items..."],
                  "pagination": {"limit": 25, "offset": 25, "returned": 25,
                                 "total_found": 1102, "has_more": true,
                                 "next_offset": 50}}
    """
    e = _check()
    if e:
        return e
    lim = min(max(limit or DEFAULT_APPS_LIMIT, 1), MAX_APPS_LIMIT)
    off = max(offset or 0, 0)
    return _ok(context.repo_api.get_comprehensive_apps(lim, off, name, stream,
                                               _to_tribool(published)))



@mcp.tool()
@_timed
@_engine_serialised
def get_app_details(app_id: Optional[str] = None, name: Optional[str] = None) -> str:
    """
    Get app overview — metadata + full list of tables and fields (cardinality, row counts, keys).

    Use this as the second step after `get_apps` to understand the data model before
    writing hypercube expressions. Opens the application with data loaded, which
    populates the server-side cache — any subsequent `engine_create_hypercube`,
    `get_app_field*`, or `get_app_variables` call against the same `app_id` will
    reuse the open connection and run much faster.

    At least one of `app_id` or `name` must be provided. `app_id` is always preferred.

    Args:
        app_id: Application GUID (e.g. `"a1b2c3d4-..."`). Preferred over `name`
            because it uniquely identifies the app. Obtain it from `get_apps`.
        name: App name to look up. Case-insensitive. If multiple apps match, the
            exact match wins over partial matches, then the first result is used.
            Prefer `app_id` when you already know it.

    Returns:
        JSON with `metainfo` (app_id, name, description, stream, modified_dttm,
        reload_dttm), `tables` (summary of each table), and `fields` (every
        non-system, non-hidden field with its table, is_key flag, distinct_values,
        row count, and tags). Tables and fields carrying a `COMMENT TABLE` /
        `COMMENT FIELD` text from the load script also get a `comment` key —
        that is the business description of the column, so read it before
        guessing a field's meaning from its name. The key is absent when the
        script sets no comment.

    Example:
        Call: {"app_id": "a1b2c3d4-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 2.84,
                  "metainfo": {"app_id": "a1b2...", "name": "Sales Dashboard",
                               "stream": "Finance",
                               "reload_dttm": "2026-07-27T03:00:00.000Z"},
                  "warnings": ["Large fact table(s) detected ..."],
                  "tables": [{"name": "Orders", "fields_count": 12,
                              "rows": 91028794, "comment": "Order facts"}],
                  "fields": [{"name": "Amount", "table": "Orders",
                              "is_key": false, "distinct_values": 9797173,
                              "rows": 91028794, "tags": ["$numeric"],
                              "comment": "Order amount, net of refunds"}],
                  "tables_count": 8, "fields_count": 120}

    Read `warnings` first — it tells you which tables are too big to
    aggregate without a set-analysis filter.

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
    """
    e = _check()
    if e:
        return e

    def _resolve():
        if app_id:
            meta = context.repo_api.get_app_by_id(app_id)
            # A transport, auth or server failure is not "no such app".
            # Reporting it as a missing id sends the caller looking for a
            # different app id when the real answer is "Qlik is unreachable"
            # — seen live when the proxy was restarting and every lookup came
            # back as "App not found".
            if isinstance(meta, dict) and meta.get("error"):
                return {
                    "error": f"Repository lookup failed: {meta['error']}",
                    "error_category": "repository_error",
                    "hint": "Qlik did not answer the app lookup. Check that the "
                            "server and (in JWT mode) the virtual proxy are up, "
                            "then retry — the app id may well be correct.",
                }
            if isinstance(meta, dict) and meta.get("id"):
                return {
                    "app_id": meta["id"],
                    "name": meta.get("name", ""),
                    "description": meta.get("description") or "",
                    "stream": (meta.get("stream") or {}).get("name", "") if meta.get("published") else "",
                    "modified_dttm": meta.get("modifiedDate", ""),
                    "reload_dttm": meta.get("lastReloadTime", ""),
                }
            return {"error": "App not found by provided app_id",
                    "error_category": "app_not_found"}
        if name:
            payload = context.repo_api.get_comprehensive_apps(MAX_APPS_LIMIT, 0, name, None, None)
            if isinstance(payload, dict) and payload.get("error"):
                return {
                    "error": f"Repository lookup failed: {payload['error']}",
                    "error_category": "repository_error",
                    "hint": "Qlik did not answer the app search — this is not "
                            "evidence that no app carries this name.",
                }
            apps = payload.get("apps", []) if isinstance(payload, dict) else []
            if not apps:
                return {"error": "No apps found by name",
                        "error_category": "app_not_found"}
            low = name.lower()
            exact = [a for a in apps if a.get("name", "").lower() == low]
            sel = exact[0] if exact else apps[0]
            sel["app_id"] = sel.pop("guid", "")
            return sel
        return {"error": "Either app_id or name must be provided"}

    resolved = _resolve()
    if "error" in resolved:
        return _ok(resolved)
    aid = resolved["app_id"]

    # Get tables and fields via Engine API (WebSocket)
    try:
        app_handle = context.engine_api.ensure_app(aid, no_data=False)
        fields_data = context.engine_api.get_fields(app_handle)
    except Exception as ex:
        fields_data = {"error": str(ex)}

    # Build tables summary from fields data
    tables = []
    fields = []
    if isinstance(fields_data, dict) and "fields" in fields_data:
        raw_fields = fields_data["fields"]
        # Group fields by table
        table_map: Dict[str, List[Dict[str, Any]]] = {}
        for f in raw_fields:
            if f.get("is_system") or f.get("is_hidden"):
                continue
            tname = f.get("table_name", "")
            table_map.setdefault(tname, []).append(f)
            entry = {
                "name": f.get("field_name", ""),
                "table": tname,
                "is_key": f.get("is_key", False),
                "distinct_values": f.get("distinct_values", 0),
                "rows": f.get("rows_count", 0),
                "tags": f.get("tags", []),
            }
            # COMMENT FIELD text, when the load script sets one. Emitted only
            # when non-empty: most apps comment a handful of fields, and an
            # empty key on every field is pure noise in the LLM context.
            if f.get("comment"):
                entry["comment"] = f["comment"]
            fields.append(entry)
        for tname, tfields in table_map.items():
            rows = max((f.get("rows_count", 0) for f in tfields), default=0)
            entry = {
                "name": tname,
                "fields_count": len(tfields),
                "rows": rows,
            }
            comment = next((f.get("table_comment") for f in tfields if f.get("table_comment")), "")
            if comment:
                entry["comment"] = comment
            tables.append(entry)

    # Build performance warnings: huge tables / high-cardinality keys are
    # the main source of hypercube timeouts on this app. Surface them so
    # the LLM filters with set analysis BEFORE building heavy aggregates.
    warnings: List[str] = []
    BIG_TABLE_ROWS = 100_000_000     # 100M
    HUGE_TABLE_ROWS = 500_000_000    # 500M
    HIGH_CARD_FIELD = 1_000_000      # 1M distinct
    big_tables = [t for t in tables if t.get("rows", 0) >= BIG_TABLE_ROWS]
    huge_tables = [t for t in tables if t.get("rows", 0) >= HUGE_TABLE_ROWS]
    high_card_fields = [
        f for f in fields if f.get("distinct_values", 0) >= HIGH_CARD_FIELD
    ]
    if huge_tables:
        count = len(huge_tables)
        max_rows_found = max(t.get("rows", 0) for t in huge_tables)
        warnings.append(
            f"HUGE fact table(s) detected ({count} table(s), largest "
            f"~{max_rows_found:,} rows). NEVER build hypercubes on these "
            f"without a set-analysis filter in every measure (narrow by "
            f"period / category / key). Unfiltered aggregates will time "
            f"out. See engine_create_hypercube docstring for the correct "
            f"set-analysis patterns."
        )
    elif big_tables:
        count = len(big_tables)
        max_rows_found = max(t.get("rows", 0) for t in big_tables)
        warnings.append(
            f"Large fact table(s) detected ({count} table(s), largest "
            f"~{max_rows_found:,} rows). Always filter measures with set "
            f"analysis to limit the period/scope and keep response times "
            f"reasonable."
        )
    if high_card_fields:
        count = len(high_card_fields)
        max_card = max(f.get("distinct_values", 0) for f in high_card_fields)
        warnings.append(
            f"High-cardinality field(s) detected ({count} field(s), "
            f"highest ~{max_card:,} distinct values). Sorting hypercube "
            f"dimensions by these via qSortByExpression forces a full "
            f"sort of the entire field — slow. Prefer narrow "
            f"set-analysis filters and keep max_rows small (15-50) for "
            f"top-N queries."
        )
    has_date = any(
        "$date" in f.get("tags", []) or "$timestamp" in f.get("tags", [])
        for f in fields
    )
    if has_date:
        warnings.append(
            "Date/timestamp fields present. To learn the loaded period, "
            "use engine_create_hypercube with measures `Min([<DimDate>])` "
            "and `Max([<DimDate>])` (no dimensions, max_rows=1), "
            "substituting <DimDate> with a real date field from the "
            "`fields` list below. DO NOT call get_app_field_statistics on "
            "date fields — it computes useless Sum/Avg/Stdev and is "
            "extremely slow on big tables."
        )

    result = {
        "metainfo": {
            "app_id": aid,
            "name": resolved.get("name", ""),
            "description": resolved.get("description", ""),
            "stream": resolved.get("stream", ""),
            "modified_dttm": resolved.get("modified_dttm", ""),
            "reload_dttm": resolved.get("reload_dttm", ""),
        },
        "warnings": warnings,
        "tables": tables,
        "fields": fields,
        "tables_count": len(tables),
        "fields_count": len(fields),
    }
    if isinstance(fields_data, dict) and "error" in fields_data:
        result["engine_error"] = fields_data["error"]
    return _ok(result)


