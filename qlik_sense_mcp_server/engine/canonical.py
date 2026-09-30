"""One text for one question, and Engine's error codes in words.

Engine keeps one result cache for every user of an app, and a result is
found again only for an expression written character for character the
same way. `sum( Amount )` and `Sum(Amount)` are the same question and two
entries. Expressions the server sends go out in one written form: known
functions in one case, whitespace collapsed, no space inside parentheses,
and `Today()`/`Now()` - which make every expression new each time they are
evaluated - replaced by the day they stand for.

Engine answers a heavy question with a number code. The codes a query runs
into are named here with what to change.
"""

import datetime
import re
from typing import Any, Optional

# Qlik functions an expression written by hand most often carries, in the
# case Qlik's documentation writes them.
_FUNCTIONS = (
    "Sum", "Count", "Avg", "Min", "Max", "Only", "Median", "Stdev",
    "Fractile", "Aggr", "If", "RangeSum", "RangeAvg", "RangeMax", "RangeMin",
    "Rank", "Above", "Below", "Num", "Text", "Date", "Year", "Month", "Day",
    "MonthStart", "MonthEnd", "YearStart", "YearEnd", "AddMonths", "Round",
    "Floor", "Ceil", "FirstSortedValue", "Concat", "MaxString", "MinString",
    "Alt", "IsNull", "Null", "Dual", "Match", "WildMatch", "Pick", "Class",
    "NullCount", "MissingCount", "NumericCount", "TextCount", "Mode",
)
_FUNCTION_CASE = {name.lower(): name for name in _FUNCTIONS}
_FUNCTION_CALL = re.compile(r"\b([A-Za-z]+)\s*\(")
_TODAY = re.compile(r"\bToday\s*\(\s*\d?\s*\)", re.IGNORECASE)
_NOW = re.compile(r"\bNow\s*\(\s*\d?\s*\)", re.IGNORECASE)


def _pieces(text: str):
    """(is_literal, piece): strings and bracketed names are literal."""
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        closing = {"'": "'", '"': '"', "[": "]"}.get(char)
        if closing:
            end = text.find(closing, index + 1)
            end = len(text) - 1 if end < 0 else end
            if index > start:
                yield False, text[start:index]
            yield True, text[index:end + 1]
            start = index = end + 1
            continue
        index += 1
    if start < len(text):
        yield False, text[start:]


def _code(piece: str, today: datetime.datetime, flatten: bool) -> str:
    piece = _TODAY.sub(
        f"MakeDate({today.year}, {today.month}, {today.day})", piece)
    piece = _NOW.sub(
        f"Timestamp(MakeDate({today.year}, {today.month}, {today.day}) "
        f"+ MakeTime({today.hour}, {today.minute}))", piece)
    piece = _FUNCTION_CALL.sub(
        lambda m: _FUNCTION_CASE.get(m.group(1).lower(), m.group(1)) + "(",
        piece)
    if flatten:
        piece = re.sub(r"\s+", " ", piece)
        piece = re.sub(r"\(\s+", "(", piece)
        piece = re.sub(r"\s+\)", ")", piece)
    return piece


def canonical_expression(expression: Any,
                         now: Optional[datetime.datetime] = None) -> str:
    """The one written form of an expression; literals are left alone.

    An expression holding a comment keeps its line breaks - flattening
    them would comment out the rest of it.
    """
    text = str(expression or "").strip()
    if not text:
        return text
    now = now or datetime.datetime.now()
    pieces = list(_pieces(text))
    code = "".join(piece for literal, piece in pieces if not literal)
    flatten = "//" not in code and "/*" not in code
    return "".join(piece if literal else _code(piece, now, flatten)
                   for literal, piece in pieces).strip()


# Engine error codes a query runs into, and what to change.
ENGINE_ERROR_MEANINGS = {
    7004: ("Engine code 7004: the calculation hit Engine's time limit. "
           "Narrow the period or the filters, or group by fewer or coarser "
           "fields."),
    7009: ("Engine code 7009: more than 10000 cells on one page. Ask for "
           "fewer rows or columns per page and read on with `offset`."),
    30100: ("Engine code 30100: the generated set expression is too long. "
            "Filter on fewer values, or on a field that says the same with "
            "fewer of them."),
    19: ("Engine code 19: Engine ran out of memory. Narrow the period or "
         "the filters and group by fewer fields."),
    6: ("Engine code 6: Engine ran out of memory. Narrow the period or the "
        "filters and group by fewer fields."),
    7002: ("Engine code 7002: Engine ran out of memory for this "
           "calculation. Narrow the period or the filters and group by "
           "fewer fields."),
}
_ERROR_CODE = re.compile(r"""['"]?(?:code|qErrorCode)['"]?\s*:\s*(\d+)""")


def explain_engine_error(message: Any) -> str:
    """The message with the meaning of the Engine code in it, if known."""
    text = str(message)
    match = _ERROR_CODE.search(text)
    if not match:
        return text
    meaning = ENGINE_ERROR_MEANINGS.get(int(match.group(1)))
    if not meaning or meaning in text:
        return text
    return f"{text} - {meaning}"
