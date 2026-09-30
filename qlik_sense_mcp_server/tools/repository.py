"""Tools backed by the Repository API: what apps exist and what is in them.

`get_app_details` also reaches into the Engine for the data model, because
"what is in this app" is the one question that spans both APIs.
"""

from typing import Any, Dict, List, Optional, Tuple

from . import context, overview, usage
from .helpers import (
    _check,
    _tool,
    _err,
    _engine_serialised,
    _ok,
    _timed,
    _to_tribool,
)
from ..config import (
    DEFAULT_APPS_LIMIT,
    MAX_APPS_LIMIT,
)

logger = context.logger

# Volume that deserves a warning: a table a query should filter, and a
# field a query should not group by without a limit.
BIG_TABLE_ROWS = 100_000_000
HIGH_CARD_FIELD = 1_000_000

# The finished `get_app_details` payload per app, keyed by the app's last
# reload. Everything in it — tables, fields, values, master items, the
# usage of published sheets — changes only when the app reloads.
_DETAILS_CACHE: Dict[str, Dict[str, Any]] = {}


def forget_app_details(app_id: str = None) -> None:
    """Drop the cached model for one app, or for all of them."""
    if app_id is None:
        _DETAILS_CACHE.clear()
    else:
        _DETAILS_CACHE.pop(app_id, None)


def _tags_text(tags: Any) -> str:
    """Qlik's field tags as one plain string: `"numeric integer"` for
    `["$numeric", "$integer"]` - the `$` carries no meaning of its own."""
    if isinstance(tags, str):
        return tags
    return " ".join(str(tag).lstrip("$") for tag in (tags or []))


def _is_temporal(field: Dict[str, Any]) -> bool:
    """Does this field hold a date or a timestamp?"""
    return overview.is_temporal(field.get("tags"))


def _values_and_ranges(app_handle: int, raw_fields: List[Dict[str, Any]]
                       ) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, List[str]]]]:
    """Values of every field with few of them, and the ends of every date.

    Both are list objects read in pipelined batches, whatever the number
    of fields; neither computes anything over the data. Best effort: a
    reply without them is still a correct reply.
    """
    distinct: Dict[str, int] = {}
    temporal: Dict[str, bool] = {}
    for f in raw_fields:
        name = f.get("field_name")
        if not name:
            continue
        distinct[name] = max(distinct.get(name, 0), int(f.get("distinct_values") or 0))
        temporal[name] = temporal.get(name, False) or _is_temporal(f)
    short = [(name, count) for name, count in distinct.items()
             if 0 < count <= overview.LIST_VALUES_MAX]
    dates = [name for name, count in distinct.items()
             if temporal[name] and count > overview.LIST_VALUES_MAX]
    values: Dict[str, List[str]] = {}
    ranges: Dict[str, Dict[str, List[str]]] = {}
    try:
        if short:
            values = context.engine_api.get_field_values_batch(
                app_handle, [(name, overview.LIST_VALUES_MAX) for name, _count in short]) or {}
        if dates:
            ranges = context.engine_api.get_field_edges_batch(app_handle, dates, count=1) or {}
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            raise
        logger.debug("Could not read field values: %s", exc)
    return values, ranges


@_tool()
@_timed
def get_about() -> str:
    """
    Which Qlik Sense this is: version, build and node type.

    WHEN TO USE
        To check that the connection works, or when a behaviour depends on
        the release.

    WHEN NOT TO USE
        As a first step before other calls — nothing here is needed to
        list apps or read a data model.

    Returns:
        `buildVersion`, `buildDate`, `databaseProvider`, `nodeType`,
        `sharedPersistence`, `requiresBootstrap`.

    Does not return:
        Anything about apps, data or the current user.
    """
    e = _check()
    if e:
        return e
    return _ok(context.repo_api.get_about())



@_tool()
@_timed
def get_apps(
    limit: int = DEFAULT_APPS_LIMIT,
    offset: int = 0,
    name: Optional[str] = None,
    stream: Optional[str] = None,
    published: str = "true",
) -> str:
    """
    Find apps by name or stream.

    WHEN TO USE
        When the app is known by a fragment of its name, or when the task
        is to survey what exists on the server.

    WHEN NOT TO USE
        When the name is known in full — `get_app_details(name=...)` looks
        it up and reads the data model in the same call.
        When the GUID is known — go straight to `get_app_details`.

    Args:
        limit: apps per page. Default 25, cap 50; page with `offset`
            rather than raising it.
        offset: apps to skip.
        name: substring of the app name, case insensitive. No wildcards
            needed: "Rev" matches "Revenue 2025".
        stream: substring of the publication stream name.
        published: `"true"` (default) for published apps, `"false"` for
            drafts in a personal space, `"both"` for either.

    Returns:
        `apps`, each with `guid`, `name`, `description`, `stream`,
        `modified_dttm`, `reload_dttm`; and `pagination` carrying
        `total_found` — the real total on the server, not the page size —
        plus `has_more` and `next_offset`.

    Does not return:
        Tables, fields or any data. Paging happens in Qlik, so nothing
        past the record limit goes missing.
    """
    e = _check()
    if e:
        return e
    lim = min(max(limit or DEFAULT_APPS_LIMIT, 1), MAX_APPS_LIMIT)
    if offset is not None and (isinstance(offset, bool)
                               or not isinstance(offset, int) or offset < 0):
        return _err(
            f"offset={offset!r} is not a row number.",
            error_category="invalid_argument",
            hint="Pass 0 or a positive integer, or omit it.",
        )
    off = offset or 0
    # An unrecognised value used to answer exactly like the correct one:
    # `published="all"` fell through to "no filter", which is what "both"
    # means, so a typo looked like it worked.
    if published is not None and str(published).strip().lower() not in (
            "true", "1", "yes", "y", "false", "0", "no", "n", "both"):
        return _err(
            f"published={published!r} is not one of the values this filter "
            f"takes.",
            error_category="invalid_argument",
            allowed_values=["true", "false", "both"],
        )
    return _ok(context.repo_api.get_comprehensive_apps(lim, off, name, stream,
                                               _to_tribool(published)))



@_tool()
@_timed
@_engine_serialised
def get_app_details(app_id: Optional[str] = None, name: Optional[str] = None) -> str:
    """
    The app's data model: its tables, its fields, and what the values look
    like.

    WHEN TO USE
        First, before asking anything about the data. Field names are
        case-sensitive and have to match exactly, and this is where they
        come from. It also opens the app, so every later call against the
        same `app_id` reuses the open connection.
        Give it `name` when that is all you have — no separate lookup is
        needed.

    WHEN NOT TO USE
        Repeatedly for the same app: the answer changes only when the app
        reloads, and a repeat call is served from cache with
        `from_cache: true`.

    Args:
        app_id: application GUID. Preferred, since it is unambiguous.
        name: app name, case insensitive. An exact match wins over a
            partial one. At least one of the two is required.

    Returns:
        `app` - app_id, name, description, stream, reload_dttm.
        `tables` - every linked table with its `role` (fact, dimension
        table, calendar - decided from the model's shape, not from names),
        `rows`, the script's `COMMENT TABLE` text where set, and `fields`:
        `name`, `distinct` values, `type` (date, timestamp, number, text),
        `key` for a field that links tables, `hidden`, `comment` (the
        script's `COMMENT FIELD` text, the only description a column
        carries), `values` when there are 25 or fewer, and `min`/`max` on a
        date field. A key field is listed once, under the table nearest the
        facts. A calendar carries `calendar`: its rows against its distinct
        dates - fewer dates than rows means one date stands for several
        rows (as-of, cumulative or shifted periods), and the fields with
        few values next to the date say by what.
        `links` - `Facts -Key-> Table`, from the facts outward; `A -Key- B`
        closes a loop.
        `islands` - tables linked to nothing, one line each with their
        fields and values: selectors used by expressions, not data to join.
        `dimensions` - master dimensions as `name = field` lines
        (`A > B (drill-down)`, `A | B (cyclic)`, `=expr (calculated)`).
        `measures` - master measures: `name`, `description`, `fields` they
        read, `expression` cut at 150 characters with `expression_truncated`
        and `id` when cut (`get_app_object` with that id returns it whole),
        and `variants` - measures named "<name> Y-1", "<name> YoY, %",
        "<name>, %" folded under their base, by exact name. `engine_query`
        computes any of them by name: {"measures": [{"master": "<name>"}]}.
        `filter_limits` says what keeps a filter from reaching a measure:
        `fields` it sets itself (its own condition wins over a filter on
        them), `own_set` - identifiers of its sets that ignore a filter
        written in front of them (`1`, `$`, a bookmark), `computed_first` -
        it holds `$(=...)`, computed before any filter.
        `usage` - how the author's sheets use the model (the approved
        sheets of a published app, every sheet of an unpublished one; read
        from definitions, nothing calculated): per field, in how many
        objects and sheets, in which role - `dimension`, `aggregated`,
        `condition` (named in a set modifier), `Aggr dimension` - and next
        to which fields and measures; `fields_in_one_object`,
        `unused_fields`; `master_measures` by use; `objects` - titled
        visualizations, the title first, as the question they answer, with
        their dimensions and measures; `formulas` - measure expressions
        written in objects rather than the library, deduplicated, with the
        sheet and how many objects use them; `alternate_state_or_set` -
        objects whose figures come from another state or a whole-cube set.
        `report_filters` - how a user filters this report: filter-pane
        fields by the number of sheets they are on, date pickers, variable
        inputs with their values, what buttons select, set or apply, and
        tab conditions with the meaning of the variables in them.
        `bookmarks` - the approved bookmarks shipped with the app, title
        and description. Bookmarks users saved are not listed.
        `alternate_states` where the app defines them.
        `warnings` - volume only: tables that need a filter, fields too
        wide to group by without a limit.
        `size_budget` - the estimated size in tokens against its limit,
        and `trimmed` naming what was shortened to stay under it.

    Does not return:
        Data, aggregates, or the load script — `get_app_script` has that.
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

    reload_stamp = resolved.get("reload_dttm", "")
    cached = _DETAILS_CACHE.get(aid)
    if cached and cached["reload_stamp"] == reload_stamp:
        # Everything below — the model read, the sample values, the edges —
        # depends only on the app and its last reload. Answer from the
        # cache and say so, rather than paying for it on every question
        # about the same app.
        reply = dict(cached["result"])
        reply["from_cache"] = True
        return _ok(reply)

    # Get tables and fields via Engine API (WebSocket). The model is cached
    # per app and dropped when the app reloads — `reload_dttm` is exactly
    # the stamp that moves when it does.
    try:
        app_handle = context.engine_api.ensure_app(aid, no_data=False)
        # The cache is an optimisation, not part of the client contract:
        # anything that can read the model is enough.
        read_model = getattr(context.engine_api, "cached_fields", None)
        if read_model is not None:
            # `resolved` is flat — `metainfo` is assembled further down, so
            # reading it here always yielded None and the cache never
            # noticed a reload. `reload_stamp` above reads the same field
            # correctly; use it.
            fields_data = read_model(app_handle, aid, reload_stamp)
        else:
            fields_data = context.engine_api.get_fields(app_handle)
    except Exception as ex:
        fields_data = {"error": str(ex)}

    raw_fields: List[Dict[str, Any]] = []
    if isinstance(fields_data, dict) and "fields" in fields_data:
        raw_fields = [f for f in fields_data["fields"] if not f.get("is_system")]

    # What the values look like, so filters are written against what Qlik
    # holds: `Moskva` is not `Moscow`, and Qlik answers the wrong one with
    # zeros rather than an error. Every field with few values lists them -
    # on a 97-field app, leaving out 48 of them cost 34 extra calls - and a
    # date field shows its first and last value, the period actually loaded.
    values, ranges = _values_and_ranges(app_handle, raw_fields) if raw_fields else ({}, {})
    model = overview.model_sections(raw_fields, values, ranges)
    model_fields = {f.get("field_name") for f in raw_fields}

    # Volume is the one thing worth a warning: a query over a billion-row
    # table without a filter, or a group-by on a field with millions of
    # values, is what runs into the time limit.
    warnings: List[str] = []
    big_tables = [t for t in model["tables"] if t.get("rows", 0) >= BIG_TABLE_ROWS]
    if big_tables:
        largest = max(t["rows"] for t in big_tables)
        warnings.append(
            f"{len(big_tables)} table(s) hold a lot of rows, the largest about "
            f"{largest:,}. Give every query a filter — a period, a category, a "
            f"key — so it reads part of the table rather than all of it. "
            f"`filters` in engine_query is the shortest way to say so.")
    wide = {f["field_name"]: int(f.get("distinct_values") or 0) for f in raw_fields
            if int(f.get("distinct_values") or 0) >= HIGH_CARD_FIELD}
    if wide:
        warnings.append(
            f"{len(wide)} field(s) hold many different values, the widest about "
            f"{max(wide.values()):,}. Grouping by one of these produces as many "
            f"rows; rank with `sort_by` and a small `limit` instead of returning "
            f"every group.")

    # The app's own vocabulary: what the author calls revenue and how it is
    # computed. A caller assembling the aggregation itself may pick a
    # neighbouring field and answer with a number nobody here recognises.
    library = {"measures": [], "dimensions": []}
    try:
        read_library = getattr(context.engine_api, "get_master_items", None)
        if read_library is not None and raw_fields:
            library = read_library(app_handle)
    except Exception as ex:
        if isinstance(ex, TimeoutError):
            raise
        logger.debug("Could not read the library of %s: %s", aid, ex)

    # Bookmarks the author approved: the named selections that are part of
    # the app. Users' own bookmarks are left out entirely - on one app 698
    # of them, half named by a GUID, were a third of the reply.
    bookmarks: List[str] = []
    bookmark_titles: Dict[str, str] = {}
    states: List[str] = []
    try:
        read_bookmarks = getattr(context.engine_api, "approved_bookmarks", None)
        if read_bookmarks is not None and raw_fields:
            approved = read_bookmarks(app_handle)
            bookmarks = [b["title"] + (f" - {b['description']}" if b.get("description") else "")
                         for b in approved]
            bookmark_titles = {b["id"]: b["title"] for b in approved if b.get("id")}
        read_sets = getattr(context.engine_api, "_known_sets", None)
        if read_sets is not None and raw_fields:
            states = sorted((read_sets(app_handle) or {}).get("states") or ())
    except Exception as ex:
        if isinstance(ex, TimeoutError):
            raise
        logger.debug("Could not read the bookmarks of %s: %s", aid, ex)

    # How the author's own sheets use the model - the reference answers of
    # this app. Read once per reload with everything else here, from
    # definitions only; a failure leaves the rest of the reply intact.
    usage_section: Dict[str, Any] = {}
    report_filters: List[str] = []
    try:
        read_sheets = getattr(context.engine_api, "published_sheets", None)
        if read_sheets is not None and raw_fields:
            sheets, basis = read_sheets(app_handle)
            if sheets:
                usage_section, report_filters = usage.summarise(
                    sheets, {f["field_name"] for f in raw_fields}, bookmark_titles)
                usage_section = {"read_from": basis, **usage_section}
    except Exception as ex:
        if isinstance(ex, TimeoutError):
            raise
        logger.warning("Could not summarise the sheets of %s: %s", aid, ex)

    result: Dict[str, Any] = {
        "app": {
            "app_id": aid,
            "name": resolved.get("name", ""),
            "description": resolved.get("description", ""),
            "stream": resolved.get("stream", ""),
            "reload_dttm": resolved.get("reload_dttm", ""),
        },
        "warnings": warnings,
        "tables": model["tables"],
        "links": model["links"],
    }
    if model["islands"]:
        result["islands"] = model["islands"]
    dimensions = overview.dimension_lines(library.get("dimensions") or [])
    if dimensions:
        result["dimensions"] = dimensions
    measures = overview.measure_entries(library.get("measures") or [], model_fields)
    if measures:
        result["measures"] = measures
    if usage_section:
        result["usage"] = usage_section
    if report_filters:
        result["report_filters"] = report_filters
    if bookmarks:
        result["bookmarks"] = bookmarks
    if states:
        result["alternate_states"] = states

    result = overview.fit_budget(result, [
        (f"values of short fields cut to the first {overview.TRIMMED_VALUES}",
         overview.trim_values),
        (f"measure expressions cut to {overview.TRIMMED_EXPRESSION_CHARS} characters",
         overview.trim_measures),
        ("usage cut to the 10 most used fields, 30 objects and 20 formulas",
         usage.trim_usage),
    ])
    if isinstance(fields_data, dict) and "error" in fields_data:
        result["engine_error"] = fields_data["error"]
    else:
        # Only cache a complete answer. A reply that lost its data model to
        # a refused session must not become the cached truth.
        _DETAILS_CACHE[aid] = {"reload_stamp": reload_stamp, "result": result}
    return _ok(result)


