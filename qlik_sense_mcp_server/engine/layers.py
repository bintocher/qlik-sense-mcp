"""Calendars whose dates repeat, and the layer of rows that does not.

A calendar with more rows than dates holds several rows per date: as-of,
cumulative or shifted periods, told apart by a field with a few values. A
filter on such a calendar reaches the facts through every date its rows
name, and the figures grow with no error: measured on a 1.16-billion-row
app, one month of a cumulative calendar summed thirteen months of facts.

The structure is read from the model's counts; which rows form the plain
layer - one row per date, the date itself - is asked of Engine, once per
app, over the calendar table alone.
"""

import logging
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

from ..utils import escape_qlik_field_name

logger = logging.getLogger(__name__)

# Fields with this many values or fewer are candidates for the field that
# picks a layer; pairs are tried among fields with this many or fewer.
LAYER_FIELD_MAX = 25
LAYER_PAIR_MAX = 3
LAYER_CANDIDATES_MAX = 8


def _temporal(tags: Any) -> bool:
    words = {str(tag).lstrip("$").lower() for tag in (tags or [])}
    return bool(words & {"date", "timestamp"})


def _model(raw_fields: List[Dict[str, Any]]):
    tables_of: Dict[str, List[str]] = {}
    fields_of: Dict[str, List[str]] = {}
    rows: Dict[str, int] = {}
    info: Dict[str, Dict[str, Any]] = {}
    for entry in raw_fields or []:
        name, table = entry.get("field_name"), entry.get("table_name")
        if not name or not table or entry.get("is_system"):
            continue
        tables_of.setdefault(name, [])
        if table not in tables_of[name]:
            tables_of[name].append(table)
        if name not in fields_of.setdefault(table, []):
            fields_of[table].append(name)
        rows[table] = max(rows.get(table, 0), int(entry.get("table_rows") or 0),
                          int(entry.get("rows_count") or 0))
        known = info.setdefault(name, {"distinct": 0, "tags": entry.get("tags")})
        known["distinct"] = max(known["distinct"], int(entry.get("distinct_values") or 0))
    return tables_of, fields_of, rows, info


def repeating_path(raw_fields: List[Dict[str, Any]], field: str
                   ) -> Optional[Dict[str, Any]]:
    """How a field reaches the largest table, and where dates repeat on
    the way.

    Walked over the shared fields from the field's own table to the table
    with the most rows. `key` is the field the walk enters that table by;
    `repeats` lists the tables on the way whose date key towards the facts
    has fewer values than the table has rows - one date, several rows.
    None when the field sits in the largest table or reaches it by nothing.
    """
    return _walk(raw_fields, _model(raw_fields)[0].get(field))


def _walk(raw_fields: List[Dict[str, Any]], start: Optional[List[str]]
          ) -> Optional[Dict[str, Any]]:
    tables_of, fields_of, rows, info = _model(raw_fields)
    if not start or not rows:
        return None
    facts = max(rows, key=lambda table: rows[table])
    if facts in start:
        return None
    entered: Dict[str, Tuple[str, str]] = {}
    visited = set(start)
    queue = list(start)
    while queue:
        table = queue.pop(0)
        for name in fields_of.get(table, []):
            for other in tables_of.get(name, []):
                if other in visited:
                    continue
                visited.add(other)
                entered[other] = (name, table)
                queue.append(other)
    if facts not in entered:
        return None
    key = entered[facts][0]
    path: List[Tuple[str, str]] = []
    table = facts
    while table in entered:
        name, previous = entered[table]
        path.append((previous, name))
        table = previous
    repeats = []
    for table, towards in reversed(path):
        dates = info.get(towards, {}).get("distinct", 0)
        if (_temporal(info.get(towards, {}).get("tags")) and dates
                and dates < rows.get(table, 0)):
            repeats.append({"table": table, "key": towards,
                            "rows": rows[table], "dates": dates})
    # The fields of the tables between the field and the facts: the part of
    # a set that decides which key values the facts are reached through.
    between = sorted({name for table, _ in path for name in fields_of.get(table, [])})
    return {"key": key, "table": facts,
            "temporal": _temporal(info.get(key, {}).get("tags")),
            "repeats": repeats, "calendar_fields": between}


def repeating_calendars(raw_fields: List[Dict[str, Any]]
                        ) -> List[Dict[str, Any]]:
    """Every table whose date key towards the facts repeats, with the
    fields that may pick a layer of it and the dates beside its key."""
    tables_of, fields_of, rows, info = _model(raw_fields)
    shared = {name for name, tables in tables_of.items() if len(tables) > 1}
    found, seen = [], set()
    for table, names in fields_of.items():
        path = _walk(raw_fields, [table])
        repeat = next((r for r in (path or {}).get("repeats", [])
                       if r["table"] == table), None)
        if repeat is None or table in seen:
            continue
        seen.add(table)
        key = repeat["key"]
        dates = [other for other in names if other != key
                 and _temporal(info.get(other, {}).get("tags"))]
        candidates = sorted(
            (other for other in names
             if other not in shared and other not in dates
             and 1 < info.get(other, {}).get("distinct", 0) <= LAYER_FIELD_MAX),
            key=lambda other: info[other]["distinct"])
        found.append(dict(repeat, dates_beside=dates,
                          days=max([info[d]["distinct"] for d in dates] or [0]),
                          candidates=candidates[:LAYER_CANDIDATES_MAX],
                          distinct={c: info[c]["distinct"] for c in candidates}))
    return found


def _condition_parts(fields: List[str], values: List[str]) -> List[str]:
    from .filters import quote_value
    return [f"{escape_qlik_field_name(field)}={{{quote_value(value)}}}"
            for field, value in zip(fields, values)]


def layer_text(layer: Dict[str, Any]) -> str:
    """One sentence: which rows of the calendar are the plain layer."""
    table, key = layer["table"], layer["key"]
    text = (f"{table} holds {layer['rows']} rows for {layer['dates']} dates of "
            f"{key}: one date stands for several rows")
    if "plain" not in layer:
        return text + ", and a filter on it counts each of them."
    plain = layer["plain"]
    if not plain:
        return text + "; no field of it picks one row per date."
    options = [" and ".join(f"[{field}] = '{value}'"
                            for field, value in zip(option["fields"], option["values"]))
               for option in plain]
    return (text + ". One row per date, the date itself: " + " or ".join(options)
            + " - set it with every filter on this calendar, e.g. "
            + "{<" + plain[0]["condition"] + ">}.")


class EngineLayersMixin:
    """Asks Engine which layer of a repeating calendar is the plain one."""

    def plain_layers(self, app_handle: int,
                     raw_fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The repeating calendars with the rows that make each date once.

        A value of a candidate field (or a pair of values) picks the plain
        layer when its rows hold every date of the key exactly once and
        each row's other date equals the key - measured per value over the
        calendar table only, which is small. Remembered per app.
        """
        calendars = repeating_calendars(raw_fields)
        if not calendars:
            return []
        cache_for = getattr(self, "_app_cache", None)
        cache = cache_for("_plain_layers") if cache_for else {}
        signature = tuple((c["table"], c["rows"], c["dates"]) for c in calendars)
        if cache.get("signature") == signature:
            return cache["layers"]
        layers = []
        for calendar in calendars:
            try:
                plain = self._plain_values(app_handle, calendar)
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    raise
                logger.debug("Could not read the layers of %s: %s",
                             calendar["table"], exc)
                plain = None
            layer = {k: calendar[k] for k in ("table", "key", "rows", "dates")}
            if plain is not None:
                layer["plain"] = plain
            layers.append(layer)
        cache["signature"], cache["layers"] = signature, layers
        return layers

    def _layer_rows(self, app_handle: int, fields: List[str], key: str,
                    dates: List[str]) -> List[List[Any]]:
        """Per value of `fields`: rows, distinct keys, rows equal to each date."""
        name = escape_qlik_field_name(key)
        measures = [{"expression": f"Count({escape_qlik_field_name(fields[0])})",
                     "label": "rows"},
                    {"expression": f"Count(DISTINCT {name})", "label": "keys"}]
        for position, other in enumerate(dates):
            measures.append({
                "expression": (f"Sum(If({escape_qlik_field_name(other)}={name}"
                               f",1,0))"),
                "label": f"same{position}"})
        # In load order: of values holding the same rows, the one loaded
        # first - the base the others were built from - comes first.
        order = {"qSortByNumeric": 0, "qSortByAscii": 0, "qSortByLoadOrder": 1,
                 "qSortByExpression": 0, "qExpression": ""}
        reply = self.create_hypercube(
            app_handle, [{"field": field, "sort_by": dict(order)} for field in fields],
            measures,
            max_rows=LAYER_FIELD_MAX * LAYER_FIELD_MAX, include_raw_layout=True,
            run_checks=False)
        if reply.get("error"):
            raise RuntimeError(reply["error"])
        rows = reply.get("rows") or []
        # A value as Qlik shows it: a dual field (a fiscal year `24-25`
        # over the number 2025) is selected by its text.
        pages = (reply.get("hypercube_data") or {}).get("qDataPages") or []
        shown = [cells for page in pages for cells in page.get("qMatrix") or []]
        if len(shown) == len(rows):
            rows = [[cell.get("qText") for cell in cells[:len(fields)]]
                    + row[len(fields):] for cells, row in zip(shown, rows)]
        return rows

    def _plain_values(self, app_handle: int,
                      calendar: Dict[str, Any]) -> List[Dict[str, Any]]:
        key, dates = calendar["key"], calendar["dates_beside"]

        def plain(row, width):
            # One row per date, and that row's other date is the date
            # itself; with no other date to compare, every date once.
            counts = row[width:]
            if not all(isinstance(c, (int, float)) for c in counts):
                return False
            rows, keys = counts[0], counts[1]
            if rows <= 0 or rows != keys:
                return False
            if dates:
                # Every date once: a layer filled for some dates only - a
                # field set on the plain rows of one year - is not it.
                return (keys >= calendar.get("days", 0)
                        and any(same == rows for same in counts[2:]))
            return keys == calendar["dates"]

        def text(value):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            return str(value)

        # One condition is named: several values holding the very same rows
        # (a month-to-date layer on a calendar of months holds each month
        # once, as the plain one does) are one layer, and naming a
        # cumulative one sends a caller to it. The first field that has
        # such a value, and of it the value loaded first.
        found = []
        for field in calendar["candidates"]:
            for row in self._layer_rows(app_handle, [field], key, dates):
                if plain(row, 1):
                    found.append({"fields": [field], "values": [text(row[0])]})
                    break
            if found:
                break
        if not found:
            few = [c for c in calendar["candidates"]
                   if calendar["distinct"].get(c, 0) <= LAYER_PAIR_MAX]
            for pair in combinations(few, 2):
                for row in self._layer_rows(app_handle, list(pair), key, dates):
                    if plain(row, 2):
                        found.append({"fields": list(pair),
                                      "values": [text(row[0]), text(row[1])]})
                        break
                if found:
                    break
        for option in found:
            option["parts"] = _condition_parts(option["fields"], option["values"])
            option["condition"] = ",".join(option["parts"])
        return found
