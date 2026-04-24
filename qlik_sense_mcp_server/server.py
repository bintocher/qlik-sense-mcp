"""Main MCP Server for Qlik Sense APIs – rewritten with FastMCP."""

import asyncio
import functools
import json
import ssl
import sys
import os
import re
import time
from typing import Any, Dict, List, Optional

# Ensure UTF-8 encoding on Windows (must be before any I/O)
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from mcp.server.fastmcp import FastMCP

from .config import (
    QlikSenseConfig,
    DEFAULT_APPS_LIMIT,
    MAX_APPS_LIMIT,
    DEFAULT_FIELD_LIMIT,
    MAX_FIELD_LIMIT,
    DEFAULT_HYPERCUBE_MAX_ROWS,
    DEFAULT_FIELD_FETCH_SIZE,
    MAX_FIELD_FETCH_SIZE,
    DEFAULT_TICKET_TIMEOUT,
    AUTH_MODE_JWT,
)
from .repository_api import QlikRepositoryAPI
from .engine_api import QlikEngineAPI
from .jwt_session import JwtSession
from .utils import generate_xrfkey
from . import __version__

import httpx
import logging
from dotenv import load_dotenv

# Initialize logging configuration early
load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_logging_level = getattr(logging, LOG_LEVEL, logging.INFO)
if not logging.getLogger().handlers:
    handler = logging.StreamHandler(stream=sys.stderr)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(_logging_level)
logger = logging.getLogger(__name__)

# ─── Globals initialised once ───────────────────────────────────────────────

config: Optional[QlikSenseConfig] = None
repo_api: Optional[QlikRepositoryAPI] = None
engine_api: Optional[QlikEngineAPI] = None
jwt_session: Optional[JwtSession] = None


def _init_clients():
    global config, repo_api, engine_api, jwt_session
    try:
        config = QlikSenseConfig.from_env()
        config.validate_runtime()

        if config.auth_mode == AUTH_MODE_JWT:
            jwt_session = JwtSession(config)
            repo_api = QlikRepositoryAPI(config, jwt_session=jwt_session)
            engine_api = QlikEngineAPI(config, jwt_session=jwt_session)
            logger.info(
                "Qlik Sense API clients initialised (JWT mode via virtual proxy '/%s')",
                config.virtual_proxy_prefix,
            )
        else:
            jwt_session = None
            repo_api = QlikRepositoryAPI(config)
            engine_api = QlikEngineAPI(config)
            logger.info(
                "Qlik Sense API clients initialised (certificate mode, user=%s\\%s)",
                config.user_directory, config.user_id,
            )
    except Exception as e:
        logger.warning("Failed to init Qlik API clients: %s", e)


_init_clients()

# ─── FastMCP server ─────────────────────────────────────────────────────────

mcp = FastMCP(
    "qlik-sense-mcp-server",
    host="127.0.0.1",
    port=8000,
)


def _err(msg: str, **extra: Any) -> str:
    d = {"error": msg}
    d.update(extra)
    return json.dumps(d, indent=2, ensure_ascii=False)


def _ok(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _check() -> Optional[str]:
    """Return error string if clients are not ready, else None."""
    if repo_api is None:
        return _err("Qlik Sense configuration missing – set QLIK_SERVER_URL, QLIK_USER_DIRECTORY, QLIK_USER_ID etc.")
    return None


def _timed(func):
    """
    Decorator for MCP tools: measures wall-clock time and injects
    `tool_call_seconds` as the first key of the JSON response.

    Works with tools that return a JSON string (via _ok / _err).
    If the result is not a JSON dict, wraps it into one.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
        except Exception as ex:
            elapsed = round(time.monotonic() - t0, 3)
            logger.exception("Tool %s raised after %.3fs", func.__name__, elapsed)
            return json.dumps(
                {
                    "tool_call_seconds": elapsed,
                    "error": str(ex) or repr(ex),
                    "error_type": type(ex).__name__,
                    "tool": func.__name__,
                },
                indent=2,
                ensure_ascii=False,
            )
        elapsed = round(time.monotonic() - t0, 3)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except Exception:
                return json.dumps(
                    {"tool_call_seconds": elapsed, "result": result},
                    indent=2,
                    ensure_ascii=False,
                )
            if isinstance(parsed, dict):
                new_dict = {"tool_call_seconds": elapsed}
                new_dict.update(parsed)
                return json.dumps(new_dict, indent=2, ensure_ascii=False)
            return json.dumps(
                {"tool_call_seconds": elapsed, "result": parsed},
                indent=2,
                ensure_ascii=False,
            )
        return json.dumps(
            {"tool_call_seconds": elapsed, "result": result},
            indent=2,
            ensure_ascii=False,
        )
    return wrapper


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y"):
            return True
        if v in ("false", "0", "no", "n"):
            return False
    return default


def _wildcard_to_regex(pattern: str, case_sensitive: bool) -> re.Pattern:
    escaped = re.escape(pattern).replace("\\*", ".*").replace("%", ".*")
    return re.compile(f"^{escaped}$", 0 if case_sensitive else re.IGNORECASE)


def _create_httpx_client() -> httpx.Client:
    if config.verify_ssl:
        ssl_context = ssl.create_default_context()
        if config.ca_cert_path:
            ssl_context.load_verify_locations(config.ca_cert_path)
    else:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    cert = None
    if config.client_cert_path and config.client_key_path:
        cert = (config.client_cert_path, config.client_key_path)
    return httpx.Client(
        verify=ssl_context if config.verify_ssl else False,
        cert=cert,
        timeout=DEFAULT_TICKET_TIMEOUT,
    )


def _get_qlik_ticket() -> Optional[str]:
    ticket_url = f"{config.server_url}:{config.proxy_port}/qps/ticket"
    ticket_data = {
        "UserDirectory": config.user_directory,
        "UserId": config.user_id,
        "Attributes": [],
    }
    xrfkey = generate_xrfkey()
    try:
        client = _create_httpx_client()
        try:
            resp = client.post(
                ticket_url,
                json=ticket_data,
                headers={"Content-Type": "application/json", "X-Qlik-Xrfkey": xrfkey},
                params={"xrfkey": xrfkey},
            )
            resp.raise_for_status()
            ticket = resp.json().get("Ticket")
            if not ticket:
                raise ValueError("Ticket not found in response")
            return ticket
        finally:
            client.close()
    except Exception as e:
        logger.error("Failed to get ticket: %s", e)
        return None


def _get_app_metadata_via_proxy(app_id: str, ticket: str) -> Dict[str, Any]:
    server_url = config.server_url
    if config.http_port:
        server_url = f"{server_url}:{config.http_port}"
    metadata_url = f"{server_url}/api/v1/apps/{app_id}/data/metadata?qlikTicket={ticket}"
    xrfkey = generate_xrfkey()
    try:
        client = _create_httpx_client()
        try:
            resp = client.get(metadata_url, headers={"X-Qlik-Xrfkey": xrfkey}, params={"xrfkey": xrfkey})
            resp.raise_for_status()
            return _filter_metadata(resp.json())
        finally:
            client.close()
    except Exception as e:
        logger.error("Failed to get app metadata: %s", e)
        return {"error": str(e)}


def _filter_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    fields_to_remove = {
        "is_system", "is_hidden", "is_semantic", "distinct_only",
        "is_locked", "always_one_selected", "is_numeric", "hash",
        "tags", "has_section_access", "tables_profiling_data",
        "is_direct_query_mode", "usage", "reload_meta", "static_byte_size",
        "byte_size", "no_of_key_fields",
    }
    qlik_reserved = {"$Field", "$Table", "$Rows", "$Fields", "$FieldNo", "$Info"}

    def _walk(obj):
        if isinstance(obj, dict):
            filtered = {}
            for k, v in obj.items():
                if k in fields_to_remove:
                    continue
                if isinstance(v, dict) and (v.get("is_system") or v.get("is_hidden")):
                    continue
                if k == "cardinal":
                    filtered["unique_count"] = v
                    continue
                filtered[k] = _walk(v)
            return filtered
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and "name" in obj[0]:
                return [_walk(i) for i in obj if i.get("name") not in qlik_reserved]
            return [_walk(i) for i in obj]
        return obj

    f = _walk(metadata)
    result = {}
    if "fields" in f:
        result["fields"] = f["fields"]
    if "tables" in f:
        result["tables"] = f["tables"]
    return result


# ═══════════════════════════════════════════════════════════════════════════
# TOOLS — Repository API
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
@_timed
def get_about() -> str:
    """
    Get Qlik Sense server info (version, build, node type) via QRS `/qrs/about` endpoint.

    Use this to verify connectivity and identify the Qlik Sense release running on the server.
    No parameters. Lightweight call, ~200ms.

    Returns:
        JSON with fields: buildVersion, buildDate, databaseProvider, nodeType, sharedPersistence, requiresBootstrap.
    """
    e = _check()
    if e:
        return e
    return _ok(repo_api.get_about())


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
            published apps; `"false"` — unpublished; any other value — both.

    Returns:
        JSON with `apps` (list of {guid, name, stream, description, modifiedDate,
        lastReloadTime, published, fileSize}) and `pagination` metadata.
    """
    e = _check()
    if e:
        return e
    lim = min(max(limit or DEFAULT_APPS_LIMIT, 1), MAX_APPS_LIMIT)
    off = max(offset or 0, 0)
    return _ok(repo_api.get_comprehensive_apps(lim, off, name, stream, _to_bool(published, True)))


@mcp.tool()
@_timed
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
        row count, and tags). Use the `fields` list to check field names and
        cardinalities before writing hypercube dimensions.
    """
    e = _check()
    if e:
        return e

    def _resolve():
        if app_id:
            meta = repo_api.get_app_by_id(app_id)
            if isinstance(meta, dict) and meta.get("id"):
                return {
                    "app_id": meta["id"],
                    "name": meta.get("name", ""),
                    "description": meta.get("description") or "",
                    "stream": (meta.get("stream") or {}).get("name", "") if meta.get("published") else "",
                    "modified_dttm": meta.get("modifiedDate", ""),
                    "reload_dttm": meta.get("lastReloadTime", ""),
                }
            return {"error": "App not found by provided app_id"}
        if name:
            payload = repo_api.get_comprehensive_apps(MAX_APPS_LIMIT, 0, name, None, None)
            apps = payload.get("apps", []) if isinstance(payload, dict) else []
            if not apps:
                return {"error": "No apps found by name"}
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
        app_handle = engine_api.ensure_app(aid, no_data=False)
        fields_data = engine_api.get_fields(app_handle)
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
            fields.append({
                "name": f.get("field_name", ""),
                "table": tname,
                "is_key": f.get("is_key", False),
                "distinct_values": f.get("distinct_values", 0),
                "rows": f.get("rows_count", 0),
                "tags": f.get("tags", []),
            })
        for tname, tfields in table_map.items():
            rows = max((f.get("rows_count", 0) for f in tfields), default=0)
            tables.append({
                "name": tname,
                "fields_count": len(tfields),
                "rows": rows,
            })

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


# ═══════════════════════════════════════════════════════════════════════════
# TOOLS — Engine API
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
@_timed
def get_app_script(app_id: str) -> str:
    """
    Get the full load script (LOAD ... FROM ... statements) of a Qlik Sense app.

    Use this to understand how data is ingested into the app — source systems,
    transformations, joins, variables, and section access. Read this BEFORE
    writing non-trivial set analysis: the script reveals field renames, data
    model shape, and any `$(variable)` definitions used in expressions.

    Args:
        app_id: Application GUID. Required. Get it from `get_apps` or
            `get_app_details`.

    Returns:
        JSON with `qScript` (the full script as a single string),
        `script_length` (character count), and `app_id`.
    """
    e = _check()
    if e:
        return e
    try:
        # no_data=False so the cached connection is reusable for later data calls
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        script = engine_api.get_script(app_handle)
        return _ok({"qScript": script, "app_id": app_id, "script_length": len(script) if script else 0})
    except Exception as ex:
        return _err(str(ex), app_id=app_id)


@mcp.tool()
@_timed
def get_app_field_statistics(
    app_id: str,
    field_name: str,
    full: bool = False,
) -> str:
    """
    Compute statistics for a single field via a measures-only hypercube.

    DEFAULT (LIGHT) MODE — fast on any table size, returns:
        unique_values, total_count, non_null_count, min_value, max_value,
        null_percentage, completeness_percentage.

    FULL MODE (`full=True`) — adds avg, sum, median, mode, std_deviation.
    These extra measures are EXTREMELY SLOW on large fact tables (>100M
    rows) and meaningless for date/text fields. Use only when you actually
    need them on a small dimension table.

    DO NOT CALL THIS ON DATE FIELDS to learn the loaded period — sum/avg
    of timestamps is nonsense and slow. Instead use `engine_create_hypercube`
    with measures `Min([YourDateField])` and `Max([YourDateField])`,
    no dimensions, `max_rows=1`. Same for "give me a couple sample values"
    — use `get_app_field` (which itself falls back to a hypercube on
    high-cardinality fields).

    PERFORMANCE: even in light mode, calling this on a 500M-row fact table
    can still take tens of seconds — Engine has to count nulls. If you
    already have `distinct_values` and row count from `get_app_details`,
    you usually don't need this tool at all.

    Args:
        app_id: Application GUID. Required.
        field_name: Exact field name as it appears in the data model. No square
            brackets — pass `"<FieldName>"`, NOT `"[<FieldName>]"`. Get valid field
            names from `get_app_details` (`fields[*].name`).
        full: If True, also compute avg/sum/median/mode/stdev. Default False.
            Only enable for small (<10M rows) numeric fields.

    Returns:
        JSON with unique_values, total_count, non_null_count, min_value,
        max_value, null_percentage, completeness_percentage. If `full=True`,
        also avg_value/sum_value/median_value/mode_value/std_deviation. Each
        stat is `{text, numeric, is_numeric}`.
    """
    e = _check()
    if e:
        return e
    try:
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        result = engine_api.get_field_statistics(app_handle, field_name, light=not full)
        return _ok(result)
    except Exception as ex:
        return _err(str(ex))


@mcp.tool()
@_timed
def engine_get_field_range(app_id: str, field_name: str) -> str:
    """
    Lightning-fast bounds query for a single field: distinct count + min + max.

    Use this BEFORE building any heavy hypercube to learn:
      - the loaded period of a date field (`Min`/`Max`)
      - the cardinality of a key column
      - the range of a numeric measure

    Internally builds a measures-only hypercube with 3 expressions
    (`Count(DISTINCT)`, `Min`, `Max`) and no dimensions. Engine resolves
    these from the symbol table without scanning rows, so the call returns
    in seconds even on multi-billion-row tables — orders of magnitude
    faster than `get_app_field_statistics`.

    PREFER THIS OVER:
      - `get_app_field_statistics` (which adds slow Sum/Avg/Median/Mode/Stdev)
      - `get_app_field` (which materializes individual values — heavy on
        high-cardinality keys)

    Args:
        app_id: Application GUID. Required.
        field_name: Exact field name, no square brackets. Required.

    Returns:
        JSON `{ "field_name": ..., "unique_values": {text,numeric},
        "min_value": {text,numeric}, "max_value": {text,numeric} }`.
    """
    e = _check()
    if e:
        return e
    try:
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        return _ok(engine_api.get_field_range(app_handle, field_name))
    except Exception as ex:
        return _err(str(ex), app_id=app_id, field_name=field_name)


@mcp.tool()
@_timed
def engine_create_hypercube(
    app_id: str,
    dimensions: Optional[List[Dict[str, Any]]] = None,
    measures: Optional[List[Dict[str, Any]]] = None,
    max_rows: int = DEFAULT_HYPERCUBE_MAX_ROWS,
) -> str:
    """
    Build a Qlik Engine hypercube (grouped aggregation) and return its rows.
    This is the MAIN data-analysis tool — use it for anything shaped like a
    SQL `SELECT ... GROUP BY ... ORDER BY`.

    BEFORE CALLING:
      1. Call `get_app_details` first to learn the exact field names AND
         `distinct_values` of every field. Field names are case-sensitive
         and must match the data model. All examples below use generic
         placeholders `<DimA>`, `<DimB>`, `<MetricX>` — substitute with
         the REAL field names from `get_app_details`.
      2. ESTIMATE THE RESULT SIZE BEFORE SENDING THE REQUEST. Multiply
         the `distinct_values` of every dimension you plan to include.
         That product is the MAXIMUM possible row count — if it exceeds
         5000, you MUST either:
           (a) narrow the scope via set analysis inside the measures
               (the dimensions themselves can't be filtered this way —
               see RULE #2 below), OR
           (b) drop one of the dimensions, OR
           (c) switch to a top-N pattern (`max_rows=15` +
               `qSortByExpression` on the ranking), OR
           (d) split the problem into N separate hypercubes, each
               slicing by one value of a categorical dimension.
         Generic example: dims=[<DimA> (10 distinct), <DimB> (5000
         distinct)] → worst case 10*5000 = 50,000 rows → TOO BIG.
         Fix: drop <DimB> or filter it via
         `Sum({<[<DimB>]={'<val1>'}>}<MetricX>)` on the measure side,
         or run 10 separate cubes (one per value of <DimA>).
      3. Read `get_app_script` to understand how calendar / derived
         fields are built in the specific app — it matters for set
         analysis. NEVER assume field names from examples.
      4. Use `get_app_variables` to discover named set-analysis
         shortcuts already defined in the app (e.g. `$(<varName>)`).

    ────────────────────────────────────────────────────────────────────────
    RULE #1 — ALWAYS USE SET ANALYSIS, NEVER `If()` INSIDE A MEASURE
    ────────────────────────────────────────────────────────────────────────
    `If()` inside an aggregate is a per-row scan of the entire fact table.
    Set analysis `{<Field={values}>}` is an index lookup on the symbol
    table BEFORE aggregation. On a huge fact table that's the difference
    between minutes and milliseconds.

    BAD  (DO NOT USE):
        Sum(If(<DimYear>=2025, <MetricX>))
        Count(If(<DimPeriod>='<val1>' and <DimYear>=2025, <KeyField>))
        Sum(If([<DimCat>]='<val1>', <MetricX>))
    GOOD:
        Sum({<[<DimYear>]={2025}>}<MetricX>)
        Count({<[<DimPeriod>]={'<val1>'}, [<DimYear>]={2025}>}<KeyField>)
        Sum({<[<DimCat>]={'<val1>'}>}<MetricX>)

    SET ANALYSIS QUICK REFERENCE (substitute field names and values from
    the real app — do NOT copy these placeholders verbatim):
      • Numeric ($integer/$numeric) field: no quotes
          `{<[<DimNum>]={2025}>}`
      • Text field: single quotes
          `{<[<DimText>]={'<val1>','<val2>'}>}`
      • Wildcard search: double quotes
          `{<[<DimText>]={"*<substring>*"}>}`
      • Multiple values (OR): comma list
          `{<[<DimText>]={'<v1>','<v2>','<v3>'}>}`
      • Multiple fields (AND): comma in modifier
          `{<[<DimA>]={1},[<DimB>]={'<v>'}>}`
      • Reset a filter: empty value
          `{<[<Dim>]=>}` ignores any current selection on <Dim>
      • Ignore ALL current selections: prefix with `1`
          `{1<[<Dim>]={<v>}>}`
      • Range on numeric/date:
          `{<[<DimDate>]={">=$(=Num(MonthStart(Today())))"}>}`
      • Combine sets: union `+`, intersect `*`, exclude `-`
          `Sum({<[<Dim>]={<v1>}>+<[<Dim>]={<v2>}>}<MetricX>)`
      • P() = "values that have ever satisfied":
          `{<[<Key>]=P({<[<Flag>]={1}>}[<Key>])>}`
      • E() = "values that have NEVER satisfied":
          `{<[<Key>]=E({<[<Flag>]={1}>}[<Key>])>}`
      • Period-over-period ratio:
          `Sum({<[<DimYear>]={<Y2>}>}<MetricX>) /
           Sum({<[<DimYear>]={<Y1>}>}<MetricX>) - 1`

    ────────────────────────────────────────────────────────────────────────
    RULE #2 — NEVER PUT EXPRESSIONS IN `qFieldDefs` (DIMENSION FIELD)
    ────────────────────────────────────────────────────────────────────────
    A dimension's `field` parameter must be a PLAIN FIELD NAME from the
    data model. If you put an `=expression` there, Qlik evaluates it for
    EVERY ROW of the underlying table (not for distinct values, not
    cached) — and it won't use any indexes. On a huge fact table this
    is a guaranteed timeout.

    BAD  (DO NOT USE):
        {"field": "=Date(<DimDate>)"}            # per-row function call
        {"field": "=Year(<DimDate>)"}            # per-row function call
        {"field": "=Month(<DimDate>)"}           # per-row function call
        {"field": "=If(<MetricX>>0,'A','B')"}    # per-row If() call
    GOOD:
        {"field": "<DimDate>"}                   # use the raw field
        {"field": "<DimYear>"}                   # use a pre-built bucket
        {"field": "<DimMonth>"}                  # if one exists already

    HOW TO HANDLE A "MISSING" DERIVED FIELD:
      If the data model has only a raw date dimension but you need
      monthly buckets, DO NOT compute a derived value in the dimension.
      Instead:
        1. Check `get_app_details` first — there's often already a
           calendar field (month / year / quarter / week / day-of-week).
           Use it directly. Names vary per app — READ the field list.
        2. If genuinely missing, just request the raw date as a
           dimension and aggregate the resulting 30-365 rows yourself
           on the client side. That's way cheaper than a per-row
           expression.

    THE ONE EXCEPTION: `qSortByExpression` is fine — it evaluates the
    expression per GROUP after aggregation, not per row. That's where
    you put `Sum({<...>}<MetricX>)` for "sort top-N by metric".

    ────────────────────────────────────────────────────────────────────────
    PERFORMANCE TIPS
    ────────────────────────────────────────────────────────────────────────
      - High-cardinality dimensions (>1M distinct values) with
        `qSortByExpression` force the engine to fully materialize and
        sort the whole set. On large apps this can take minutes. Keep
        measures simple, prefer `Sum()` over `Aggr()`, and keep
        `max_rows` small (15-50) when sorting by expression.
      - `Aggr()` expressions over high-cardinality dimensions are
        especially expensive — they build an in-memory pivot per group.
        Avoid if you can.
      - Always narrow the period with set analysis before aggregating —
        a small period filter can cut a billion-row scan by orders of
        magnitude.
      - Raise the `QLIK_WS_TIMEOUT` environment variable if you get
        "WebSocket recv() timed out" on legitimately heavy computations.

    Args:
        app_id: Application GUID. Required.
        dimensions: List of GROUP BY columns. Each element is an object:
            ```
            {
              "field": "<DimFieldName>",          # real field name, no []
              "sort_by": {                        # OPTIONAL, default ASCII asc
                "qSortByNumeric": 0,              # -1 desc, 1 asc, 0 disabled
                "qSortByAscii": 1,                # -1 desc, 1 asc, 0 disabled
                "qSortByExpression": -1,          # -1 desc, 1 asc, 0 disabled
                "qExpression": "Sum({<[<DimYear>]={<Y>}>}<MetricX>)"
                                                   # required if qSortByExpression != 0
              }
            }
            ```
            Omit the whole list or pass `[]` for a grand-total row only.
            Use `qSortByExpression` for "top/bottom N by metric" queries.
        measures: List of aggregate expressions. Each element is an object:
            ```
            {
              "expression": "Sum({<[<DimYear>]={<Y>}>}<MetricX>)",
                                              # any valid Qlik expression
              "label": "<display label>",     # OPTIONAL display name
              "sort_by": { "qSortByNumeric": -1 }  # OPTIONAL, default desc
            }
            ```
            Set analysis (`{<field={value}>}`) is the ONLY correct way
            to filter inside a measure — NEVER put filters in dimensions.
            Use Qlik functions: Sum, Count, Avg, Min, Max, Only,
            FirstSortedValue, RangeSum, etc. Period-over-period:
            `"Sum({<[<DimYear>]={<Y2>}>}<MetricX>) /
              Sum({<[<DimYear>]={<Y1>}>}<MetricX>) - 1"`.
        max_rows: Maximum rows to return. Default 1000. HARD LIMIT: 5000.
            The server REJECTS requests with `max_rows > 5000` with a
            `limit_exceeded` error. The server also REJECTS requests
            whose `columns * max_rows > 9900` with a `cell_cap_exceeded`
            error (Qlik's own single-page limit is 10000 cells).

            DO NOT try to work around these limits by bumping max_rows.
            Instead, narrow the query with set analysis. Correct patterns
            (substitute the placeholders with real field names from
            `get_app_details`):

              TOP-N: max_rows=15, qSortByExpression=-1 on the ranking dim,
                qExpression="Sum({<[<DimPeriod>]={<val>}>}<MetricX>)".
              PERIOD FILTER: put the period inside every measure —
                Sum({<[<DimYear>]={<Y>},[<DimPeriod>]={'<v>'}>}<MetricX>).
              SLICE-BY-CATEGORY: run N small hypercubes, one per category
                value, each filtered via {<[<DimCat>]={'<v>'}>}. Prefer
                100 focused queries over 1 giant scan — they're faster
                AND don't timeout.
              ESTIMATE BEFORE CALLING: multiply `distinct_values` of your
                dimensions (from `get_app_details`). If the product
                exceeds 5000, the query is too broad — add more
                set-analysis filters or switch to top-N.

    Returns:
        JSON with:
          - `total_rows`: full result size on the server (could be huge)
          - `returned_rows`: how many rows are actually in the matrix
            below (<= `max_rows`, <= 5000 hard cap)
          - `hard_max_rows`: 5000 — the enforced upper bound
          - `truncation_warning`: non-null string if `total_rows >
            returned_rows`. Tells you the result was truncated and
            instructs how to narrow the query with set analysis. DO NOT
            ignore this — retry with a narrower set-analysis filter, or
            switch to a top-N pattern, or slice the query by category.
          - `total_columns`: number of dim+measure columns
          - `dimensions` / `measures`: echoed input
          - `hypercube_data.qDataPages[0].qMatrix`: the actual cells,
            each as `{qText, qNum, qElemNumber, qState}`. Read values
            from `qText` (display) or `qNum` (numeric). `"NaN"` means the
            cell is empty or contains text.

    ON ERROR: the response contains `error`, `error_category`, and
    `hint`. Relevant categories:
      - `limit_exceeded`: you asked for max_rows > 5000. Redesign the
        query.
      - `cell_cap_exceeded`: columns * max_rows > 9900. Either drop
        columns or reduce max_rows (the hint gives you the exact
        suggested max_rows).
      - `socket_timeout`: Qlik is actually computing something slow.
        Add more set-analysis filters, reduce max_rows, or switch to
        top-N with qSortByExpression.
      - `engine_api_error`: invalid expression / unknown field.
    """
    import traceback as _tb
    e = _check()
    if e:
        return e
    stage = "ensure_app"
    try:
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        stage = "create_hypercube"
        return _ok(engine_api.create_hypercube(app_handle, dimensions or [], measures or [], max_rows))
    except Exception as ex:
        logger.exception("engine_create_hypercube failed at stage=%s", stage)
        return _err(
            str(ex) or repr(ex),
            error_type=type(ex).__name__,
            failed_stage=stage,
            app_id=app_id,
            ws_operation_timeout=engine_api.ws_operation_timeout,
            traceback=_tb.format_exc(),
        )


@mcp.tool()
@_timed
def get_app_field(
    app_id: str,
    field_name: str,
    limit: int = DEFAULT_FIELD_LIMIT,
    offset: int = 0,
    search_string: Optional[str] = None,
    search_number: Optional[str] = None,
    case_sensitive: bool = False,
) -> str:
    """
    List distinct values of a single field (like `SELECT DISTINCT field FROM ... LIMIT N`),
    with optional wildcard filtering and pagination.

    Use this to see what values a field actually contains before writing set
    analysis (`{<Field={'value1','value2'}>}`). Particularly useful for
    dimension fields like status codes, categories, region names.

    For min/max/cardinality of a single field prefer the much faster
    `engine_get_field_range`. For grouped aggregations use
    `engine_create_hypercube`.

    IMPLEMENTATION NOTE: this tool first tries the lightweight ListObject
    API. If ListObject returns an empty result (which can happen for fields
    in fact tables that have no current "state"), it transparently falls
    back to a one-dimension hypercube. The response then includes
    `fallback_used: "hypercube"`. If both methods return nothing, the
    response includes a `warning` explaining why and suggesting next steps.

    Args:
        app_id: Application GUID. Required.
        field_name: Exact field name, no square brackets. Case-sensitive.
        limit: Max values to return per page. Default 10, cap 100.
        offset: Number of values to skip for pagination. Default 0.
        search_string: Optional wildcard filter applied to the text form of
            the value. Supports `*` and `%` as multi-character wildcards.
            Example: `"<prefix>*"` matches any value starting with `<prefix>`.
            Leave `None` to return all values.
        search_number: Optional wildcard filter on the numeric/text form.
            Matches values whose number OR text representation matches the
            pattern. Useful for filtering IDs by prefix.
        case_sensitive: If `False` (default) the wildcard match is
            case-insensitive; set to `True` for exact case matching.

    Returns:
        JSON `{ "field_values": ["val1", "val2", ...] }` — plain list after
        filtering and pagination. Order is by frequency descending on the
        Qlik side.
    """
    e = _check()
    if e:
        return e
    lim = min(max(limit or DEFAULT_FIELD_LIMIT, 1), MAX_FIELD_LIMIT)
    off = max(offset or 0, 0)
    try:
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        fetch_size = min(max(lim + off, DEFAULT_FIELD_FETCH_SIZE), MAX_FIELD_FETCH_SIZE)
        field_data = engine_api.get_field_values(app_handle, field_name, fetch_size, include_frequency=False)
        values = [v.get("value", "") for v in field_data.get("values", [])]
        if search_string:
            rx = _wildcard_to_regex(search_string, case_sensitive)
            values = [v for v in values if isinstance(v, str) and rx.match(v)]
        if search_number:
            rxn = _wildcard_to_regex(search_number, case_sensitive)
            filtered = []
            for vobj in field_data.get("values", []):
                qnum = vobj.get("numeric_value")
                cell_text = vobj.get("value", "")
                if qnum is not None and (rxn.match(str(qnum)) or rxn.match(str(cell_text))):
                    filtered.append(cell_text)
            values = filtered
        out: Dict[str, Any] = {"field_values": values[off:off + lim]}
        # Surface internal hints from get_field_values so the LLM knows
        # whether the result came from the fast ListObject path or from the
        # heavier hypercube fallback, and whether anything looked off.
        if isinstance(field_data, dict):
            if field_data.get("fallback_used"):
                out["fallback_used"] = field_data["fallback_used"]
            if field_data.get("warning"):
                out["warning"] = field_data["warning"]
        return _ok(out)
    except Exception as ex:
        return _err(str(ex))


@mcp.tool()
@_timed
def get_app_variables(
    app_id: str,
    limit: int = DEFAULT_FIELD_LIMIT,
    offset: int = 0,
    created_in_script: Optional[str] = None,
    search_string: Optional[str] = None,
    case_sensitive: bool = False,
) -> str:
    """
    List user-defined Qlik variables (`SET`/`LET` from script, or UI-created),
    split by source. System/reserved variables are always excluded.

    Use this to discover `$(vCurrentYear)`-style shortcuts used in chart
    expressions — expanding them manually gives you the real set analysis.

    Args:
        app_id: Application GUID. Required.
        limit: Max variables per page. Default 10, cap 100.
        offset: Number of variables to skip for pagination. Default 0.
        created_in_script: Filter by source. Accepts `"true"` / `"false"`
            (case-insensitive). `"true"` — only script-created (`SET`/`LET`);
            `"false"` — only UI-created; `None` (default) — only UI-created
            (for historical reasons). Pass `"true"` explicitly to inspect
            script variables.
        search_string: Optional wildcard filter on variable name OR its
            text value. Supports `*` and `%`. Leave `None` for no filter.
        case_sensitive: Toggle case-sensitive wildcard match. Default `False`.

    Returns:
        JSON `{ "variables_from_script": {name: value, ...}, "variables_from_ui":
        {name: value, ...} }`. Empty group becomes `""` instead of `{}`.
    """
    e = _check()
    if e:
        return e
    lim = min(max(limit or DEFAULT_FIELD_LIMIT, 1), MAX_FIELD_LIMIT)
    off = max(offset or 0, 0)
    script_flag = None
    if created_in_script is not None:
        script_flag = _to_bool(created_in_script, None)
    try:
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        var_list = engine_api._get_user_variables(app_handle) or []
        prepared = [{"name": v.get("name", ""), "text_value": v.get("text_value", "") or "", "is_script": v.get("is_script_created", False)} for v in var_list]
        if script_flag is True:
            prepared = [x for x in prepared if x["is_script"]]
        elif script_flag is False:
            prepared = [x for x in prepared if not x["is_script"]]
        else:
            prepared = [x for x in prepared if not x["is_script"]]
        if search_string:
            rx = _wildcard_to_regex(search_string, case_sensitive)
            prepared = [x for x in prepared if rx.match(x["name"]) or rx.match(x["text_value"])]
        from_script = {x["name"]: x["text_value"] for x in prepared[off:off+lim] if x["is_script"]}
        from_ui = {x["name"]: x["text_value"] for x in prepared[off:off+lim] if not x["is_script"]}
        return _ok({"variables_from_script": from_script or "", "variables_from_ui": from_ui or ""})
    except Exception as ex:
        return _err(str(ex))


@mcp.tool()
@_timed
def get_app_sheets(app_id: str) -> str:
    """
    List all sheets (tabs) in a Qlik Sense application.

    Use this to discover which sheets exist before drilling into their objects
    with `get_app_sheet_objects`. Sheets are the top-level pages users see in
    the Qlik dashboard UI.

    Args:
        app_id: Application GUID. Required.

    Returns:
        JSON `{ "app_id": ..., "total_sheets": N, "sheets": [{sheet_id, title,
        description}, ...] }`. Pass `sheet_id` into `get_app_sheet_objects` to
        list the charts/tables on that sheet.
    """
    e = _check()
    if e:
        return e
    try:
        # no_data=False to keep the cached connection data-ready for later calls
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        sheets = engine_api.get_sheets(app_handle)
        sheets_list = [
            {"sheet_id": s.get("qInfo", {}).get("qId", ""), "title": s.get("qMeta", {}).get("title", ""), "description": s.get("qMeta", {}).get("description", "")}
            for s in sheets
        ]
        return _ok({"app_id": app_id, "total_sheets": len(sheets_list), "sheets": sheets_list})
    except Exception as ex:
        return _err(str(ex))


@mcp.tool()
@_timed
def get_app_sheet_objects(app_id: str, sheet_id: str) -> str:
    """
    List all visualization objects (charts, tables, KPIs, filters, etc) placed
    on a specific sheet, with their IDs and types.

    Use this to discover the `object_id` of a specific chart the user mentions
    by title, then pass it to `get_app_object` to inspect the chart's full
    layout (dimensions, measures, expressions, current selections).

    Args:
        app_id: Application GUID. Required.
        sheet_id: Sheet ID from `get_app_sheets`. Required.

    Returns:
        JSON with `objects` array where each element has `object_id`,
        `object_type` (e.g. `"barchart"`, `"table"`, `"kpi"`, `"listbox"`)
        and `object_description` (title). Use `object_id` in `get_app_object`.
    """
    e = _check()
    if e:
        return e
    try:
        # no_data=False to keep the cached connection data-ready
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        objects = engine_api._get_sheet_objects_detailed(app_handle, sheet_id) or []
        formatted = [
            {"object_id": o.get("object_id", ""), "object_type": o.get("object_type", ""), "object_description": o.get("object_title", "")}
            for o in objects if isinstance(o, dict)
        ]
        return _ok({"app_id": app_id, "sheet_id": sheet_id, "total_objects": len(formatted), "objects": formatted})
    except Exception as ex:
        return _err(str(ex), app_id=app_id, sheet_id=sheet_id)


@mcp.tool()
@_timed
def get_app_object(app_id: str, object_id: str) -> str:
    """
    Fetch the full layout of a specific visualization object (chart, table, KPI,
    pivot table, etc.) by its object ID — equivalent to Engine API
    `GetObject` + `GetLayout`.

    This returns everything the Qlik client renders: the hypercube with
    current data, dimension/measure definitions and expressions, title,
    subtitle, colors, sort order, current selections applied to the chart.
    Use it to reverse-engineer how a dashboard chart is computed before
    rebuilding the same logic in `engine_create_hypercube`.

    Args:
        app_id: Application GUID. Required.
        object_id: Object ID from `get_app_sheet_objects`. Required.

    Returns:
        JSON with full `qLayout` of the object. Key fields depend on the
        object type — look for `qHyperCube.qDimensionInfo`, `qMeasureInfo`,
        `qDataPages[0].qMatrix` for charts/tables.
    """
    e = _check()
    if e:
        return e
    try:
        app_handle = engine_api.ensure_app(app_id, no_data=False)
        obj_result = engine_api.send_request("GetObject", {"qId": object_id}, handle=app_handle)
        if "qReturn" not in obj_result:
            return _err(f"Object {object_id} not found")
        obj_handle = obj_result["qReturn"]["qHandle"]
        layout_result = engine_api.send_request("GetLayout", [], handle=obj_handle)
        if "qLayout" not in layout_result:
            return _err("Failed to get object layout")
        return _ok(layout_result)
    except Exception as ex:
        return _err(str(ex), app_id=app_id, object_id=object_id)


# ═══════════════════════════════════════════════════════════════════════════
# TOOLS — Task management (QRS)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
@_timed
def get_tasks(
    status_filter: Optional[str] = None,
    name_filter: Optional[str] = None,
    app_filter: Optional[str] = None,
) -> str:
    """
    List Qlik Sense reload tasks with their last execution status.

    Use this as the entry point for anything reload/schedule-related. For
    "find all broken reloads" prefer `status_filter="failed"` — it's faster
    because it uses a QRS query filter instead of fetching everything and
    client-side filtering.

    Args:
        status_filter: Last-execution status. Accepts:
            - `"failed"` — tasks whose last run errored out
            - `"success"` — tasks that last finished cleanly (status code 7)
            - `"running"` — currently executing (best-effort, can be stale)
            - `"all"` or `None` (default) — everything
        name_filter: Wildcard filter on task name. Supports `*` and `%` as
            multi-char wildcards. Case-insensitive. Example: `"Daily*"`.
        app_filter: Wildcard filter on the target app name (the one this task
            reloads). Same syntax as `name_filter`.

    Returns:
        JSON `{ "tasks": [...], "count": N }`. Each task has id, name,
        app_name, enabled, last_execution_result (status, start_time,
        stop_time, details).
    """
    e = _check()
    if e:
        return e
    if status_filter == "failed":
        tasks = repo_api.get_failed_tasks()
    else:
        tasks = repo_api.get_task_operational_status()
        if status_filter == "success":
            tasks = [t for t in tasks if t.get("last_execution_result", {}).get("status") == 7]
    if name_filter:
        rx = _wildcard_to_regex(name_filter, False)
        tasks = [t for t in tasks if rx.match(t.get("name", ""))]
    if app_filter:
        rx = _wildcard_to_regex(app_filter, False)
        tasks = [t for t in tasks if rx.match(t.get("app_name", ""))]
    return _ok({"tasks": tasks, "count": len(tasks)})


@mcp.tool()
@_timed
def get_task_details(task_id: str) -> str:
    """
    Fetch full QRS reload-task object by task ID — all properties, not just
    the summary from `get_tasks`.

    Args:
        task_id: Task GUID from `get_tasks`.

    Returns:
        Raw QRS JSON for `/qrs/reloadtask/{id}` — includes enabled, maxRetries,
        taskSessionTimeout, preloadNodes, app reference, tags, privileges, etc.
    """
    e = _check()
    if e:
        return e
    return _ok(repo_api.get_reload_task_by_id(task_id))


@mcp.tool()
@_timed
def start_task(task_id: str) -> str:
    """
    Trigger a reload task to start immediately.

    This is a write operation that affects the live Qlik Sense server — only
    call it when the user has explicitly asked to start/retry a reload.
    Returns right after the task is queued; use `get_task_executions` to
    track progress afterwards.

    Args:
        task_id: Task GUID to start. Required.

    Returns:
        QRS response to `/qrs/task/{id}/start`.
    """
    e = _check()
    if e:
        return e
    return _ok(repo_api.start_task(task_id))


@mcp.tool()
@_timed
def create_task(app_id: str, task_name: str, enabled: bool = True) -> str:
    """
    Create a new reload task for a Qlik application.

    Write operation — only call with explicit user intent. The created task
    has NO schedule by default; attach one separately via
    `create_task_schedule`.

    Args:
        app_id: Application GUID the task will reload. Required.
        task_name: Human-readable task name (must be unique in QRS). Required.
        enabled: Whether the task is enabled after creation. Default `True`.

    Returns:
        Created QRS task object, including the new task `id`.
    """
    e = _check()
    if e:
        return e
    return _ok(repo_api.create_reload_task(app_id, task_name, enabled))


@mcp.tool()
@_timed
def update_task(task_id: str, name: Optional[str] = None, enabled: Optional[bool] = None) -> str:
    """
    Update properties of an existing reload task. Write operation.

    Args:
        task_id: Task GUID to update. Required.
        name: New task name. Pass `None` to keep the current one.
        enabled: New enabled state. Pass `None` to keep the current one.

    Returns:
        Updated QRS task object.
    """
    e = _check()
    if e:
        return e
    updates: Dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if enabled is not None:
        updates["enabled"] = enabled
    return _ok(repo_api.update_reload_task(task_id, updates))


@mcp.tool()
@_timed
def delete_task(task_id: str) -> str:
    """
    Permanently delete a reload task. DESTRUCTIVE write operation — only call
    after explicit user confirmation. There is no undo.

    Args:
        task_id: Task GUID to delete. Required.

    Returns:
        QRS delete response.
    """
    e = _check()
    if e:
        return e
    return _ok(repo_api.delete_reload_task(task_id))


@mcp.tool()
@_timed
def get_task_schedule(task_id: str) -> str:
    """
    List schedule triggers (cron-like time triggers) attached to a reload task.

    Args:
        task_id: Task GUID. Required.

    Returns:
        JSON `{ "task_id": ..., "triggers": [...], "count": N }`. Each trigger
        describes its repetition rule (daily/hourly/etc.), start time, time
        zone, and enabled state.
    """
    e = _check()
    if e:
        return e
    triggers = repo_api.get_schema_triggers(task_id)
    return _ok({"task_id": task_id, "triggers": triggers, "count": len(triggers)})


@mcp.tool()
@_timed
def create_task_schedule(
    task_id: str,
    name: str,
    repeat: str = "daily",
    interval_minutes: int = 1440,
    start_date: str = "2026-04-01T00:00:00.000Z",
    time_zone: str = "Europe/Moscow",
    enabled: bool = True,
) -> str:
    """
    Attach a new schedule trigger to a reload task. Write operation.

    Args:
        task_id: Task GUID to attach the schedule to. Required.
        name: Display name of the schedule trigger. Required.
        repeat: Repetition rule. One of:
            `"once"`, `"minutely"`, `"hourly"`, `"daily"` (default),
            `"weekly"`, `"monthly"`. Case-insensitive.
        interval_minutes: Interval between runs in minutes. Default 1440
            (once a day). Only meaningful for repeating schedules; ignored
            for `"once"`.
        start_date: First-run timestamp in ISO-8601 UTC format
            (`"YYYY-MM-DDThh:mm:ss.sssZ"`). Default is hardcoded — always
            override this to a future time the user wants.
        time_zone: IANA time-zone name. Default `"Europe/Moscow"`.
        enabled: Whether the schedule is active immediately. Default `True`.

    Returns:
        QRS response with the created schema event/trigger object.
    """
    e = _check()
    if e:
        return e
    repeat_map = {"once": 0, "minutely": 1, "hourly": 2, "daily": 3, "weekly": 4, "monthly": 5}
    repeat_opt = repeat_map.get(repeat.lower(), 3)
    return _ok(repo_api.create_schema_trigger(task_id, name, time_zone, start_date, repeat_opt, interval_minutes, enabled))


@mcp.tool()
@_timed
def get_task_executions(task_id: str, top: int = 10) -> str:
    """
    Get execution history (results) for a reload task, newest first.

    Use this after `start_task` to verify the reload worked, or to show a
    reliability timeline for a flaky task.

    Args:
        task_id: Task GUID. Required.
        top: How many most-recent executions to return. Default 10.

    Returns:
        JSON `{ "task_id": ..., "executions": [...], "count": N }`. Each
        entry: status, start_time, stop_time, duration, details (script log
        tail on failure), execution_host.
    """
    e = _check()
    if e:
        return e
    results = repo_api.get_execution_results(task_id, top)
    return _ok({"task_id": task_id, "executions": results, "count": len(results)})


@mcp.tool()
@_timed
def get_task_script_log(task_id: str) -> str:
    """
    Download the full script log (stdout of the last reload run) for a task.

    Use this to diagnose a reload failure — search the log for "Error",
    "not found", timestamps around the failure, etc. Can be LARGE (several MB
    for long scripts); prefer `get_failed_tasks_with_logs` if you only need
    the tail of failures.

    Args:
        task_id: Task GUID. Required.

    Returns:
        Raw log text (not JSON).
    """
    e = _check()
    if e:
        return e
    log_text = repo_api.get_script_log_by_task_id(task_id)
    return log_text


@mcp.tool()
@_timed
def get_failed_tasks_with_logs() -> str:
    """
    Get all currently-failed reload tasks together with the last ~50 lines of
    their script logs — in a single call, no parameters.

    This is the fastest way to answer "what's broken on the Qlik server right
    now" / "show me today's reload failures". Prefer this over combining
    `get_tasks(status_filter="failed")` + `get_task_script_log` for every
    task.

    Returns:
        JSON `{ "failed_tasks": [{task_id, task_name, app_name, last_start,
        last_stop, details, log_tail}, ...], "count": N }`.
    """
    e = _check()
    if e:
        return e
    failed = repo_api.get_failed_tasks()
    results = []
    for t in failed:
        tid = t.get("id", "")
        log_text = repo_api.get_script_log_by_task_id(tid) if tid else "No task ID"
        # Extract last ~50 lines of log to find error
        log_lines = log_text.strip().split("\n") if log_text else []
        tail = log_lines[-50:] if len(log_lines) > 50 else log_lines
        results.append({
            "task_id": tid,
            "task_name": t.get("name", ""),
            "app_name": t.get("app_name", ""),
            "last_start": t.get("last_execution_result", {}).get("start_time", ""),
            "last_stop": t.get("last_execution_result", {}).get("stop_time", ""),
            "details": t.get("last_execution_result", {}).get("details", ""),
            "log_tail": "\n".join(tail),
        })
    return _ok({"failed_tasks": results, "count": len(results)})


@mcp.tool()
@_timed
def get_task_dependencies(task_id: str, direction: str = "downstream") -> str:
    """
    Resolve the full transitive dependency chain of a reload task, following
    composite events (one task's successful finish triggering another).

    Use `direction="downstream"` to answer "what else will run after this
    task succeeds / what is the blast radius". Use `direction="upstream"` to
    answer "what must have finished before this task can start".

    The result is flattened (not a tree) — each entry carries a `depth`
    field so you can reconstruct hierarchy if needed. Cycles are broken
    using a visited set.

    Args:
        task_id: Root task GUID. Required.
        direction: `"downstream"` (default) — tasks this one triggers;
            `"upstream"` — tasks that trigger this one.

    Returns:
        JSON `{ "root_task_id": ..., "direction": ..., "dependencies":
        [{id, name, depth}, ...], "count": N }`.
    """
    e = _check()
    if e:
        return e

    all_events = repo_api.get_all_composite_events()

    # Build lookup: trigger_task_id -> list of dependent tasks
    # and reverse: dependent_task_id -> list of trigger tasks
    downstream_map: Dict[str, List[Dict[str, Any]]] = {}
    upstream_map: Dict[str, List[Dict[str, Any]]] = {}

    for evt in all_events:
        dependent_task = evt.get("reloadTask") or evt.get("externalProgramTask")
        if not dependent_task:
            continue
        dep_id = dependent_task.get("id", "")
        dep_name = dependent_task.get("name", "")

        rules = evt.get("compositeRules", [])
        for rule in rules:
            trigger_task = rule.get("reloadTask") or rule.get("externalProgramTask")
            if not trigger_task:
                continue
            trig_id = trigger_task.get("id", "")
            trig_name = trigger_task.get("name", "")

            downstream_map.setdefault(trig_id, []).append({"id": dep_id, "name": dep_name})
            upstream_map.setdefault(dep_id, []).append({"id": trig_id, "name": trig_name})

    visited = set()
    result = []

    def walk(tid: str, depth: int):
        if tid in visited:
            return
        visited.add(tid)
        lookup = downstream_map if direction == "downstream" else upstream_map
        for child in lookup.get(tid, []):
            result.append({"id": child["id"], "name": child["name"], "depth": depth})
            walk(child["id"], depth + 1)

    walk(task_id, 1)
    return _ok({"root_task_id": task_id, "direction": direction, "dependencies": result, "count": len(result)})


# ═══════════════════════════════════════════════════════════════════════════
# Entry points
# ═══════════════════════════════════════════════════════════════════════════


async def async_main():
    """Async main entry point — runs streamable HTTP transport."""
    logger.info("Starting qlik-sense-mcp-server v%s (streamable-http on :8000/mcp)", __version__)
    await mcp.run_streamable_http_async()


def main():
    """CLI entry point."""
    if len(sys.argv) > 1:
        if sys.argv[1] in ("--help", "-h"):
            _print_help()
            return
        if sys.argv[1] in ("--version", "-v"):
            sys.stderr.write(f"qlik-sense-mcp-server {__version__}\n")
            return
        if sys.argv[1] == "--stdio":
            asyncio.run(_run_stdio())
            return
    asyncio.run(async_main())


async def _run_stdio():
    """Run in stdio mode for backward-compat."""
    logger.info("Starting qlik-sense-mcp-server v%s (stdio)", __version__)
    await mcp.run_stdio_async()


def _print_help():
    sys.stderr.write(f"""
Qlik Sense MCP Server v{__version__} — Model Context Protocol server for Qlik Sense Enterprise APIs

USAGE:
    qlik-sense-mcp-server              Start with HTTP streaming on :8000/mcp
    qlik-sense-mcp-server --stdio      Start with stdio transport (legacy)
    qlik-sense-mcp-server --help       Show this help
    qlik-sense-mcp-server --version    Show version

TOOLS ({len(mcp._tool_manager._tools)} total):
    Repository: get_about, get_apps, get_app_details
    Engine:     get_app_script, get_app_field_statistics, engine_create_hypercube,
                get_app_field, get_app_variables, get_app_sheets,
                get_app_sheet_objects, get_app_object
    Tasks:      get_tasks, get_task_details, start_task, create_task, update_task,
                delete_task, get_task_schedule, create_task_schedule,
                get_task_executions, get_task_script_log, get_failed_tasks_with_logs

GitHub: https://github.com/bintocher/qlik-sense-mcp
""")


if __name__ == "__main__":
    main()
