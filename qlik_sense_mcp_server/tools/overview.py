"""The data model as `get_app_details` tells it: tables, links, fields,
master items - within a size a client keeps in context.

Nothing here talks to Qlik. The tool reads the model, the library and the
values, and these functions shape them. The server states what the model
is - which table holds the facts, which keys link what, how many dates a
calendar has against its rows - and does not choose for the caller which
field answers a question.
"""

import datetime
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..engine.layers import layer_text
from ..engine.objects import fields_in_expression, measure_text
from ..engine.outer_set import filter_limits

# A table this large next to the largest one of its group is a fact table
# too: a second fact (bets beside their settlements) links to the first
# by a key that is unique on its own side, and would otherwise read as a
# dimension table.
FACT_SHARE_OF_LARGEST = 0.1
# Fields with this many distinct values or fewer list them.
LIST_VALUES_MAX = 25
# A calendar's fields with this many values or fewer, besides its date,
# are named next to it: when dates repeat, rows of one date differ by them.
CALENDAR_SPLIT_MAX = 25
# Measure expressions are cut here; the whole expression is used when the
# measure is named in engine_query, and `get_app_object` reads it by id.
MEASURE_EXPRESSION_CHARS = 150

# Size of the reply. Clients cut or divert a tool result past about 25k
# tokens, and a reply that is diverted to a file is a reply the model never
# reads. Tokens are estimated as characters / 2.5, which is what mixed
# Latin and Cyrillic JSON measured at.
TOKEN_BUDGET = 20_000
CHARS_PER_TOKEN = 2.5
TRIMMED_VALUES = 10
TRIMMED_EXPRESSION_CHARS = 80

# Suffixes that make a measure a variant of another one: last year, year
# over year, share or percentage points. A measure named "<base> <suffix>"
# next to a measure named "<base>" is folded into it.
_VARIANT_TOKEN = re.compile(
    r"^(y-?\d+|yoy|ly|py|%|p\.?p\.?|п\.?п\.?|vs|delta|diff|Δ|abs|rel)$", re.IGNORECASE)


def _words(tags: Any) -> Set[str]:
    if isinstance(tags, str):
        items = tags.split()
    else:
        items = [str(t) for t in tags or []]
    return {t.lstrip("$").lower() for t in items}


def field_type(tags: Any) -> Optional[str]:
    """date, timestamp, number or text, from Qlik's tags."""
    words = _words(tags)
    if "date" in words:
        return "date"
    if "timestamp" in words:
        return "timestamp"
    if "numeric" in words or "integer" in words:
        return "number"
    if "text" in words or "ascii" in words:
        return "text"
    return None


def is_temporal(tags: Any) -> bool:
    return field_type(tags) in ("date", "timestamp")


# -- tables and links ---------------------------------------------------------

def describe_model(raw_fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Tables with their role, the links between them and the islands.

    Roles come from the shape of the model, never from names: a table
    that shares no field with another is an island; in each linked group
    the largest table holds facts, and so does any table of at least a
    tenth of its rows; a table reached from its neighbour through a date
    key is a calendar; the rest are dimension tables. Links are walked
    outward from the facts, so `A -key-> B` reads from the facts towards
    what describes them.
    """
    fields = [f for f in raw_fields if not f.get("is_system") and f.get("field_name")]
    order: List[str] = []
    by_table: Dict[str, List[Dict[str, Any]]] = {}
    rows: Dict[str, int] = {}
    comments: Dict[str, str] = {}
    for f in fields:
        table = f.get("table_name") or ""
        if table not in by_table:
            order.append(table)
            by_table[table] = []
        by_table[table].append(f)
        rows[table] = max(rows.get(table, 0), int(f.get("table_rows") or 0),
                          int(f.get("rows_count") or 0))
        if f.get("table_comment") and table not in comments:
            comments[table] = f["table_comment"]

    tables_of: Dict[str, List[str]] = {}
    for table in order:
        for f in by_table[table]:
            tables_of.setdefault(f["field_name"], [])
            if table not in tables_of[f["field_name"]]:
                tables_of[f["field_name"]].append(table)
    shared = {name for name, tables in tables_of.items() if len(tables) > 1}
    field_info = {f["field_name"]: f for f in fields}

    # Groups of linked tables, each walked from its largest table.
    seen: Set[str] = set()
    groups: List[List[str]] = []
    for table in sorted(order, key=lambda t: -rows.get(t, 0)):
        if table in seen:
            continue
        group, queue = [], [table]
        seen.add(table)
        while queue:
            current = queue.pop(0)
            group.append(current)
            for f in by_table[current]:
                for other in tables_of[f["field_name"]]:
                    if other not in seen:
                        seen.add(other)
                        queue.append(other)
        groups.append(group)

    roles: Dict[str, str] = {}
    links: List[str] = []
    parent_key: Dict[str, str] = {}
    walk_order: List[str] = []
    for position, group in enumerate(groups):
        # The group of the largest table is the model even when it is one
        # table on its own; any other lone table is an island.
        if position and len(group) == 1:
            roles[group[0]] = "island"
            continue
        root = group[0]
        largest = rows.get(root, 0)
        visited = {root}
        queue = [root]
        linked: Set[Tuple[str, str, str]] = set()
        keys_done: Set[str] = set()
        while queue:
            current = queue.pop(0)
            walk_order.append(current)
            for f in by_table[current]:
                key = f["field_name"]
                # A key shared by several tables is one association: it is
                # followed once, from the first table that reaches it.
                if key not in shared or key in keys_done:
                    continue
                keys_done.add(key)
                for other in tables_of[key]:
                    if other == current:
                        continue
                    if other not in visited:
                        visited.add(other)
                        queue.append(other)
                        parent_key[other] = key
                        links.append(f"{current} -{key}-> {other}")
                        linked.add(tuple(sorted((current, other))) + (key,))
                    elif tuple(sorted((current, other))) + (key,) not in linked:
                        # A second path between tables already reached: a
                        # loop in the model, shown without a direction.
                        linked.add(tuple(sorted((current, other))) + (key,))
                        links.append(f"{current} -{key}- {other}")
        for table in group:
            if table == root or (largest and rows.get(table, 0) >= FACT_SHARE_OF_LARGEST * largest):
                roles[table] = "fact"
            elif is_temporal((field_info.get(parent_key.get(table, "")) or {}).get("tags")):
                roles[table] = "calendar"
            else:
                roles[table] = "dimension table"

    tables = []
    for table in walk_order:
        entry: Dict[str, Any] = {"name": table, "role": roles[table], "rows": rows.get(table, 0)}
        if comments.get(table):
            entry["comment"] = comments[table]
        if roles[table] == "calendar":
            key = parent_key[table]
            dates = min(int(field_info[key].get("distinct_values") or 0), entry["rows"]) \
                or entry["rows"]
            text = f"rows {entry['rows']}, distinct dates {dates} ({key})"
            if dates < entry["rows"]:
                few = [f"{f['field_name']} ({f.get('distinct_values')})" for f in by_table[table]
                       if f["field_name"] not in shared
                       and 1 < int(f.get("distinct_values") or 0) <= CALENDAR_SPLIT_MAX]
                if few:
                    text += "; fields with few values next to the date: " + ", ".join(few)
            entry["calendar"] = text
        tables.append(entry)
    islands = [t for t in order if roles[t] == "island"]
    return {"tables": tables, "links": links, "islands": islands,
            "by_table": by_table, "shared": shared, "rows": rows}


def field_entry(f: Dict[str, Any], shared: Set[str]) -> Dict[str, Any]:
    """One field: name, distinct values, type, and what else it carries."""
    entry: Dict[str, Any] = {"name": f["field_name"],
                             "distinct": int(f.get("distinct_values") or 0)}
    kind = field_type(f.get("tags"))
    if kind:
        entry["type"] = kind
    if f["field_name"] in shared:
        entry["key"] = True
    if f.get("is_hidden") or "hidden" in _words(f.get("tags")):
        entry["hidden"] = True
    if f.get("comment"):
        entry["comment"] = f["comment"]
    return entry


def model_sections(raw_fields: List[Dict[str, Any]], values: Dict[str, List[str]],
                   ranges: Dict[str, Dict[str, List[str]]]) -> Dict[str, Any]:
    """`tables` with their fields grouped under them, `links`, `islands`.

    A key field is listed once, under the first table the walk reaches it
    from - the fact side. A field with few values lists them; a date
    field carries its first and last value.
    """
    model = describe_model(raw_fields)
    placed: Set[str] = set()

    def entries(table: str) -> List[Dict[str, Any]]:
        out = []
        for f in model["by_table"][table]:
            name = f["field_name"]
            if name in placed:
                continue
            placed.add(name)
            entry = field_entry(f, model["shared"])
            if name in values and 0 < entry["distinct"] <= LIST_VALUES_MAX:
                entry["values"] = values[name]
            elif name in ranges:
                ends = ranges[name]
                if ends.get("lowest"):
                    entry["min"] = ends["lowest"][0]
                if ends.get("highest"):
                    entry["max"] = ends["highest"][0]
            out.append(entry)
        return out

    tables = []
    for table in model["tables"]:
        table["fields"] = entries(table["name"])
        tables.append(table)
    islands = []
    for table in model["islands"]:
        parts = []
        for entry in entries(table):
            text = entry["name"]
            if entry.get("values"):
                text += " = " + " | ".join(entry["values"])
            else:
                text += f" ({entry['distinct']} values)"
            parts.append(text)
        islands.append(f"{table} ({model['rows'].get(table, 0)} rows): " + "; ".join(parts))
    return {"tables": tables, "links": model["links"], "islands": islands}


def apply_layers(tables: List[Dict[str, Any]],
                 layers: Iterable[Dict[str, Any]]) -> List[str]:
    """Say on each repeating calendar which rows are the plain layer.

    `layers` come from Engine: a table whose date key repeats, and the
    field values that pick one row per date (`plain`, empty when none
    does). The table gets `plain_layer`, the condition to write; the
    fields that pick it keep their values even when hidden - a hidden
    field is still the one the figures depend on. Returns one warning per
    such calendar.
    """
    by_name = {table.get("name"): table for table in tables}
    warnings = []
    for layer in layers:
        table = by_name.get(layer.get("table"))
        if table is None:
            continue
        options = layer.get("plain") or []
        if options:
            table["plain_layer"] = " or ".join(
                "{<" + option["condition"] + ">}" for option in options)
            picked = {field for option in options for field in option["fields"]}
            for entry in table.get("fields") or []:
                if entry.get("name") in picked:
                    entry["picks_plain_layer"] = True
        warnings.append(layer_text(layer))
    return warnings


# -- the description against the data ----------------------------------------

_DAY_DOTTED = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})")
_DAY_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_MONTH_ISO = re.compile(r"^(\d{4})-(\d{2})$")
_MONTH_DOTTED = re.compile(r"^(\d{2})\.(\d{4})$")
_SERIAL = re.compile(r"^\d{5}(\.\d+)?$")
_EPOCH = datetime.date(1899, 12, 30)

# Dates and spans an author writes into a description. The span words are
# English and Russian, the two languages descriptions were seen in.
_DATES_IN_TEXT = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2})\b")
_SPAN_IN_TEXT = re.compile(
    r"\b(\d{1,3})\s*(months?|мес(?:яцев|яца|яц)?|days?|дн(?:я|ей|ь)|"
    r"weeks?|недел(?:я|и|ь)|years?|год(?:а)?|лет)(?![\w])", re.IGNORECASE)
_SPAN_DAYS = {"month": 30.44, "мес": 30.44, "day": 1, "дн": 1, "week": 7,
              "недел": 7, "year": 365.25, "год": 365.25, "лет": 365.25}


def _day(text: Any, upper: bool = False) -> Optional[datetime.date]:
    """A value as Qlik displays a date, read back as a day; a month is its
    first or last day depending on the side."""
    text = str(text or "").strip()
    try:
        match = _DAY_DOTTED.match(text)
        if match:
            day, month, year = (int(g) for g in match.groups())
            return datetime.date(year, month, day)
        match = _DAY_ISO.match(text)
        if match:
            return datetime.date(*(int(g) for g in match.groups()))
        match = _MONTH_ISO.match(text) or _MONTH_DOTTED.match(text)
        if match:
            first, second = (int(g) for g in match.groups())
            year, month = (first, second) if first > 12 else (second, first)
            start = datetime.date(year, month, 1)
            if not upper:
                return start
            following = datetime.date(year + month // 12, month % 12 + 1, 1)
            return following - datetime.timedelta(days=1)
        if _SERIAL.match(text):
            return _EPOCH + datetime.timedelta(days=int(float(text)))
    except (ValueError, OverflowError):
        return None
    return None


def date_ranges(tables: Iterable[Dict[str, Any]]
                ) -> List[Tuple[str, datetime.date, datetime.date]]:
    """First and last day of every date field, from `min`/`max` or its values."""
    out = []
    for table in tables or []:
        for entry in table.get("fields") or []:
            if entry.get("type") not in ("date", "timestamp"):
                continue
            if entry.get("min") and entry.get("max"):
                low, high = _day(entry["min"]), _day(entry["max"], upper=True)
            else:
                days = [d for d in (_day(v) for v in entry.get("values") or []) if d]
                ends = [d for d in (_day(v, upper=True) for v in entry.get("values") or []) if d]
                low, high = (min(days), max(ends)) if days and ends else (None, None)
            if low and high and low <= high:
                out.append((entry["name"], low, high))
    return out


def description_disagrees(description: str,
                          ranges: List[Tuple[str, datetime.date, datetime.date]]
                          ) -> Optional[str]:
    """A warning when the description names dates or a span the data lacks.

    A date in the description must fall inside some date field's range; a
    span ("2 months") must be the length of one of them, give or take a
    quarter of it or a month. Descriptions are written once and outlive
    the reloads that change the data; which date field is the period is
    still the caller's call.
    """
    if not description or not ranges:
        return None
    said: List[str] = []
    for text in _DATES_IN_TEXT.findall(description):
        day = _day(text)
        if day and not any(low <= day <= high for _, low, high in ranges):
            said.append(text)
    for match in _SPAN_IN_TEXT.finditer(description):
        count, unit = int(match.group(1)), match.group(2).lower()
        size = next((days for word, days in _SPAN_DAYS.items()
                     if unit.startswith(word)), None)
        if not size or not count:
            continue
        wanted = count * size
        slack = max(wanted * 0.25, 31)
        if not any(abs((high - low).days + 1 - wanted) <= slack
                   for _, low, high in ranges):
            said.append(match.group(0))
    if not said:
        return None
    # In the order of `tables`, from the facts outward: the ranges nearest
    # the data come first.
    loaded = ", ".join(f"{name} {low.isoformat()}..{high.isoformat()}"
                       for name, low, high in ranges[:3])
    return (f"The app description says " + ", ".join(repr(s) for s in said)
            + f", which matches none of the loaded date ranges ({loaded}). "
            f"The description disagrees with the data; the ranges in "
            f"`tables` are what is loaded.")


# -- master items -------------------------------------------------------------

def _variant_suffix(name: str, base: str) -> Optional[str]:
    """The suffix that makes `name` a variant of `base`, or None."""
    full, root = " ".join(name.split()), " ".join(base.split())
    if full.lower() == root.lower() or not full.lower().startswith(root.lower()):
        return None
    rest = full[len(root):]
    if rest[:1].isalnum() and root[-1:].isalnum():
        return None  # "GrossWin" is not a variant of "Gross"
    tokens = [t for t in re.split(r"[\s,;/()_]+", rest) if t]
    if tokens and all(_VARIANT_TOKEN.match(t) for t in tokens):
        return rest.strip(" ,_")
    return None


def measure_entries(measures: Iterable[Dict[str, Any]], model_fields: Set[str],
                    limit: int = MEASURE_EXPRESSION_CHARS) -> List[Dict[str, Any]]:
    """Master measures: name, description, fields, expression cut short.

    A measure named "<base> Y-1", "<base> YoY, %" or "<base>, %" next to
    a measure named "<base>" is listed under it as a variant, by its exact
    name - `engine_query` computes it by that name.
    """
    measures = [m for m in measures if m.get("name")]
    base_of: Dict[str, str] = {}
    for m in measures:
        best = None
        for other in measures:
            if other is m:
                continue
            if _variant_suffix(m["name"], other["name"]) is not None:
                if best is None or len(other["name"]) > len(best):
                    best = other["name"]
        if best:
            base_of[m["name"]] = best
    # A variant of a variant belongs to the root.
    for name in list(base_of):
        seen = {name}
        while base_of.get(base_of[name]) and base_of[name] not in seen:
            seen.add(base_of[name])
            base_of[name] = base_of[base_of[name]]

    out: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for m in measures:
        if m["name"] in base_of:
            continue
        expression = measure_text(m.get("expression", ""))
        entry: Dict[str, Any] = {"name": m["name"]}
        if m.get("description"):
            entry["description"] = m["description"]
        used = [f for f in fields_in_expression(expression) if f in model_fields]
        if used:
            entry["fields"] = used
        entry["expression"] = expression
        # What keeps a filter from reaching the measure, known before the
        # measure is asked for: the fields it sets itself, a set of its own
        # that ignores an outer one, a `$(=...)` computed before the cube.
        limits = filter_limits(m.get("expression", ""))
        if limits:
            entry["filter_limits"] = limits
        entry["_id"] = m.get("id")
        _cut_expression(entry, limit)
        index[m["name"]] = entry
        out.append(entry)
    for m in measures:
        base = base_of.get(m["name"])
        if base and base in index:
            index[base].setdefault("variants", []).append(m["name"])
    return out


def _cut_expression(entry: Dict[str, Any], limit: int) -> None:
    """Cut an expression for reading; the id then says where the whole one is."""
    full = entry.get("_full_expression", entry.get("expression") or "")
    if len(full) > limit:
        entry["_full_expression"] = full
        entry["expression"] = full[:limit] + "..."
        entry["expression_truncated"] = True
        if entry.get("_id"):
            entry["id"] = entry["_id"]


def dimension_lines(dimensions: Iterable[Dict[str, Any]]) -> List[str]:
    """Master dimensions as `name = field` lines."""
    return [d.get("definition") or d.get("name") for d in dimensions
            if d.get("definition") or d.get("name")]


# -- the size budget ----------------------------------------------------------

def estimate_tokens(reply: Dict[str, Any]) -> int:
    text = json.dumps(_public(reply), ensure_ascii=False, separators=(",", ":"))
    return int(len(text) / CHARS_PER_TOKEN)


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


def fit_budget(reply: Dict[str, Any], steps: List[Tuple[str, Any]],
               budget: int = TOKEN_BUDGET) -> Dict[str, Any]:
    """Apply the trimming steps in order until the reply fits.

    Each step is (what it trims, function taking the reply). What was
    applied is listed in `size_budget.trimmed`, so the caller knows a list
    is shorter than the model and where to ask for the rest.
    """
    trimmed: List[str] = []
    for label, step in steps:
        if estimate_tokens(reply) <= budget:
            break
        if step(reply):
            trimmed.append(label)
    reply = _public(reply)
    reply["size_budget"] = {"estimated_tokens": estimate_tokens(reply), "limit": budget}
    if trimmed:
        reply["size_budget"]["trimmed"] = trimmed
    return reply


def trim_values(reply: Dict[str, Any]) -> bool:
    changed = False
    for table in reply.get("tables") or []:
        for entry in table.get("fields") or []:
            # The field that picks the plain layer keeps every value: the
            # one the condition names must stay readable.
            if entry.get("picks_plain_layer"):
                continue
            if len(entry.get("values") or []) > TRIMMED_VALUES:
                entry["values"] = entry["values"][:TRIMMED_VALUES]
                changed = True
    return changed


def trim_measures(reply: Dict[str, Any]) -> bool:
    changed = False
    for entry in reply.get("measures") or []:
        before = entry.get("expression")
        _cut_expression(entry, TRIMMED_EXPRESSION_CHARS)
        changed = changed or entry.get("expression") != before
    return changed
