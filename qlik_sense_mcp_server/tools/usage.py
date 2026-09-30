"""How the published sheets use the model: the `usage` and
`report_filters` sections of `get_app_details`.

The visualizations are the reference answers the app's author wrote. Which
fields they group by, which ones they only filter on inside a set, which
measures stand next to a date - that is what shows which of two similar
fields the report means, and the server reports it instead of choosing.

Input is what `ObjectParser` makes of each object; nothing is calculated.
Field names are read lexically from expressions and kept only when the data
model has them.
"""

import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..engine.objects import clean_expression, cut, fields_in_expression, strip_comments

# Lines a section keeps before the budget has to cut it.
TOP_FIELDS_WITH = 3
TEMPLATE_CHARS = 150
SHORT_MEASURE_CHARS = 40

# Objects that choose what the rest of the sheet shows rather than showing
# figures themselves.
_FILTER_TYPES = frozenset({"filterpane", "listbox", "VizlibFilter"})
_DATE_TYPES = frozenset({"qlik-date-picker", "VizlibCalendar"})

_SET_START = re.compile(r"\{\s*(?:\$|1|\$_?\d+|[\w.]+)?\s*<", re.UNICODE)
_AGGR = re.compile(r"(?<![\w.])aggr\s*\(", re.IGNORECASE)
_MODIFIER_FIELD = re.compile(r"^\s*(\[[^\]]+\]|[^\s=+\-*/\[\]]+)\s*[-+*/]?=", re.UNICODE)

CONDITION, AGGREGATED, AGGR_DIMENSION, DIMENSION = (
    "condition", "aggregated", "Aggr dimension", "dimension")


# -- reading an expression ----------------------------------------------------

def _closing(text: str, start: int, opening: str, closing: str) -> int:
    """Index just past the bracket that closes the one at `start`."""
    depth, i, quote = 0, start, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "[":
            end = text.find("]", i)
            i = len(text) if end < 0 else end
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _split_top(text: str) -> List[str]:
    """Split on commas outside brackets and quotes."""
    parts, depth, quote, start, i = [], 0, None, 0, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "[":
            end = text.find("]", i)
            i = len(text) if end < 0 else end
        elif ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _bare(name: str) -> str:
    name = name.strip()
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def expression_roles(expression: str, rest: str = AGGREGATED) -> Dict[str, Set[str]]:
    """Which field plays which part in an expression.

    A field named on the left of a set modifier is a condition
    (`Status={1}`); a field after the first argument of `Aggr` is the
    dimension the inner aggregation runs over; everything else is
    aggregated - or, for a calculated dimension, `rest`. A lexical
    reading, not a parser: good enough to tell "filtered on in 5 objects"
    from "summed in 3".
    """
    text = " " + " ".join(strip_comments(expression or "").split()) + " "
    roles: Dict[str, Set[str]] = defaultdict(set)

    # Set modifiers, innermost last: blank each one out once read.
    while True:
        match = _SET_START.search(text)
        if not match:
            break
        begin = match.start()
        end = _closing(text, begin, "{", "}")
        body = text[match.end():end - 1].rstrip()
        body = body[:-1] if body.endswith(">") else body
        for part in _split_top(body):
            found = _MODIFIER_FIELD.match(part)
            if found:
                roles[_bare(found.group(1))].add(CONDITION)
                for field in fields_in_expression(part[found.end():]):
                    roles[field].add(CONDITION)
        text = text[:begin] + " " + text[end:]

    # The dimensions of each Aggr are read and cut out; its first
    # argument stays, since an Aggr inside it is read on the next pass.
    position = 0
    while True:
        match = _AGGR.search(text, position)
        if not match:
            break
        open_at = match.end() - 1
        end = _closing(text, open_at, "(", ")")
        arguments = _split_top(text[open_at + 1:end - 1])
        for argument in arguments[1:]:
            for field in fields_in_expression(re.sub(r"<[^>]*>", " ", argument)):
                roles[field].add(AGGR_DIMENSION)
        text = text[:open_at + 1] + arguments[0] + ")" + text[end:]
        position = open_at + 1

    for field in fields_in_expression(text):
        roles[field].add(rest)
    return roles


# -- walking the objects ------------------------------------------------------

def _walk(objects: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for obj in objects:
        yield obj
        yield from _walk(obj.get("children") or [])


def _column_roles(column: Dict[str, Any], is_dimension: bool) -> Dict[str, Set[str]]:
    roles: Dict[str, Set[str]] = defaultdict(set)
    if is_dimension:
        for field in ([column["field"]] if column.get("field") else []) + \
                list(column.get("fields") or column.get("drill_down") or []):
            text = str(field or "").strip()
            if text.startswith("="):
                for name, found in expression_roles(text[1:], DIMENSION).items():
                    roles[name] |= found
            elif text:
                roles[_bare(text)].add(DIMENSION)
        if column.get("expression") and not column.get("master_id"):
            for name, found in expression_roles(column["expression"].lstrip("="), DIMENSION).items():
                roles[name] |= found
    else:
        for key in ("expression", "target"):
            if column.get(key):
                for name, found in expression_roles(column[key]).items():
                    roles[name] |= found
    return roles


def _measure_name(column: Dict[str, Any]) -> str:
    if column.get("master_name"):
        return column["master_name"]
    if column.get("label"):
        return column["label"]
    return cut(column.get("expression", ""), SHORT_MEASURE_CHARS)


def _dimension_text(column: Dict[str, Any]) -> str:
    name = column.get("master_name") or column.get("field") or \
        cut(column.get("expression", ""), SHORT_MEASURE_CHARS)
    if column.get("label") and column["label"] != name:
        return f"[{column['label']}]={name}"
    return name


def summarise(sheets: List[Dict[str, Any]], model_fields: Set[str],
              bookmark_titles: Optional[Dict[str, str]] = None
              ) -> Tuple[Dict[str, Any], List[str]]:
    """`usage` and `report_filters` from the described objects of each sheet.

    `sheets` is `[{"title": ..., "objects": [...]}]`, the objects as the
    per-type parser describes them; `bookmark_titles` names the bookmarks
    buttons apply by id.
    """
    bookmark_titles = bookmark_titles or {}
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "objects": 0, "sheets": set(), "roles": Counter(), "fields": Counter(),
        "measures": Counter()})
    master_use: Counter = Counter()
    formulas: Dict[str, Dict[str, Any]] = {}
    titled: List[str] = []
    special: List[str] = []
    filter_sheets: Dict[str, Set[str]] = defaultdict(set)
    date_sheets: Dict[str, Set[str]] = defaultdict(set)
    variables: Dict[str, Dict[str, Any]] = {}
    buttons: List[str] = []
    tabs: List[str] = []
    used: Set[str] = set()
    seen_titles: Set[str] = set()

    for sheet in sheets:
        sheet_title = sheet.get("title") or sheet.get("id") or ""
        for obj in _walk(sheet.get("objects") or []):
            kind = obj.get("object_type", "")
            label = obj.get("title") or ""
            if obj.get("shown_if"):
                tab = obj.get("tab") or label or kind
                tabs.append(f'"{tab}" -> {kind}{" " + chr(34) + label + chr(34) if label and label != tab else ""}'
                            f" shown if {obj['shown_if']}")
            for control in obj.get("controls") or []:
                action = control.get("action", "")
                if kind in _DATE_TYPES and control.get("field"):
                    date_sheets[control["field"]].add(sheet_title)
                    used.add(_bare(control["field"]))
                elif action == "set variable" and control.get("variable"):
                    entry = variables.setdefault(control["variable"], {"sheets": set()})
                    entry["sheets"].add(sheet_title)
                    if control.get("alternatives"):
                        entry["values"] = ", ".join(control["alternatives"])
                    elif control.get("alternatives_expression"):
                        entry["values"] = "values from " + control["alternatives_expression"]
                elif action not in ("navigate", "select", "select dates") and (
                        control.get("field") or control.get("variable") or control.get("bookmark")):
                    words = [action]
                    for key in ("field", "variable"):
                        if control.get(key):
                            words.append(control[key])
                    if control.get("bookmark"):
                        bookmark = bookmark_titles.get(control["bookmark"], control["bookmark"])
                        words.append(f"bookmark \"{bookmark}\"")
                    text = " ".join(words)
                    if control.get("value"):
                        text += f" to {cut(control['value'], 80)}"
                    line = (f'button "{label}": {text}' if label else f"{kind}: {text}")
                    if line not in buttons:
                        buttons.append(line)
                    if control.get("field"):
                        used.add(_bare(control["field"]))
            if kind in _FILTER_TYPES:
                for column in obj.get("dimensions") or []:
                    for name in _column_roles(column, True):
                        if name in model_fields:
                            filter_sheets[name].add(sheet_title)
                            used.add(name)
                continue
            if kind in _DATE_TYPES:
                continue

            dims = (obj.get("dimensions") or []) + (obj.get("alt_dimensions") or [])
            measures = (obj.get("measures") or []) + (obj.get("alt_measures") or [])
            for layer in obj.get("layers") or []:
                if layer.get("dimension"):
                    dims.append(layer["dimension"])
                measures += layer.get("measures") or []
            extra = [{"expression": e["expression"]} for e in obj.get("expressions") or []
                     if e.get("expression")]
            if not dims and not measures and not extra:
                continue
            roles: Dict[str, Set[str]] = defaultdict(set)
            for column in dims:
                for name, found in _column_roles(column, True).items():
                    roles[name] |= found
            for column in measures + extra:
                for name, found in _column_roles(column, False).items():
                    roles[name] |= found
            roles = {name: found for name, found in roles.items() if name in model_fields}
            names = [_measure_name(m) for m in measures if _measure_name(m)]
            # Neighbours are the fields the object shows or computes; one
            # it only filters on inside a set is in its own role count.
            shown = {name for name, found in roles.items() if found - {CONDITION}}
            for name, found in roles.items():
                entry = stats[name]
                entry["objects"] += 1
                entry["sheets"].add(sheet_title)
                entry["roles"].update(found)
                entry["fields"].update(other for other in shown if other != name)
                entry["measures"].update(set(names))
                used.add(name)
            for column in measures:
                if column.get("master_id"):
                    master_use[column.get("master_name") or column["master_id"]] += 1
                elif column.get("expression") and any(
                        f in model_fields for f in fields_in_expression(column["expression"])):
                    # Only formulas over the data: `[Revenue]` or `$(vLabel)`
                    # name a master measure or a variable, not a template.
                    text = clean_expression(column["expression"])
                    formula = formulas.setdefault(text, {"label": column.get("label") or "",
                                                         "sheet": sheet_title, "objects": 0})
                    formula["objects"] += 1
            if label and label not in seen_titles:
                seen_titles.add(label)
                parts = []
                if dims:
                    parts.append("dims " + ", ".join(_dimension_text(d) for d in dims[:6]))
                if names:
                    parts.append("measures " + ", ".join(names[:6]))
                titled.append(f'"{label}" ({kind}, sheet "{sheet_title}"): ' + "; ".join(parts))
            if obj.get("state") or obj.get("context_set"):
                what = f"state {obj['state']}" if obj.get("state") else \
                    f"set {cut(obj['context_set'], 120)} on the whole object"
                special.append(f'{kind} "{label or obj.get("object_id", "")}" '
                               f'(sheet "{sheet_title}"): {what}')

    field_lines, once = [], []
    for name, entry in sorted(stats.items(), key=lambda item: (-item[1]["objects"], item[0])):
        if entry["objects"] == 1:
            # One object says little about how the report uses a field;
            # these share a line, each with its role.
            once.append(f"{name} ({', '.join(sorted(entry['roles']))})")
            continue
        text = f"{name}: {entry['objects']} objects, {_sheets(len(entry['sheets']))}"
        roles_text = ", ".join(f"{role} in {count}" for role, count in entry["roles"].most_common())
        if roles_text:
            text += f"; {roles_text}"
        if entry["fields"]:
            text += "; with fields: " + ", ".join(
                f for f, _n in entry["fields"].most_common(TOP_FIELDS_WITH))
        if entry["measures"]:
            text += "; with measures: " + ", ".join(
                m for m, _n in entry["measures"].most_common(TOP_FIELDS_WITH))
        field_lines.append(text)

    usage: Dict[str, Any] = {"sheets": [s.get("title") or s.get("id") for s in sheets]}
    if field_lines:
        usage["fields"] = field_lines
    if once:
        usage["fields_in_one_object"] = ", ".join(once)
    unused = sorted(model_fields - used - set(stats))
    if unused:
        usage["unused_fields"] = ", ".join(unused)
    if master_use:
        usage["master_measures"] = ", ".join(f"{name} {count}"
                                             for name, count in master_use.most_common())
    if titled:
        usage["objects"] = titled
    if formulas:
        usage["formulas"] = [
            (f'"{f["label"]}" = ' if f["label"] else "") + cut(text, TEMPLATE_CHARS)
            + f' (sheet "{f["sheet"]}"' + (f", {f['objects']} objects)" if f["objects"] > 1 else ")")
            for text, f in sorted(formulas.items(), key=lambda item: -item[1]["objects"])]
    if special:
        usage["alternate_state_or_set"] = special

    meanings: Dict[str, str] = {}
    for name, entry in variables.items():
        if entry.get("values"):
            meanings[name] = entry["values"]
    filters: List[str] = []
    if filter_sheets:
        filters.append("filter panes, by sheets: " + ", ".join(
            f"{field} {len(sheet_set)}" for field, sheet_set in
            sorted(filter_sheets.items(), key=lambda item: (-len(item[1]), item[0]))))
    for field, sheet_set in date_sheets.items():
        filters.append(f"date picker on {field}: {_sheets(len(sheet_set))}")
    for name, entry in variables.items():
        filters.append(f"variable input {name}" + (f": {entry['values']}" if entry.get("values") else "")
                       + f" ({_sheets(len(entry['sheets']))})")
    filters += buttons
    for line in dict.fromkeys(tabs):
        named = [v for v in meanings if f"$({v})" in line.replace(" ", "")]
        filters.append(line + "".join(f" [{v}: {meanings[v]}]" for v in named))
    return usage, filters


def _sheets(count: int) -> str:
    return f"{count} sheet" if count == 1 else f"{count} sheets"


def trim_usage(reply: Dict[str, Any], fields: int = 10, objects: int = 30,
               formulas: int = 20) -> bool:
    """Keep the most used fields, the first titled objects and formulas."""
    usage = reply.get("usage") or {}
    changed = False
    for key, keep in (("fields", fields), ("objects", objects), ("formulas", formulas)):
        if len(usage.get(key) or []) > keep:
            usage[key] = usage[key][:keep]
            changed = True
    return changed
