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

logger = context.logger

# A field with few enough distinct values to list them outright. Above this
# the list stops being an aid and starts being a wall of text.
SAMPLE_VALUES_MAX_CARDINALITY = 25
# How many fields to sample in one reply. Sampling is one pipelined batch,
# but the values still cost context.
SAMPLE_VALUES_MAX_FIELDS = 20
# For fields too wide to list, show the ends instead: five smallest and
# five largest values, sorted by Qlik so text sorts as text.
EDGE_VALUES_COUNT = 5
EDGE_VALUES_MAX_FIELDS = 12

# Past this many fields the reply switches from a list of objects to a
# header plus rows. Measured: as objects, 33 fields cost 7.6k characters and
# the repeated key names are most of it. Below the threshold the readable
# form is worth its size; a 300-field warehouse model is not.
WIDE_MODEL_FIELDS = 60
WIDE_MODEL_COLUMNS = ["name", "table", "is_key", "distinct_values", "rows",
                      "tags", "comment"]

# The finished `get_app_details` payload per app, keyed by the app's last
# reload. Everything in it — tables, fields, sample values, edges — changes
# only when the app reloads.
_DETAILS_CACHE: Dict[str, Dict[str, Any]] = {}


def forget_app_details(app_id: str = None) -> None:
    """Drop the cached model for one app, or for all of them."""
    if app_id is None:
        _DETAILS_CACHE.clear()
    else:
        _DETAILS_CACHE.pop(app_id, None)


def _tags_text(tags: Any) -> str:
    """Qlik's field tags as one plain string.

    Qlik hands them out as `["$numeric", "$integer"]`. The `$` is on every
    tag and carries no meaning of its own, and the brackets and quotes cost
    more than the words. `"numeric integer"` says the same thing.
    """
    if isinstance(tags, str):
        return tags
    return " ".join(str(tag).lstrip("$") for tag in (tags or []))


def _is_temporal(field: Dict[str, Any]) -> bool:
    """Does this field hold a date or a timestamp?

    Tags reach here as one string (`"numeric timestamp"`), so membership is
    a word check, not a list lookup — and both spellings, with and without
    Qlik's leading `$`, are accepted so the answer does not depend on where
    the field came from.
    """
    tags = field.get("tags") or ""
    words = tags.split() if isinstance(tags, str) else [str(t) for t in tags]
    return any(w.lstrip("$") in ("date", "timestamp") for w in words)


def _attach_sample_values(app_handle: int, fields: List[Dict[str, Any]]) -> None:
    """Add `values` to low-cardinality fields, and `sample` to date fields.

    Filtering is written against the values Qlik holds, not against the
    ones a caller assumes — and the two differ constantly: `Moskva` for
    Moscow, `01.01.2024` for a date that a model will otherwise filter as
    a serial number. Both mistakes return zeros rather than an error, so
    the cheapest fix is to put the real values in front of the caller
    before any expression is written.
    """
    candidates = [
        f for f in fields
        if 0 < (f.get("distinct_values") or 0) <= SAMPLE_VALUES_MAX_CARDINALITY
    ]
    # Dates are the other guessing trap, and they are never low-cardinality;
    # a couple of values are enough to show the display format.
    date_fields = [f for f in fields if f not in candidates and _is_temporal(f)]
    wanted = (candidates + date_fields)[:SAMPLE_VALUES_MAX_FIELDS]
    results = {}
    if wanted:
        try:
            results = context.engine_api.get_field_values_batch(
                app_handle,
                [(f["name"], SAMPLE_VALUES_MAX_CARDINALITY if f in candidates else 3)
                 for f in wanted],
            )
        except Exception as exc:
            # An aid, not the answer: if it cannot be produced, the reply is
            # still correct without it.
            logger.debug("Could not sample field values: %s", exc)
            results = {}

    for field in wanted:
        values = results.get(field["name"])
        if not values:
            continue
        if field in candidates:
            field["values"] = values
        else:
            field["sample"] = values[:3]

    # Fields with too many values to list still need to show their shape.
    # The two ends answer the questions that actually get asked — what does
    # a value look like, and what range is it in — where a bare count of
    # distinct values answers neither.
    wide = [
        f for f in fields
        if (f.get("distinct_values") or 0) > SAMPLE_VALUES_MAX_CARDINALITY
        and not f.get("sample")
    ][:EDGE_VALUES_MAX_FIELDS]
    if not wide:
        return
    try:
        edges = context.engine_api.get_field_edges_batch(
            app_handle, [f["name"] for f in wide], count=EDGE_VALUES_COUNT)
    except Exception as exc:
        logger.debug("Could not read field edges: %s", exc)
        return
    for field in wide:
        ends = edges.get(field["name"])
        if not ends:
            continue
        if ends.get("lowest"):
            field["lowest_values"] = ends["lowest"]
        if ends.get("highest"):
            field["highest_values"] = ends["highest"]


@mcp.tool()
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
    off = max(offset or 0, 0)
    return _ok(context.repo_api.get_comprehensive_apps(lim, off, name, stream,
                                               _to_tribool(published)))



@mcp.tool()
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
        `metainfo` (app_id, name, description, stream, modified_dttm,
        reload_dttm).
        `tables`, each with `name`, `fields_count`, `rows` and the load
        script's `COMMENT TABLE` text where there is one.
        `fields`, each with `name`, `table`, `distinct_values`, Qlik's
        `tags` as one string, `is_key` where true, and `comment` — the
        script's `COMMENT FIELD` text, the only description a column
        carries, worth reading before inferring meaning from a name.
        Fields with few enough values list them in `values`; wider ones
        show `lowest_values` and `highest_values`; a date field carries a
        `sample`, which is the writing to match when naming a period.
        `warnings` names the tables large enough to need a filter.
        Past 60 fields the list becomes `columns` plus `rows` to keep the
        reply small, and values and edges are left out.

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
            # Only what this field does not share with its table. `rows` is
            # the table's row count repeated on every field; `is_key` is
            # false for most of them; empty tags say nothing. Each omission
            # is ~20 characters per field, and a model reads a shorter list
            # more reliably than a longer one saying the same thing.
            entry = {
                "name": f.get("field_name", ""),
                "table": tname,
                "distinct_values": f.get("distinct_values", 0),
            }
            if f.get("is_key"):
                entry["is_key"] = True
            # Qlik's own tags, minus the `$` every one of them carries and
            # joined into one string: `["$numeric","$integer"]` is 26
            # characters of JSON punctuation for 14 of meaning, and the same
            # two words repeat down the whole model.
            tags = _tags_text(f.get("tags", []))
            if tags:
                entry["tags"] = tags
            # COMMENT FIELD text, when the load script sets one. Emitted only
            # when non-empty: most apps comment a handful of fields, and an
            # empty key on every field is pure noise in the LLM context.
            if f.get("comment"):
                entry["comment"] = f["comment"]
            fields.append(entry)

        # Show what the values actually look like. Without this the caller
        # has to guess them, and Qlik answers a wrong guess with a number
        # rather than an error: a filter on 'Moscow' where the data says
        # 'Moskva' returns a clean table of zeros. Measured against a real
        # LLM, guessing cost ten tool calls and two minutes on a question
        # that needs two calls once the values are visible.
        # Skipped on a wide model: the reply drops values and edges there
        # anyway, and reading them would be two pipelined batches paid for
        # nothing.
        if len(fields) <= WIDE_MODEL_FIELDS:
            _attach_sample_values(app_handle, fields)
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
    if huge_tables or big_tables:
        found = huge_tables or big_tables
        max_rows_found = max(t.get("rows", 0) for t in found)
        warnings.append(
            f"{len(found)} table(s) hold a lot of rows, the largest about "
            f"{max_rows_found:,}. Give every query a filter — a period, a "
            f"category, a key — so it reads part of the table rather than "
            f"all of it. `filters` in engine_query is the shortest way to "
            f"say so."
        )
    if high_card_fields:
        max_card = max(f.get("distinct_values", 0) for f in high_card_fields)
        warnings.append(
            f"{len(high_card_fields)} field(s) hold many different values, "
            f"the widest about {max_card:,}. Grouping by one of these "
            f"produces as many rows; rank with `sort_by` and a small "
            f"`limit` instead of returning every group."
        )
    date_fields = [f["name"] for f in fields if _is_temporal(f)][:3]
    if date_fields:
        warnings.append(
            "Date field(s) present: " + ", ".join(date_fields)
            + ". To learn the loaded period, call engine_get_field_range on "
              "one of them. To ask about a period, state it as a filter — "
              '{"field": "' + date_fields[0] + '", "period": "2024"} — and '
              "the server writes the set analysis and reports the period it "
              "actually selected."
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
    if len(fields) > WIDE_MODEL_FIELDS:
        # A model this wide spends most of the reply repeating the same keys.
        # Measured: 33 fields cost 7.6k characters as objects, and the key
        # names are two thirds of that. Past the threshold the same content
        # goes out as a header plus rows — no information lost, and the
        # narrow models that make up the normal case keep the readable form.
        result["fields"] = {
            "columns": WIDE_MODEL_COLUMNS,
            "rows": [[field.get(key) for key in WIDE_MODEL_COLUMNS]
                     for field in fields],
            "note": ("Модель широкая, поэтому поля отданы таблицей: "
                     "columns задаёт порядок значений в каждой строке rows."),
        }
        result.setdefault("warnings", []).append(
            f"{len(fields)} fields — listed as columns+rows to keep the reply "
            f"small. Values and edges are omitted for the same reason; ask "
            f"about a specific field with get_app_field."
        )
    if isinstance(fields_data, dict) and "error" in fields_data:
        result["engine_error"] = fields_data["error"]
    else:
        # Only cache a complete answer. A reply that lost its data model to
        # a refused session must not become the cached truth.
        _DETAILS_CACHE[aid] = {"reload_stamp": reload_stamp, "result": result}
    return _ok(result)


