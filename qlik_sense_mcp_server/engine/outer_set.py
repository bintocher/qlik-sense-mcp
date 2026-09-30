"""A filter written in front of an expression, and what can keep it out.

A set expression written before a whole expression - an outer set - is
standard Qlik syntax: it reaches every aggregation inside, a master measure
named by its label included. Measured on a model of 1.16 billion rows:

    {<Date={">=01.08.2026<=31.08.2026"}>} [GGR]      the reference figure
    {<Date=...>} (<the measure's own expression>)     the same figure
    {<Date=...>} [Margin]                             a ratio, right too
    {<Date=..., Flag={1}>} [GGR]                      the reference again:
                                                      the measure sets Flag
                                                      itself, and its own
                                                      condition won

Three things keep an outer set out, and none of them is an error:

1. a field the measure sets itself - its own condition wins;
2. a set with an identifier inside the measure (`{1<...>}`, `{$<...>}`, a
   bookmark) - it does not inherit the outer set at all;
3. `$(=...)` - computed once, before the cube and without the filter.

Two sets side by side are a different matter. Inside one aggregation Qlik
reads one set and drops the other without a word: measured on the same
model, `Sum({<Date=...>} {<Flag={1}>} Amount)` answered the sum over every
date. A filter marked next to a set of the expression's own is therefore
merged into that set, and one that cannot be merged is refused.

Nothing here talks to Qlik: the measure is read, not evaluated.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Where a described filter goes inside an expression the caller wrote.
FILTER_MARKER = "{filter}"
# The word of the marker; `{filter <A={1}>}` is the marker and a set of the
# expression's own written in one pair of braces.
_MARKER_WORD = "filter"

_FIELD_IN_MODIFIER = re.compile(
    r"^\s*(?:[^\s:\[\]]+::)?(\[[^\]]+\]|[^\s=+\-*/<>\[\]]+)\s*[+\-*/]?=")
_COMPUTED_FIRST = re.compile(r"\$\(\s*=")
# What may stand between the opening parenthesis of an aggregation and the
# expression it aggregates, besides sets.
_PREFIX_KEYWORD = re.compile(r"(?i)(DISTINCT|NODISTINCT|ALL|TOTAL)\b")
# Aggregations: the functions whose set decides which rows they read.
AGGREGATIONS = frozenset(name.lower() for name in (
    "Sum", "Count", "Avg", "Min", "Max", "Only", "Median", "Mode",
    "Fractile", "FractileExc", "Stdev", "Sterr", "Concat", "MinString",
    "MaxString", "FirstSortedValue", "Kurtosis", "Skew", "Correl",
    "LinEst_M", "LinEst_B"))
_SET_IDENTIFIER = re.compile(r"^(\$_?\d*|\d+|\[[^\]]*\]|[\w.\-]+)?$")
_SET_DIFFERENCE = re.compile(r"^(\$_?\d*|\d+)\s*-\s*(\$_?\d*|\d+)$")


class FilterPlacementError(ValueError):
    """A filter marker that cannot be honoured where it stands."""


def outer_modifier(modifier: str) -> str:
    """The same set, marked to keep an empty result empty.

    A chain of outer sets resets a field that one of them emptied to "all"
    unless the first carries `&`. Measured: `{&<...>}` answers the same
    figure as `{<...>}` wherever the set selects something.
    """
    if modifier.startswith("{<"):
        return "{&" + modifier[1:]
    return modifier


def _body(expression: str) -> str:
    """The expression without the `=` a measure may start with."""
    text = (expression or "").strip()
    return text[1:].lstrip() if text.startswith("=") else text


def _marker_rest(content: str) -> Optional[str]:
    """For the text inside a pair of braces: None when it is not the
    marker, "" for `{filter}`, and the set written after the word for
    `{filter <A={1}>}`."""
    text = content.strip()
    if not text.startswith(_MARKER_WORD):
        return None
    rest = text[len(_MARKER_WORD):]
    if not rest:
        return ""
    if not (rest[0].isspace() or rest[0] == "<"):
        return None
    return rest.strip()


def has_marker(expression: str) -> bool:
    """Whether the expression marks a place for the filter."""
    return any(item[0] == "marker" for run in _runs(expression or "")
               for item in run["items"]) or FILTER_MARKER in (expression or "")


def marks_outer(expression: str) -> bool:
    """Whether the filter marker stands in front of the whole expression."""
    body = _body(expression)
    run = _prefix_run(body, 0)
    return any(item[0] == "marker" for item in run["items"])


def in_front(modifier: str, expression: str) -> str:
    """The expression with the filter as its outer set.

    Used for a master measure named by its label, whose own sets stay
    inside it and inherit this one.
    """
    return f"{outer_modifier(modifier)} {_body(expression)}"


def place_filter(expression: str, modifier: str) -> str:
    """Every marker replaced by the filter.

    In front of the whole expression the marker is an outer set; at the
    start of an aggregation it narrows that aggregation. Either way a set
    of the expression's own standing in the same place is merged with the
    filter into one set, because Qlik reads only one of two sets side by
    side. Raises FilterPlacementError when that cannot be done faithfully.
    """
    text = _body(expression)
    # One marker at a time, re-reading the text after each: a marker inside
    # P() sits in a set that an outer marker may just have rewritten.
    for _ in range(text.count("{") + 1):
        runs = [run for run in _runs(text)
                if any(item[0] == "marker" for item in run["items"])]
        if not runs:
            break
        run = runs[-1]
        written = _merged_run(run, modifier, text)
        end = run["end"]
        if not written:
            # Nothing left where the run stood: its spaces go with it.
            while end < len(text) and text[end].isspace():
                end += 1
            while run["start"] < len(text) and text[run["start"]].isspace():
                run["start"] += 1
        text = text[:run["start"]] + written + text[end:]
    # A marker that stands nowhere a set can go is replaced as written:
    # Qlik then says what it makes of it.
    return text.replace(FILTER_MARKER, modifier)


def _merged_run(run: Dict[str, Any], modifier: str, text: str) -> str:
    """The sets and keywords of one run, the filter merged into its set."""
    owns: List[str] = []
    keywords: List[str] = []
    for kind, start, end, rest in run["items"]:
        if kind == "set":
            owns.append(text[start:end])
        elif kind == "marker" and rest:
            owns.append("{" + rest + "}")
        elif kind == "keyword":
            keywords.append(text[start:end])
    if modifier:
        merged = merge_sets(modifier, owns)
        if run["outer"]:
            merged = outer_modifier(merged)
    else:
        # No filter for this measure: the marker goes, its own set stays.
        merged = " ".join(owns)
    written = " ".join(part for part in [merged] + keywords if part)
    # The run swallows the space after it; the expression keeps one.
    follows = text[run["end"]:run["end"] + 1]
    if not written:
        return ""
    return written + ("" if not follows or follows in " )," else " ")


def _parse_set(written: str) -> Optional[Tuple[str, List[str]]]:
    """A set `{ident<a, b>}` as (identifier, modifiers), or None when it
    is anything else - sets joined by an operator, above all."""
    text = written.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    content = text[1:-1].strip()
    if content.startswith("&"):
        content = content[1:].strip()
    opening = _first_angle(content)
    if opening < 0:
        identifier, modifiers = content, ""
    else:
        identifier = content[:opening].strip()
        closing = _angle_closing(content, opening)
        if closing < 0 or content[closing + 1:].strip():
            return None
        modifiers = content[opening + 1:closing]
    if not _SET_IDENTIFIER.match(identifier) or _SET_DIFFERENCE.match(identifier):
        return None
    parts = [part.strip() for part in _split_top(modifiers) if part.strip()]
    return identifier, parts


def _angle_closing(content: str, opening: int) -> int:
    """The `>` closing the modifiers opened at `opening`, or -1."""
    depth = 0
    quote = None
    for index in range(opening + 1, len(content)):
        char = content[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "[":
            quote = "]"
        elif char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        elif char == ">" and depth == 0:
            return index
    return -1


def _modifier_fields(parts: Iterable[str]) -> List[str]:
    names = []
    for part in parts:
        match = _FIELD_IN_MODIFIER.match(part)
        if match:
            name = match.group(1)
            names.append(name[1:-1] if name.startswith("[") else name)
    return names


def merge_sets(modifier: str, owns: List[str]) -> str:
    """The filter and the expression's own set, as one set.

    Refused rather than guessed: two sets of the expression's own, a set
    joined to another by an operator, two different identifiers, and a
    field both name - one condition would silently replace the other.
    """
    if not owns:
        return modifier
    if len(owns) > 1:
        raise FilterPlacementError(
            f"{' '.join(owns)} are {len(owns)} sets in one place; Qlik reads "
            f"one of them and drops the rest without saying so. Write them "
            f"as one set.")
    stated = _parse_set(modifier)
    if stated is None:
        raise FilterPlacementError(
            "The filters make a combination of sets, which cannot be merged "
            "into a set of the expression's own. Put {filter} where no set "
            "of the expression's own stands.")
    own = _parse_set(owns[0])
    if own is None:
        raise FilterPlacementError(
            f"{owns[0]} joins sets with an operator, and the filter cannot be "
            f"merged into it faithfully. Write the filter's conditions into "
            f"that set yourself, or put {{filter}} in front of the whole "
            f"expression.")
    filter_id, filter_parts = stated
    own_id, own_parts = own
    if filter_id and own_id and filter_id != own_id:
        raise FilterPlacementError(
            f"The filters count over {{{filter_id}}} and {owns[0]} over "
            f"{{{own_id}}}; one set cannot say both.")
    # The same condition written twice is one condition: the filter's copy
    # goes, the expression keeps its own.
    own_conditions = {_condition_of(part) for part in own_parts} - {None}
    filter_parts = [part for part in filter_parts
                    if _condition_of(part) not in own_conditions]
    both = sorted(set(_modifier_fields(filter_parts))
                  & set(_modifier_fields(own_parts)))
    if both:
        names = ", ".join(f"[{name}]" for name in both)
        raise FilterPlacementError(
            f"{owns[0]} sets {names} itself and the filters name {names} "
            f"too; one condition would replace the other. Name each field "
            f"once, in `filters` or in the expression.")
    identifier = own_id or filter_id
    parts = filter_parts + own_parts
    return "{" + identifier + ("<" + ",".join(parts) + ">" if parts else "") + "}"


def _set_parts(written: str) -> Optional[Tuple[str, List[str]]]:
    """(identifier, modifiers) of one set, ("", []) of none, None of what
    is not one simple set."""
    text = (written or "").strip()
    if not text:
        return "", []
    return _parse_set(text)


def holds_filter(context: Tuple[str, str], modifier: str) -> bool:
    """Whether the filter's conditions are written into this context,
    in front of the whole expression or at the start of the aggregation.
    A condition inside P() narrows another set, not this aggregation."""
    stated = _set_parts(modifier)
    if not stated or not stated[1]:
        return False
    written = set()
    for side in context:
        parts = _set_parts(side)
        if parts:
            written.update(parts[1])
    return set(stated[1]) <= written


def flat_set(context: Tuple[str, str]) -> Optional[Tuple[str, List[str]]]:
    """The one set an aggregation reads through, as Qlik composes it.

    The set at the start of the aggregation inherits the outer one, its
    own condition winning on a field both name; a set with an identifier
    inherits nothing. None when either side is not one simple set.
    """
    outer, inner = (_set_parts(side) for side in context)
    if outer is None or inner is None:
        return None
    if inner[0]:
        return inner
    own = set(_modifier_fields(inner[1]))
    kept = [part for part in outer[1]
            if not (set(_modifier_fields([part])) & own)]
    return outer[0], kept + inner[1]


def _prefix_run(text: str, start: int) -> Dict[str, Any]:
    """The sets and keywords at `start`, before what they apply to.

    Items are (kind, start, end, marker rest): kind is `set`, `marker` or
    `keyword`; `TOTAL <A, B>` is one keyword.
    """
    items: List[Tuple[str, int, int, str]] = []
    index = start
    end = start
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        if text[index] == "{":
            closing = _closing(text, index)
            if closing < 0:
                break
            rest = _marker_rest(text[index + 1:closing])
            items.append(("marker" if rest is not None else "set",
                          index, closing + 1, rest or ""))
            index = end = closing + 1
            continue
        match = _PREFIX_KEYWORD.match(text, index)
        if not match:
            break
        stop = match.end()
        if match.group(1).upper() == "TOTAL":
            after = stop
            while after < len(text) and text[after].isspace():
                after += 1
            if after < len(text) and text[after] == "<":
                closing = text.find(">", after)
                if closing > 0:
                    stop = closing + 1
        items.append(("keyword", index, stop, ""))
        index = end = stop
    return {"start": start, "end": end, "items": items}


def _runs(text: str) -> List[Dict[str, Any]]:
    """Every run of sets: at the start of the text, and after every
    opening parenthesis, with the function it opens."""
    runs = []
    first = _prefix_run(text, 0)
    if first["items"]:
        first.update(outer=True, function="", inside_set=False)
        runs.append(first)
    quote = None
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "(":
            run = _prefix_run(text, index + 1)
            if run["items"]:
                name = re.search(r"([\w.#$]+)\s*$", text[:index])
                run.update(outer=False,
                           function=(name.group(1) if name else ""),
                           inside_set=depth > 0)
                runs.append(run)
    return runs


def element_set_warning(name: str, expression: str,
                        modifier: str) -> Optional[str]:
    """A warning when the filter stops short of P() or E() in the measure.

    `{<Client=P({<Sport={'A'}>} Client)>}` picks the clients from a set of
    its own, which the filter written around it does not reach: the
    clients are chosen over every date while the sum is over the period.
    A model read that as the filter not applying.
    """
    body = _body(expression)
    missed = 0
    for run in _runs(body):
        if run["function"].upper() not in ("P", "E") or not run.get("inside_set"):
            continue
        written = " ".join(body[s:e] for kind, s, e, _ in run["items"]
                           if kind == "set")
        if not holds_filter(("", written), modifier):
            missed += 1
    # P(Field) with no set at all: a run needs an item, so count those too.
    missed += len(re.findall(r"(?<![\w.])[PE]\s*\(\s*(?!\{)[^)]", body))
    if not missed:
        return None
    return (f"Measure {name!r} picks values with P() or E() over sets of "
            f"their own ({missed}), and the filters do not reach inside "
            f"them: those values are chosen without the filters. Put "
            f"{{filter}} in those sets too if they should share them.")


def set_contexts(expression: str) -> List[Tuple[str, str]]:
    """The sets each aggregation of the expression reads through.

    One (outer, inner) pair per aggregation: `outer` is the set in front of
    the whole expression, `inner` the set at the start of the aggregation.
    Asking `{outer} Min({inner} Field)` answers which values of the field
    that aggregation really read - the text is Qlik's, so whatever Qlik
    does with it, the answer does too.
    """
    body = _body(expression)
    runs = _runs(body)
    outer = ""
    if runs and runs[0].get("outer"):
        outer = " ".join(body[s:e] for kind, s, e, _ in runs[0]["items"]
                         if kind == "set")
    found: List[Tuple[str, str]] = []
    with_sets = 0
    for run in runs:
        if (run.get("outer") or run.get("inside_set")
                or run["function"].lower() not in AGGREGATIONS):
            continue
        inner = " ".join(body[s:e] for kind, s, e, _ in run["items"]
                         if kind == "set")
        if not inner:
            continue
        with_sets += 1
        if (outer, inner) not in found:
            found.append((outer, inner))
    # An aggregation with no set of its own reads through the outer set
    # alone, and so does a master measure named by its label.
    calls = len(re.findall(
        r"(?i)(?<![\w.])(" + "|".join(sorted(AGGREGATIONS)) + r")\s*\(", body))
    if (calls > with_sets or not found) and (outer, "") not in found:
        found.append((outer, ""))
    return found


def _closing(text: str, start: int) -> int:
    """Index of the brace closing the one at `start`, or -1."""
    depth = 0
    quote = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _split_top(text: str) -> List[str]:
    """Split on commas that are not inside braces, brackets, parentheses or
    quotes."""
    parts, depth, quote, current = [], 0, None, []
    for char in text:
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "[":
            quote = "]"
        elif char in "{(":
            depth += 1
        elif char in "})":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def set_expressions(expression: str) -> List[Tuple[str, str]]:
    """Every set expression in the text, as (identifier, modifiers).

    Strings and bracketed names are skipped; element lists inside a set
    belong to it and are not reported on their own.
    """
    text = expression or ""
    found: List[Tuple[str, str]] = []
    index = 0
    quote = None
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "{":
            end = _closing(text, index)
            if end < 0:
                break
            content = text[index + 1:end].strip()
            rest = _marker_rest(content)
            if rest == "":
                # The marker itself: where the filter goes, not a set.
                index = end + 1
                continue
            if rest:
                # `{filter <A={1}>}`: the set written after the marker.
                content = rest
            if content.startswith("&"):
                content = content[1:].lstrip()
            opening = _first_angle(content)
            if opening < 0:
                found.append((content, ""))
            else:
                modifiers = content[opening + 1:].rstrip()
                if modifiers.endswith(">"):
                    modifiers = modifiers[:-1]
                found.append((content[:opening].strip(), modifiers))
            index = end
        index += 1
    return found


def _first_angle(content: str) -> int:
    """The `<` that opens the modifiers, outside quotes and brackets."""
    quote = None
    for index, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "<":
            return index
    return -1


def filter_limits(expression: str) -> Dict[str, Any]:
    """What keeps an outer set from reaching this expression.

    `fields` - fields the expression sets itself; `own_set` - identifiers
    of sets that ignore an outer one; `computed_first` - the expression
    holds `$(=...)`. Keys are present only when they say something.
    """
    fields: List[str] = []
    identifiers: List[str] = []
    for identifier, modifiers in set_expressions(expression):
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
        for part in _split_top(modifiers) if modifiers else []:
            match = _FIELD_IN_MODIFIER.match(part)
            if not match:
                continue
            name = match.group(1)
            if name.startswith("[") and name.endswith("]"):
                name = name[1:-1]
            if name.startswith("$("):
                continue
            if name not in fields:
                fields.append(name)
    limits: Dict[str, Any] = {}
    if fields:
        limits["fields"] = sorted(fields)
    if identifiers:
        limits["own_set"] = identifiers
    if _COMPUTED_FIRST.search(expression or ""):
        limits["computed_first"] = True
    return limits


def _condition_of(part: str) -> Optional[Tuple[str, str, frozenset]]:
    """(field, operator, element set) of one plain modifier `F={a,b}`."""
    match = re.match(r"^\s*(\[[^\]]+\]|[^\s=+\-*/<>\[\]]+)\s*([+\-*/]?=)\s*\{(.*)\}\s*$",
                     part, re.DOTALL)
    if not match:
        return None
    name, operator, elements = match.groups()
    name = name[1:-1] if name.startswith("[") else name
    values = frozenset(value.strip().strip("'\"").strip()
                       for value in _split_top(elements) if value.strip())
    return name, operator, values


def repeated_conditions(expression: str, modifier: str) -> List[str]:
    """Fields the expression sets itself to exactly what the filter sets.

    Such a filter changes nothing: the measure's own condition and the
    filter select the same values. Only a different condition is one that
    wins over the filter.
    """
    stated = _set_parts(modifier)
    if not stated:
        return []
    wanted: Dict[str, Any] = {}
    for part in stated[1]:
        condition = _condition_of(part)
        if condition:
            wanted[condition[0]] = condition
    own: Dict[str, List[Any]] = {}
    for _identifier, modifiers in set_expressions(expression):
        for part in _split_top(modifiers) if modifiers else []:
            condition = _condition_of(part)
            if condition:
                own.setdefault(condition[0], []).append(condition)
            else:
                for name in _modifier_fields([part]):
                    own.setdefault(name, []).append(None)
    return sorted(name for name, conditions in own.items()
                  if name in wanted and all(c == wanted[name] for c in conditions))


def repeated_notes(name: str, fields: Iterable[str]) -> List[str]:
    return [f"Measure {name!r} sets [{field}] itself to the same values as "
            f"the filter on [{field}], which therefore adds nothing to it."
            for field in fields]


def limit_warnings(name: str, limits: Dict[str, Any],
                   filter_fields: Iterable[str],
                   repeated: Iterable[str] = ()) -> List[str]:
    """What the caller must know before reading the filtered figure.

    `repeated` - fields the expression sets to the filter's own values: a
    repeat, not a conflict, and said as a note by the caller.
    """
    repeated = set(repeated)
    filter_fields = [field for field in filter_fields if field not in repeated]
    warnings: List[str] = []
    if limits.get("own_set"):
        sets = ", ".join("{" + ident + "<...>}" for ident in limits["own_set"])
        warnings.append(
            f"Measure {name!r} counts over its own set {sets}, which does not "
            f"inherit a set written in front of it: the filters do not reach "
            f"that part of it.")
    overlap = [field for field in filter_fields
               if field in (limits.get("fields") or [])]
    for field in dict.fromkeys(overlap):
        warnings.append(
            f"Measure {name!r} sets [{field}] itself, and its own condition "
            f"wins over the filter on [{field}]: that filter does not narrow "
            f"it.")
    if limits.get("computed_first"):
        warnings.append(
            f"Measure {name!r} holds $(=...), which Qlik computes once before "
            f"the cube and without the filters: that part is not filtered.")
    return warnings


def master_reference(name: str, expression: str,
                     field_names: Optional[Iterable[str]] = None) -> str:
    """How a master measure is named in an expression.

    By its label, so the library decides what it computes; by its own
    expression in parentheses when the label cannot be written in
    brackets or is also the name of a field, which the label would then
    mean.
    """
    label = (name or "").strip()
    if (not label or "]" in label or "[" in label
            or label in set(field_names or ())):
        return f"({_body(expression)})"
    return f"[{label}]"
