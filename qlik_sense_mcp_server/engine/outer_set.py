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

Nothing here talks to Qlik: the measure is read, not evaluated.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Where a described filter goes inside an expression the caller wrote.
FILTER_MARKER = "{filter}"

_FIELD_IN_MODIFIER = re.compile(
    r"^\s*(?:[^\s:\[\]]+::)?(\[[^\]]+\]|[^\s=+\-*/<>\[\]]+)\s*[+\-*/]?=")
_COMPUTED_FIRST = re.compile(r"\$\(\s*=")


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


def marks_outer(expression: str) -> bool:
    """Whether the filter marker stands in front of the whole expression."""
    return _body(expression).startswith(FILTER_MARKER)


def in_front(modifier: str, expression: str) -> str:
    """The expression with the filter as its outer set.

    An expression that already starts with an outer set of its own keeps
    it: the chain is applied left to right, so the author's set comes
    second and wins on any field both name. Measured: the period of a
    month followed by a set naming its first half answered the half.
    """
    return f"{outer_modifier(modifier)} {_body(expression)}"


def place_filter(expression: str, modifier: str) -> str:
    """Every marker replaced: the leading one as an outer set, the rest
    inside the aggregations that carry them."""
    if marks_outer(expression):
        rest = _body(expression)[len(FILTER_MARKER):].lstrip()
        return in_front(modifier, rest.replace(FILTER_MARKER, modifier))
    return expression.replace(FILTER_MARKER, modifier)


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
            if content == FILTER_MARKER[1:-1]:
                # The marker itself: where the filter goes, not a set.
                index = end + 1
                continue
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


def limit_warnings(name: str, limits: Dict[str, Any],
                   filter_fields: Iterable[str]) -> List[str]:
    """What the caller must know before reading the filtered figure."""
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
