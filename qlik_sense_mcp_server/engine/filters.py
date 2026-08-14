"""Filters described by the caller, written as set analysis by the server.

A caller states what it wants filtered — a field and a period, or a field
and a list of values. This module turns that into a set modifier Qlik
actually honours, and proves it does before the query runs.

The proof is necessary. Measured: filtering a date field:

    {<[F]={">=40542<40908"}>}                  0 on a timestamp field,
                                               correct on a numeric one
    {<[F]={">=30.10.2010<01.01.2011"}>}        0 on both
    {<[F]={"=[F]>=40542 and [F]<40908"}>}      correct on both
    {<[F]={">=$(=Date(40542))"}>}              0 on both

Comparison inside a set modifier runs against the text Qlik displays for
the value, so the numeric form works only where that text is itself a
number. The expression form always works and costs about sixty times more
(480ms against 7ms on a field with 295k values). Neither is chosen by
assumption: the cheap form is measured against a reference count, and used
only where it agrees.
"""

import datetime
import decimal
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ..exceptions import QlikEngineError, QlikProbeUnavailable
from ..utils import bare_field_name, escape_qlik_field_name

logger = logging.getLogger(__name__)

# Day zero of Qlik's date serial numbers.
_EPOCH = datetime.date(1899, 12, 30)

# How a caller may write a period bound. Anything else is refused with the
# list of accepted forms rather than guessed at.
_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DOTTED_DAY = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")

BOUND_FORMS = ("2024-01-31", "31.01.2024", "2024-01 (whole month)",
               "2024 (whole year)", "45292 (Qlik date serial number)")


def _to_serial(day: datetime.date) -> int:
    return (day - _EPOCH).days


def _last_day_of_month(year: int, month: int) -> datetime.date:
    return (datetime.date(year + month // 12, month % 12 + 1, 1)
            - datetime.timedelta(days=1))


def _parse_bound(value: Any, upper: bool) -> Optional[datetime.date]:
    """One period bound as a calendar date, or None if unrecognised.

    A year or a month names a span, and which end of it is meant depends on
    the side: `from: "2024"` is the first of January, `to: "2024"` the
    thirty-first of December. Getting this wrong loses a whole year's tail,
    which is exactly the mistake that returns a plausible number.
    """
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # A date written as 20240101 lands somewhere around the year 57000,
        # which is past what a date can hold. That is a bound this server
        # cannot read, and it belongs with the rest of them.
        try:
            return _EPOCH + datetime.timedelta(days=int(value))
        except (OverflowError, ValueError, OSError):
            return None
    text = str(value or "").strip()
    if not text:
        return None

    # A bound that looks like a date but is not one — 2024-02-30, month 13,
    # 31.04 — is a bound this server cannot read, not an internal failure.
    # Returning None sends it to the same refusal as "last tuesday", which
    # names the forms that are accepted.
    try:
        match = _ISO_DAY.match(text)
        if match:
            year, month, day = (int(g) for g in match.groups())
            return datetime.date(year, month, day)
        match = _DOTTED_DAY.match(text)
        if match:
            day, month, year = (int(g) for g in match.groups())
            return datetime.date(year, month, day)
        match = _ISO_MONTH.match(text)
        if match:
            year, month = (int(g) for g in match.groups())
            return (_last_day_of_month(year, month) if upper
                    else datetime.date(year, month, 1))
        match = _YEAR.match(text)
        if match:
            year = int(match.group(1))
            return (datetime.date(year, 12, 31) if upper
                    else datetime.date(year, 1, 1))
        return _EPOCH + datetime.timedelta(days=int(float(text)))
    except (TypeError, ValueError, OverflowError):
        return None


# What one filter value may be, what all of them together may be, how many
# there may be, and how deep a description may nest. A modifier is built
# from all of it and read by Qlik on the connection every query shares.
MAX_VALUE_CHARS = 4096
MAX_FILTER_CHARS = 20000
MAX_FILTER_VALUES = 1000
# How many pieces a description may hold at all, whatever they are.
MAX_FILTER_PIECES = 20000
MAX_FILTER_DEPTH = 20


VALUE_KEYS = ("values", "exclude", "add", "intersect")


def _modifier_too_long(modifier: str) -> Optional[Dict[str, Any]]:
    """The refusal a built modifier earns for its length, or nothing.

    Measured after building, because quoting makes a value longer than it
    was written, and measured before any probe runs on it.
    """
    if len(modifier) <= MAX_FILTER_CHARS:
        return None
    return {"error": (
        f"The filters build a modifier of {len(modifier)} characters, over "
        f"the {MAX_FILTER_CHARS} this server sends."),
        "error_category": "limit_exceeded",
        "hint": ("Quoting makes a value longer than it was written; ask "
                 "for fewer values at a time.")}


def _weigh(value: Any, depth: int = 0, tally: Optional[Dict[str, Any]] = None,
           counting: bool = False) -> Dict[str, Any]:
    """How much text, how many values and how deep a description carries.

    Stops at the first excess: walking a description of arbitrary size to
    find out that it is too large costs exactly what the ceilings exist to
    prevent. Every member counts, in a list or in an object.

    Inside `values` a container is one value and is measured as Qlik will
    see it - the text of the container - because that is what goes into
    the modifier.
    """
    if tally is None:
        tally = {"chars": 0, "values": 0, "pieces": 0, "too_deep": False,
                 "too_many": False, "longest": 0, "done": False}
    if tally["done"]:
        return tally
    if depth > MAX_FILTER_DEPTH:
        tally["too_deep"] = True
        tally["done"] = True
        return tally

    if counting == "item" and isinstance(value, (list, tuple, dict)):
        # One value, whatever shape it has.
        text = str(value)
        tally["values"] += 1
        tally["chars"] += len(text)
        tally["longest"] = max(tally["longest"], len(text))
        if (tally["values"] > MAX_FILTER_VALUES
                or tally["chars"] > MAX_FILTER_CHARS
                or tally["longest"] > MAX_VALUE_CHARS):
            tally["done"] = True
        return tally

    if isinstance(value, (dict, list, tuple)):
        members = (list(value.items()) if isinstance(value, dict)
                   else [(None, item) for item in value])
        for key, inner in members:
            tally["pieces"] += 1
            if tally["pieces"] > MAX_FILTER_PIECES:
                tally["too_many"] = True
                tally["done"] = True
                return tally
            if key is not None:
                tally["chars"] += len(str(key))
            # The list under `values` is a list of values; each member of
            # it is one value, container or not.
            if key in VALUE_KEYS if key is not None else False:
                inside = "list"
            elif counting == "list":
                inside = "item"
            else:
                inside = counting
            _weigh(inner, depth + 1, tally, inside)
            if tally["done"]:
                return tally
        return tally

    text = "" if value is None else str(value)
    tally["chars"] += len(text)
    tally["longest"] = max(tally["longest"], len(text))
    if counting in ("item", "list"):
        tally["values"] += 1
    if (tally["longest"] > MAX_VALUE_CHARS
            or tally["chars"] > MAX_FILTER_CHARS
            or tally["values"] > MAX_FILTER_VALUES):
        tally["done"] = True
    return tally


def _too_much(description: Any) -> Optional[Dict[str, Any]]:
    """The refusal a filter description earns for its size, or nothing."""
    weight = _weigh(description)
    if weight["too_deep"]:
        return {"error": (
            f"A filter nests deeper than {MAX_FILTER_DEPTH} levels."),
            "error_category": "limit_exceeded",
            "hint": ("Conditions inside conditions end somewhere; this one "
                     "does not.")}
    if weight.get("too_many"):
        return {"error": (
            f"A filter holds more than {MAX_FILTER_PIECES} pieces."),
            "error_category": "limit_exceeded",
            "hint": "Every piece is read, whatever it holds."}
    if weight["longest"] > MAX_VALUE_CHARS:
        return {"error": (
            f"A filter carries a value of {weight['longest']} characters, "
            f"longer than the {MAX_VALUE_CHARS} this server sends."),
            "error_category": "limit_exceeded",
            "hint": "Field values and expressions are short by nature."}
    if weight["values"] > MAX_FILTER_VALUES:
        return {"error": (
            f"The filters name more than {MAX_FILTER_VALUES} values, over "
            f"what this server checks in one call."),
            "error_category": "limit_exceeded",
            "hint": "Every value is checked against the field before use."}
    if weight["chars"] > MAX_FILTER_CHARS:
        return {"error": (
            f"The filters together carry more than {MAX_FILTER_CHARS} "
            f"characters, over what this server sends."),
            "error_category": "limit_exceeded",
            "hint": ("Many short values build one long modifier; ask for "
                     "fewer at a time.")}
    return None


def _exactly_held(value: Any) -> bool:
    """Whether the bound Qlik receives is the bound that was asked for.

    Not a size limit: 2**54 is written exactly, while 2**53 + 1 is not.
    The question is only whether what gets written back matches what was
    read - a bound that shifts takes in a neighbouring row, and the answer
    looks entirely reasonable.
    """
    if isinstance(value, bool) or value is None:
        return True
    # An integer is measured too: the writing goes through a float, so
    # 9007199254740993 comes back one less than it went in.
    text = value.strip() if isinstance(value, str) else value
    try:
        asked = decimal.Decimal(str(text))
    except (decimal.InvalidOperation, ValueError, TypeError):
        # Not a number at all; the rest of the reading refuses it by name.
        return True
    written = _plain_number(text)
    if written is None:
        return True
    try:
        return decimal.Decimal(written) == asked
    except decimal.InvalidOperation:
        return True


def _plain_number(value: Any) -> Optional[str]:
    """A bound as Qlik reads it: a plain number, no thousands separators.

    `400.0` is written `400`, because a search string is compared as text
    and a trailing `.0` is text Qlik has no value for. Infinity and
    not-a-number are not bounds Qlik can compare against, so they come
    back as None and are refused with everything else it cannot read.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        # An integer past what a double holds raises rather than rounds;
        # either way it is not a bound Qlik can compare against.
        return None
    if not math.isfinite(number):
        return None
    return str(int(number)) if number == int(number) else repr(number)


def quote_value(value: Any) -> str:
    """One literal value inside a set modifier.

    Single quotes make it an exact value rather than a search: a double
    quoted string is a search expression, where `>` and `*` carry meaning
    and a value containing either would filter something else. A quote
    inside the value is doubled, Qlik's own escape.
    """
    return "'" + str(value).replace("'", "''") + "'"


# What a filter does to the set of values, and the Qlik operator for it.
# Measured on four rows worth 110 in total: `=` on one value gave 20,
# `-=` on one value gave 60, and two conditions together gave 30.
SET_OPERATORS = {
    "values": "=",       # exactly these
    "exclude": "-=",     # everything except these
    "add": "+=",         # these as well as what is selected
    "intersect": "*=",   # only those of these that are selected
}


# Which set the filters narrow. Without one, Qlik reads the modifier
# against the current selection, which is what a question about "the data"
# normally means.
SET_COMBINERS = {
    "union": " + ",
    "intersect": " * ",
    "exclude": " - ",
    "symmetric_difference": " / ",
}

SCOPE_KEYS = ("ignore_selections", "current_selection", "bookmark", "state",
              "selection_back", "selection_forward", "combine", "of")


def _probe_complaint(result: Dict[str, Any]) -> str:
    """What Qlik said about a probe, when what it said was a complaint.

    A probe that comes back with no number has two very different causes.
    Qlik answers a broken expression with the text of the error, and that
    is a refusal to pass on. A probe that never ran at all - a dropped
    frame, a timeout - says nothing about the query and must not be read
    as one.
    """
    text = str(result.get("text") or "")
    if text.startswith("Error"):
        return text.split("Error:", 1)[-1].strip() or text
    return ""


def _unproven(result: Dict[str, Any], what: str) -> Optional[str]:
    """A note when a probe answered neither a number nor a complaint.

    The filter still goes out - a dropped frame is not the caller's
    mistake - but the reply must not read like one Qlik confirmed.
    """
    if not result or result.get("number") is not None:
        return None
    if _probe_complaint(result):
        return None
    return f"{what} could not be checked: Qlik did not answer the probe."


def _probe_unusable(result: Dict[str, Any]) -> str:
    """Whether a probe says nothing that can be built on.

    Wider than a complaint: a call that failed outright reports through
    `error` rather than through the text of a value, and a form of a filter
    must not be chosen on the strength of an answer that never came.
    """
    complaint = _probe_complaint(result)
    if complaint:
        return complaint
    stated = result.get("error")
    return str(stated) if stated else ""


def _set_identifier(scope: Dict[str, Any]) -> Dict[str, Any]:
    """The identifier that goes before the modifier, from a description.

    Returns {"identifier": str} or an error. One key at a time, except a
    bookmark belonging to a state, which needs both — a bookmark holds the
    selections of every state at once, so naming the state says which part
    of it to read.
    """
    # A combination is read elsewhere, by the code that builds the sets it
    # joins; here it is neither an identifier nor two of them at once.
    if scope.get("combine") is not None or scope.get("of") is not None:
        return {"identifier": ""}
    unknown = [key for key in scope if key not in SCOPE_KEYS]
    if unknown:
        return {
            "error": f"scope states {', '.join(sorted(unknown))}, which it "
                     f"does not read.",
            "error_category": "invalid_argument",
            "hint": "It reads: " + ", ".join(SCOPE_KEYS) + ".",
        }
    named = [key for key in SCOPE_KEYS
             if scope.get(key) is not None and scope.get(key) is not False]
    if not named:
        return {"identifier": ""}
    pair = set(named) == {"state", "bookmark"}
    if len(named) > 1 and not pair:
        return {
            "error": f"scope states {' and '.join(named)} at once.",
            "error_category": "invalid_argument",
            "hint": ("One of them, or `state` together with `bookmark` for a "
                     "bookmark belonging to a state."),
        }
    if pair:
        blank = [key for key in ("state", "bookmark")
                 if not str(scope.get(key) or "").strip()]
        if blank:
            return {"error": f"scope names an empty {blank[0]}.",
                    "error_category": "invalid_argument"}
        return {"identifier": f"{str(scope['state']).strip()}::"
                              f"{str(scope['bookmark']).strip()}"}
    key = named[0]
    value = scope[key]
    if key in ("ignore_selections", "current_selection"):
        if not isinstance(value, bool):
            return {"error": f"scope {key}={value!r} is not yes or no.",
                    "error_category": "invalid_argument",
                    "hint": "true or false, not the word for it."}
        marker = "1" if key == "ignore_selections" else "$"
        return {"identifier": marker} if value else {"identifier": ""}
    if key in ("bookmark", "state"):
        name = str(value or "").strip()
        if not name:
            return {"error": f"scope names an empty {key}.",
                    "error_category": "invalid_argument"}
        return {"identifier": name}
    # Steps through the selection history: `$1` back, `$_1` forward. Two
    # keys rather than one signed number, because Qlik spells them with
    # different characters rather than different signs.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return {"error": f"scope {key}={value!r} is not a number of steps.",
                "error_category": "invalid_argument",
                "hint": "1 is one step, 2 is two, and so on."}
    return {"identifier": f"${value}" if key == "selection_back"
            else f"$_{value}"}


class EngineFiltersMixin:
    """Build set modifiers from plain descriptions, and prove they filter."""

    # How long a measured filter form is reused. A field's form follows how
    # Qlik displays its values, which a reload can change — a script that
    # starts formatting a bare number as a date turns the cheap numeric
    # form into the wrong one. A form that selects nothing is measured
    # again on the spot, but one that selects a wrong non-zero count would
    # not be, so the memory expires rather than lasting the life of a
    # process that is deliberately long-lived.
    FILTER_FORM_TTL_SECONDS = 600.0

    def _filter_form_store(self) -> Dict[Tuple[str, str], Tuple[str, float]]:
        store = self.__dict__.get("_filter_forms")
        if store is None:
            store = {}
            self.__dict__["_filter_forms"] = store
        return store

    def _remembered_form(self, app_id: str, field: str) -> Optional[str]:
        entry = self._filter_form_store().get((app_id, field))
        if not entry:
            return None
        form, measured_at = entry
        if (time.monotonic() - measured_at) > self.FILTER_FORM_TTL_SECONDS:
            return None
        return form

    def _remember_form(self, app_id: str, field: str, form: str) -> None:
        self._filter_form_store()[(app_id, field)] = (form, time.monotonic())

    def forget_filter_forms(self, app_id: str = None) -> None:
        """Drop the remembered forms for one app, or for all of them."""
        store = self._filter_form_store()
        if app_id is None:
            store.clear()
            return
        for key in [k for k in store if k[0] == app_id]:
            store.pop(key, None)

    def _is_temporal_field(self, app_handle: int, field: str) -> bool:
        """Does this field hold dates, according to Qlik's own tags?

        What `from` and `to` mean depends on the answer. On a date field
        they are days; on any other field they are the values themselves.
        Reading `{"field": "discount", "from": 400}` as a date turned "more
        than 400 off" into "some time in 1901" and answered 18,774 where
        the truth was 1,898,591 — a plausible number, and wrong.
        """
        try:
            description = self.get_field_description(app_handle, field) or {}
        except Exception:
            # The wrapper is documented not to raise; a subclass that does
            # is telling us the same thing an empty answer does - ask
            # again, directly.
            description = {}
        if not description:
            # The wrapper answers `{}` to both "Qlik says there is no such
            # field" and "the question never arrived", and here the
            # difference decides whether a period stays a period. Asked
            # again, directly, so the two can be told apart.
            try:
                reply = self.send_request("GetFieldDescription", [field],
                                          handle=app_handle)
                description = {"tags": ((reply or {}).get("qReturn")
                                        or {}).get("qTags", [])}
            except QlikEngineError as exc:
                logger.debug("No description for %r: %s", field, exc)
                return False
            except Exception as exc:
                # Reading silence as "not a date" turns a period into a
                # numeric range and answers a plausible wrong number - the
                # very failure this check exists to stop.
                raise QlikProbeUnavailable(
                    f"Qlik could not be asked whether "
                    f"{escape_qlik_field_name(field)} holds dates: {exc}"
                ) from exc
        tags = description.get("tags") or []
        return any(str(tag).lstrip("$") in ("date", "timestamp")
                   for tag in tags)

    def range_modifier(self, app_handle: int, app_id: str, field: str,
                       low: Any, high: Any, low_exclusive: bool = False,
                       high_exclusive: bool = False,
                       base: str = "") -> Dict[str, Any]:
        """A set modifier selecting a range of a field that is not a date.

        The bounds are the values themselves, and by default both ends are
        included: "discount from 400 to 500" is `>=400<=500`. "More than
        400" is a different question — `greater_than` excludes the bound,
        and the difference is every row sitting exactly on it. Measured on
        a 10M-row app: 184 orders have a discount of exactly 400, which is
        the whole gap between a right answer and a wrong one.
        """
        stated = [v for v in (low, high) if v is not None]
        written = [_plain_number(v) for v in stated
                   if not isinstance(v, bool)]
        if len(written) != len(stated) or any(w is None for w in written):
            return {
                "error": (
                    f"Bounds {low!r}..{high!r} on {field!r} are neither dates "
                    f"nor numbers."
                ),
                "error_category": "invalid_filter",
                "accepted_forms": list(BOUND_FORMS) + ["400", "400.5"],
            }
        name = escape_qlik_field_name(field)
        for bound in (low, high):
            if bound is not None and not _exactly_held(bound):
                return {"error": (
                    f"Bound {bound!r} on {escape_qlik_field_name(field)} "
                    f"shifts to {_plain_number(bound)} when written."),
                    "error_category": "invalid_filter",
                    "hint": ("The filter would take in a neighbouring "
                             "value. Not every large number shifts - "
                             "18014398509481984 is written exactly.")}
        parts = []
        if low is not None:
            parts.append(f"{'>' if low_exclusive else '>='}{_plain_number(low)}")
        if high is not None:
            parts.append(f"{'<' if high_exclusive else '<='}{_plain_number(high)}")
        if not parts:
            return {"error": f"Filter on {field!r} states no bound.",
                    "error_category": "invalid_filter"}
        modifier = f'{name}={{"{"".join(parts)}"}}'
        counted = self.evaluate_expressions(
            app_handle, [f"=Count({{{base}<{modifier}>}} DISTINCT {name})"])
        complaint = _probe_complaint(counted[0]) if counted else ""
        if complaint:
            return {"error": (
                f"Qlik cannot read the bounds on "
                f"{escape_qlik_field_name(field)}: {complaint}"),
                "error_category": "invalid_filter"}
        matched = counted[0].get("number") if counted else None
        if matched is not None and int(matched) == 0:
            return {
                "error": (
                    f"No value of {field!r} falls in "
                    f"{_plain_number(low) if low is not None else '-inf'}.."
                    f"{_plain_number(high) if high is not None else '+inf'}."
                ),
                "error_category": "empty_range",
                "next_actions": [
                    f"read the range with engine_get_field_range on {field!r}",
                    "then ask for bounds inside it",
                ],
            }
        note = _unproven(counted[0] if counted else {},
                         f"Whether any value of "
                         f"{escape_qlik_field_name(field)} falls in the "
                         f"range")
        outcome = {
            "modifier": modifier, "field": field,
            "from": low, "to": high,
            "from_excluded": low_exclusive, "to_excluded": high_exclusive,
            "distinct_values_in_range": int(matched) if matched is not None else None,
        }
        if note:
            outcome["note"] = note
        return outcome

    def period_modifier(self, app_handle: int, app_id: str, field: str,
                        start: Any, end: Any,
                        base: str = "") -> Dict[str, Any]:
        """A set modifier selecting one period of a date field.

        Returns {"modifier", "form", "from", "to", "days", "error", ...}.
        `days` is how many distinct values of the field fall in the period,
        counted by Qlik while the form was being chosen — a period that
        selects nothing is refused here rather than answered with a zero.
        """
        low = _parse_bound(start, upper=False)
        high = _parse_bound(end if end is not None else start, upper=True)
        if low is None or high is None:
            return {
                "error": (
                    f"Period bounds {start!r}..{end!r} on {field!r} are not a "
                    f"date this server reads."
                ),
                "error_category": "invalid_period",
                "accepted_forms": list(BOUND_FORMS),
            }
        if high < low:
            return {
                "error": (
                    f"Period on {escape_qlik_field_name(field)} starts at "
                    f"{low.isoformat()} and ends at {high.isoformat()}, which "
                    f"is earlier."
                ),
                "error_category": "invalid_period",
                "hint": "Swap `from` and `to` if that is what was meant.",
            }

        # The upper bound is exclusive at the next day. A date field that
        # carries a time of day holds 31.12.2024 23:59 as a value larger
        # than 31.12.2024, so `<=` on the last day drops it — and the answer
        # is short by one day with nothing to show for it.
        serial_from = _to_serial(low)
        serial_to = _to_serial(high) + 1

        form = self._resolve_period_form(
            app_handle, app_id, field, serial_from, serial_to, base=base)
        if form.get("error"):
            return form

        return {
            **({"note": form["note"]} if form.get("note") else {}),
            "modifier": form["modifier"],
            "form": form["form"],
            "field": field,
            "from": low.isoformat(),
            "to": high.isoformat(),
            "serial_from": serial_from,
            "serial_to_exclusive": serial_to,
            "distinct_values_in_period": form["matched"],
        }

    def _period_forms(self, field: str, serial_from: int,
                      serial_to: int) -> List[Tuple[str, str]]:
        """Candidate modifiers for one period, cheapest first."""
        name = escape_qlik_field_name(field)
        return [
            ("numeric", f'{name}={{">={serial_from}<{serial_to}"}}'),
            ("expression",
             f'{name}={{"={name}>={serial_from} and {name}<{serial_to}"}}'),
        ]

    def _resolve_period_form(self, app_handle: int, app_id: str, field: str,
                             serial_from: int, serial_to: int,
                             base: str = "") -> Dict[str, Any]:
        """Pick the working form by measuring, not by inspecting the field.

        The reference is `Count(DISTINCT If(...))`, which compares numbers
        and therefore cannot be fooled by how the field is displayed. A
        candidate is accepted only when it counts the same values. Both
        counts go out in one pipelined batch.
        """
        name = escape_qlik_field_name(field)
        remembered = self._remembered_form(app_id, field)
        candidates = self._period_forms(field, serial_from, serial_to)
        if remembered:
            # The form was measured against the reference once and holds for
            # this field until the app reloads. Only the count is still
            # worth having — it is the control value the reply carries.
            chosen = [c for c in candidates if c[0] == remembered]
            if chosen:
                label, modifier = chosen[0]
                counted = self.evaluate_expressions(
                    app_handle,
                    [f"=Count({{{base}<{modifier}>}} DISTINCT {name})"])
                complaint = _probe_complaint(counted[0]) if counted else ""
                if complaint:
                    return {"error": (
                        f"Qlik cannot read the period on "
                        f"{escape_qlik_field_name(field)}: {complaint}"),
                        "error_category": "invalid_period"}
                matched = counted[0].get("number") if counted else None
                if matched is not None and int(matched) > 0:
                    return {"modifier": modifier, "form": label,
                            "matched": int(matched)}
                # Nothing selected: fall through and measure again rather
                # than answer a zero built on a remembered choice.

        reference_expr = (
            f"=Count({{{base}}} DISTINCT If({name}>={serial_from} and "
            f"{name}<{serial_to}, {name}))" if base else
            f"=Count(DISTINCT If({name}>={serial_from} and {name}<{serial_to}, "
            f"{name}))")
        probes = [reference_expr] + [
            f"=Count({{{base}<{modifier}>}} DISTINCT {name})"
            for _, modifier in candidates
        ]
        values = self.evaluate_expressions(app_handle, probes)
        # A complaint is a refusal: Qlik answering the reference with the
        # text of an error says the bounds cannot be read at all, and
        # picking a form over that answer hides it.
        complaint = _probe_unusable(values[0]) if values else ""
        if complaint:
            # The reference is what every candidate form is measured
            # against; without it a form would be chosen on nothing.
            return {"error": (
                f"Qlik cannot read the period on "
                f"{escape_qlik_field_name(field)}: {complaint}"),
                "error_category": "invalid_period"}
        if not values or values[0].get("number") is None:
            # Qlik answered, but not with a count. Every candidate form is
            # measured against this number, so without it there is nothing
            # to choose on.
            return {"error": (
                f"The period on {escape_qlik_field_name(field)} could not "
                f"be measured: Qlik returned no count for it."),
                "error_category": "invalid_period",
                "hint": ("Ask again; a filter form chosen without this "
                         "number would go out unproven.")}

        reference = int(values[0]["number"])
        if reference == 0:
            return {
                "error": (
                    f"No value of {field!r} falls between "
                    f"{(_EPOCH + datetime.timedelta(days=serial_from)).isoformat()} "
                    f"and "
                    f"{(_EPOCH + datetime.timedelta(days=serial_to - 1)).isoformat()}."
                ),
                "error_category": "empty_period",
                "next_actions": [
                    f"read the loaded period with engine_get_field_range "
                    f"on {field!r}",
                    "then ask for a period inside it",
                ],
            }

        complaints = []
        for (label, modifier), value in zip(candidates, values[1:]):
            complaint = _probe_unusable(value)
            if complaint:
                complaints.append(complaint)
                continue
            number = value.get("number")
            if number is not None and int(number) == reference:
                self._remember_form(app_id, field, label)
                return {"modifier": modifier, "form": label,
                        "matched": reference}

        if len(complaints) == len(candidates):
            # Qlik refused every form there is. Falling back to one of them
            # would send out a filter Qlik has already called unreadable.
            return {"error": (
                f"Qlik cannot read any form of a period filter on "
                f"{escape_qlik_field_name(field)}: {complaints[0]}"),
                "error_category": "invalid_period"}

        # Every candidate disagreed with the reference. The one that goes
        # out is built from the same comparison the reference uses - but
        # never one Qlik has already refused, even when another form only
        # disagreed.
        usable = [(label, modifier)
                  for (label, modifier), value in zip(candidates, values[1:])
                  if not _probe_unusable(value)
                  and value.get("number") is not None]
        if not usable:
            return {"error": (
                f"No form of a period filter on "
                f"{escape_qlik_field_name(field)} could be measured"
                + (f": {complaints[0]}" if complaints else
                   ": Qlik answered none of them with a count.")),
                "error_category": "invalid_period"}
        label, modifier = usable[-1]
        unproven_note = (
            f"Which form of a period filter on "
            f"{escape_qlik_field_name(field)} Qlik reads could not be "
            f"measured: no candidate agreed with the reference count."
        )
        logger.warning(
            "No filter form matched the reference count on %r (reference=%d)",
            field, reference)
        return {"modifier": modifier, "form": label, "matched": reference,
                "note": unproven_note}

    def values_modifier(self, app_handle: int, field: str,
                        values: List[Any], operator: str = "values",
                        base: str = "") -> Dict[str, Any]:
        """A set modifier over named values of a field.

        `operator` says what the values do to the set: keep exactly these,
        drop these, add these, or intersect with these.

        Each value is checked against the field before the query runs. Qlik
        answers a filter on a value that does not exist with zeros, so
        `Moscow` against a field holding `Moskva` produces a clean, wrong
        table; here it produces a refusal naming the values that are
        missing.
        """
        wanted = list(values or [])
        if not wanted:
            return {
                "error": (
                    f"Filter on {escape_qlik_field_name(field)} lists no "
                    f"values."
                ),
                "error_category": "invalid_filter",
            }
        empty = [position for position, value in enumerate(wanted)
                 if value is None or str(value) == ""]
        if empty:
            return {
                "error": (
                    f"Filter on {escape_qlik_field_name(field)} has an empty "
                    f"value at position {empty[0]}."
                ),
                "error_category": "invalid_filter",
                "hint": ("Every value is matched against the field; an empty "
                         "one matches nothing and would narrow the result to "
                         "nothing."),
            }
        name = escape_qlik_field_name(field)
        probes = [
            f"=Count({{{base}<{name}={{{quote_value(v)}}}>}} DISTINCT {name})"
            for v in wanted
        ]
        counts = self.evaluate_expressions(app_handle, probes)
        for result in counts:
            complaint = _probe_complaint(result)
            if complaint:
                return {"error": (
                    f"Qlik cannot read the filter on "
                    f"{escape_qlik_field_name(field)}: {complaint}"),
                    "error_category": "invalid_filter"}
        unproven = [value for value, result in zip(wanted, counts)
                    if result.get("number") is None]
        missing = [
            value for value, result in zip(wanted, counts)
            if result.get("number") is not None and int(result["number"]) == 0
        ]
        # Only for the operators that keep what they name. Excluding a value
        # the field does not hold changes nothing and is not a mistake; the
        # same goes for adding to a selection.
        if missing and operator in ("values", "intersect"):
            return {
                "error": (
                    f"Field {escape_qlik_field_name(field)} holds none of "
                    f"these values: "
                    + ", ".join(repr(v) for v in missing)
                ),
                "error_category": "value_not_found",
                "unknown_values": missing,
                "next_actions": [
                    f"read the values with get_app_field on "
                    f"{escape_qlik_field_name(field)}",
                    "copy a value exactly as Qlik stores it",
                ],
            }
        listed = ",".join(quote_value(v) for v in wanted)
        sign = SET_OPERATORS[operator]
        outcome = {"modifier": f"{name}{sign}{{{listed}}}", "field": field,
                   "values": wanted}
        if operator != "values":
            outcome["operator"] = operator
        if unproven:
            # The filter still goes out - a dropped frame is not the
            # caller's mistake - but calling it checked would be a
            # statement about data nobody looked at.
            outcome["unverified_values"] = unproven
            outcome["note"] = (
                f"Whether {escape_qlik_field_name(field)} holds "
                + ", ".join(repr(v) for v in unproven)
                + " could not be checked: Qlik did not answer the probe."
            )
        return outcome

    def _element_set(self, app_handle: int, app_id: str, field: str,
                     entry: Dict[str, Any],
                     outer_set: str = "") -> Dict[str, Any]:
        """Values of a field that satisfy a condition on another field.

        This is what P() and E() are for, and nothing else expresses it: a
        set modifier takes literal values, and "the customers who bought in
        2023" is not a literal — it is the answer to another question.

        `matching` keeps the values a condition makes possible, `not_matching`
        drops the ones it makes possible. Both together read as "these and
        not those", which is one condition on one field rather than two
        filters that would have to be combined by guesswork.

        `of_field` carries the answer from one field to another: the
        suppliers of a product, applied to customers. `base` says whether
        the condition is asked of the whole model or of what is selected;
        the whole model by default, since a question asked through this
        server has no user sitting in front of a selection.
        """
        # With both stated, the second is subtracted from the first as
        # another P: "possible under a, minus possible under b". Written as
        # P(a) - E(b) it would read "possible under a, minus excluded under
        # b", which is a different set and only coincides on some data.
        both = (entry.get("matching") is not None
                and entry.get("not_matching") is not None)
        pieces = []
        inner_notes: List[str] = []
        for key, function in (("matching", "P"),
                              ("not_matching", "P" if both else "E")):
            wanted = entry.get(key)
            if wanted is None:
                continue
            if not isinstance(wanted, dict):
                return {"error": (
                    f"{key} on {escape_qlik_field_name(field)} must be an "
                    f"object holding `filters`."),
                    "error_category": "invalid_filter"}
            inner_filters = wanted.get("filters")
            if not isinstance(inner_filters, list) or not inner_filters:
                return {"error": (
                    f"{key} on {escape_qlik_field_name(field)} states no "
                    f"filters."),
                    "error_category": "invalid_filter",
                    "hint": ('{"matching": {"filters": [{"field": "Year", '
                             '"values": [2023]}]}}')}
            base = str(wanted.get("base") or "all").strip().lower()
            if base not in ("all", "current"):
                return {"error": (
                    f"base={wanted.get('base')!r} is neither 'all' nor "
                    f"'current'."),
                    "error_category": "invalid_filter"}
            inner = self.build_filters(
                app_handle, app_id, inner_filters,
                scope={"ignore_selections": True} if base == "all" else None)
            if inner.get("error"):
                return inner
            # What the inner filters could not prove, the element set
            # cannot prove either: a period whose form went unmeasured
            # inside `matching` picks its rows just as blindly.
            inner_notes.extend(applied["note"]
                               for applied in inner.get("applied") or []
                               if applied.get("note"))
            of_field = bare_field_name(str(wanted.get("of_field") or field))
            if "[" in of_field or "]" in of_field:
                return {"error": (
                    f"of_field [{of_field}] carries a bracket."),
                    "error_category": "invalid_filter"}
            # Checked like the field it will restrict: an unknown name here
            # would be scored as an expression and quietly select nothing.
            if of_field != field:
                try:
                    described = self.send_request(
                        "GetFieldDescription", [of_field], handle=app_handle)
                    known = bool((described.get("qReturn") or {}).get("qName"))
                except QlikEngineError:
                    known = False
                except Exception:
                    known = True
                if not known:
                    return {"error": (
                        f"of_field names a field this app does not have: "
                        f"{escape_qlik_field_name(of_field)}"),
                        "error_category": "field_not_found"}
            pieces.append(
                f"{function}({inner['modifier']} "
                f"{escape_qlik_field_name(of_field)})")

        if not pieces:
            return {"error": "no element set stated",
                    "error_category": "invalid_filter"}
        # P(a) - P(b) rather than P(a) * E(b): the two are equivalent, and
        # the difference is the one Qlik's own examples are written with.
        joined = " - ".join(pieces) if len(pieces) > 1 else pieces[0]
        # No braces around it: an element set is assigned to the field
        # directly, `Customer = P({...} Customer)`, the way Qlik's own
        # examples are written. Wrapped in braces it does not parse.
        name = escape_qlik_field_name(field)
        modifier = f"{name}={joined}"
        refusal = _modifier_too_long(modifier)
        if refusal:
            return refusal
        # Proven to select something, like every other kind of condition.
        # "The clients who bought in a year nobody bought in" narrows the
        # field to nothing, and an empty answer reads as "no such data"
        # rather than as "no such client".
        counted = self.evaluate_expressions(
            app_handle,
            [f"=Count({{{outer_set}<{modifier}>}} DISTINCT {name})"])
        complaint = _probe_complaint(counted[0]) if counted else ""
        if complaint:
            return {"error": (
                f"Qlik cannot read the condition on {name}: {complaint}"),
                "error_category": "invalid_filter"}
        matched = counted[0].get("number") if counted else None
        if matched is not None and int(matched) == 0:
            return {"error": (
                f"No value of {name} satisfies the condition stated for "
                f"it."),
                "error_category": "value_not_found",
                "next_actions": [
                    f"read the values with get_app_field on {name}",
                    "check the filters inside `matching` select something",
                ]}
        outcome = {"modifier": modifier, "field": field,
                   "element_set": joined}
        if matched is not None:
            outcome["matched"] = int(matched)
        note = _unproven(counted[0] if counted else {},
                         f"Whether any value of {name} satisfies the "
                         f"condition stated for it")
        notes = ([note] if note else []) + inner_notes
        if notes:
            outcome["note"] = " ".join(notes)
        return outcome

    def pattern_modifier(self, app_handle: int, field: str, kind: str,
                         value: Any, base: str = "") -> Dict[str, Any]:
        """A modifier matching values by their text.

        Not written as a Qlik wildcard search. There is no escape for `*`,
        `?` or a quote inside one, so a value that happens to contain them
        would silently become a different search. Written instead as a
        comparison of strings, where the value is a quoted literal and the
        same doubling that protects an exact value protects this one.
        """
        text = str(value)
        if not text:
            return {"error": (
                f"{kind} on {escape_qlik_field_name(field)} states no text."),
                "error_category": "invalid_filter"}
        name = escape_qlik_field_name(field)
        literal = quote_value(text)
        upper_field = f"Upper({name})"
        upper_value = f"Upper({literal})"
        if kind == "contains":
            condition = f"Index({upper_field}, {upper_value})>0"
        elif kind == "starts_with":
            condition = f"Index({upper_field}, {upper_value})=1"
        else:
            condition = (f"Upper(Right({name}, Len({literal})))={upper_value}")
        modifier = f'{name}={{"={condition}"}}'
        # Proven to select something, the way every other filter is: a
        # search matching no value is answered by Qlik with an empty table
        # rather than with a word about it, and an empty table reads as
        # "there is no such data" instead of "there is no such spelling".
        counted = self.evaluate_expressions(
            app_handle, [f"=Count({{{base}<{modifier}>}} DISTINCT {name})"])
        complaint = _probe_complaint(counted[0]) if counted else ""
        if complaint:
            return {"error": (
                f"Qlik cannot read the {kind} filter on "
                f"{escape_qlik_field_name(field)}: {complaint}"),
                "error_category": "invalid_filter"}
        matched = counted[0].get("number") if counted else None
        if matched is not None and int(matched) == 0:
            return {
                "error": (
                    f"No value of {escape_qlik_field_name(field)} "
                    f"{kind.replace('_', ' ')} {text!r}."),
                "error_category": "value_not_found",
                "next_actions": [
                    f"read the values with get_app_field on "
                    f"{escape_qlik_field_name(field)}",
                ],
            }
        outcome = {"modifier": modifier, "field": field, kind: text}
        if matched is not None:
            outcome["matched"] = int(matched)
        note = _unproven(counted[0] if counted else {},
                         f"Whether any value of "
                         f"{escape_qlik_field_name(field)} "
                         f"{kind.replace('_', ' ')} {text!r}")
        if note:
            outcome["note"] = note
        return outcome

    def _combined_sets(self, app_handle: int, app_id: str,
                       filters: List[Dict[str, Any]],
                       scope: Dict[str, Any]) -> Dict[str, Any]:
        """Two or more sets, joined by one operation between them.

        Each set is described the way any set is - an identifier and
        filters of its own - and the operation says what to do with them:
        everything in either (`union`), only what is in both (`intersect`),
        the first without the second (`exclude`), or what belongs to
        exactly one of them (`symmetric_difference`).

        Measured on the server: two sets holding 40 and 60 answer 100 under
        `union`, 40 under `intersect` where one contains the other, and 0
        under `exclude` and `symmetric_difference` in the same case.
        """
        # Weighed whole: each set is measured again as it is built, and
        # measuring only the parts let a combination carry any number of
        # them past the ceiling.
        refusal = _too_much([filters, scope])
        if refusal:
            return refusal

        # A key stated beside the combination is still a key: accepting
        # `{"combine": ..., "of": ..., "bookmark": "BM"}` dropped the
        # bookmark silently and answered over a different set.
        beside = [key for key in scope if key not in ("combine", "of")]
        if beside:
            named = ("a combination" if scope.get("combine") is not None
                     else "a list of sets")
            return {
                "error": (f"scope states {named} and "
                          + ", ".join(sorted(beside)) + " at once."),
                "error_category": "invalid_argument",
                "hint": ("Put the key inside the entry of `of` it belongs "
                         "to - each set of a combination states its own."),
            }

        operation = scope.get("combine")
        parts = scope.get("of")
        if operation is None or parts is None:
            return {"error": ("scope names one of `combine` and `of` without "
                              "the other."),
                    "error_category": "invalid_argument",
                    "hint": ('{"combine": "union", "of": [{...}, {...}]} - '
                             "the operation and the sets it joins.")}
        operation = str(operation).strip().lower()
        if operation not in SET_COMBINERS:
            return {"error": f"combine={operation!r} is not an operation "
                             f"between sets.",
                    "error_category": "invalid_argument",
                    "allowed_values": sorted(SET_COMBINERS)}
        if not isinstance(parts, (list, tuple)) or len(parts) < 2:
            return {"error": "`of` holds fewer than two sets.",
                    "error_category": "invalid_argument",
                    "hint": "An operation between sets needs two of them."}
        if operation == "symmetric_difference" and len(parts) > 2:
            # Qlik applies it pairwise, so three sets answer "in an odd
            # number of them" rather than "in exactly one of them". Saying
            # that plainly beats answering a different question.
            return {"error": ("symmetric_difference joins exactly two sets, "
                              f"and `of` holds {len(parts)}."),
                    "error_category": "invalid_argument",
                    "hint": ("Qlik applies it in pairs, so three sets would "
                             "answer \"in an odd number of them\" instead.")}
        if filters:
            # Measured: Qlik refuses a modifier written outside the
            # combination - `{((A) + (B))<Field={'x'}>}` comes back as
            # "'}' expected". Narrowing each set separately is the same
            # question and Qlik reads it.
            return {"error": ("Filters cannot narrow a combination of sets "
                              "from the outside."),
                    "error_category": "invalid_filter",
                    "hint": ("State them inside each set of `of` - Qlik "
                             "reads no modifier written around a "
                             "combination.")}

        written, applied = [], []
        for position, part in enumerate(parts):
            if not isinstance(part, dict):
                return {"error": f"of[{position}] is not an object: {part!r}",
                        "error_category": "invalid_argument"}
            own_filters = part.get("filters")
            if own_filters is not None and not isinstance(own_filters,
                                                          (list, tuple)):
                return {"error": (
                    f"of[{position}].filters={own_filters!r} is not a list."),
                    "error_category": "invalid_filter",
                    "hint": "A list of filter objects, or nothing at all."}
            own_filters = list(own_filters or [])
            own_scope = {k: v for k, v in part.items() if k != "filters"}
            built = self.build_filters(app_handle, app_id, own_filters,
                                       scope=own_scope or None)
            if built.get("error"):
                return built
            modifier = built.get("modifier") or "{$}"
            # The outermost braces, not every brace: a bookmark named with
            # one would lose part of its name to `strip`.
            inner = modifier[1:-1] if (modifier.startswith("{")
                                       and modifier.endswith("}")) else modifier
            written.append("(" + inner + ")")
            applied.append({"set": position, "modifier": modifier,
                            "filters_applied": built.get("applied", [])})

        combined = "{" + SET_COMBINERS[operation].join(written) + "}"
        refusal = _modifier_too_long(combined)
        if refusal:
            return refusal
        return {"modifier": combined, "applied": applied,
                "scope": f"{operation} of {len(written)} sets"}

    def build_filters(self, app_handle: int, app_id: str,
                      filters: List[Dict[str, Any]],
                      scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Turn a list of filter descriptions into one set modifier.

        Every filter narrows the result further — they combine with AND,
        which is what "revenue in 2024 for the North region" means.
        """
        # Sets combine with each other: "bought in 2023 or lives in the
        # South" is the union of two sets, and no modifier on one field
        # says it.
        if isinstance(scope, dict) and (scope.get("combine") is not None
                                        or scope.get("of") is not None):
            return self._combined_sets(app_handle, app_id, filters, scope)

        identifier = ""
        # `is not None`, not truthiness: 0, "" and False are not objects,
        # and passing them silently as "no scope" hides a mistake in the
        # request behind a number counted over the current selections.
        if scope is not None:
            if not isinstance(scope, dict):
                return {"error": f"scope must be an object, got {scope!r}.",
                        "error_category": "invalid_argument"}
            resolved = _set_identifier(scope)
            if resolved.get("error"):
                return resolved
            identifier = resolved["identifier"]

        # Measured here rather than in one caller: both tools build their
        # filters through this, and a value of a few megabytes holds the
        # shared connection for as long as Qlik takes to read it. The scope
        # is weighed with them - a bookmark name goes into the same
        # modifier.
        refusal = _too_much([filters, scope])
        if refusal:
            return refusal

        parts: List[str] = []
        applied: List[Dict[str, Any]] = []
        for entry in filters or []:
            if not isinstance(entry, dict):
                return {
                    "error": f"A filter must be an object, got {entry!r}.",
                    "error_category": "invalid_filter",
                    "hint": ('{"field": "OrderDate", "from": "2024-01-01", '
                             '"to": "2024-12-31"} for a period, '
                             '{"field": "Region", "values": ["North"]} for '
                             'named values.'),
                }
            field = bare_field_name(str(entry.get("field") or ""))
            if not field:
                return {
                    "error": f"Filter {entry!r} names no field.",
                    "error_category": "invalid_filter",
                }
            if "[" in field or "]" in field:
                # The name is written into a set modifier, and Qlik has no
                # escape for a bracket inside one. A name carrying one
                # cannot be written unambiguously, so it is refused rather
                # than sent as a broken modifier Qlik would quietly drop.
                return {
                    "error": (
                        f"Field name [{field}] carries a bracket, which "
                        f"cannot be written into a filter unambiguously."
                    ),
                    "error_category": "invalid_filter",
                }
            # Does the app have this field at all? Asked before the bounds
            # are read, because a missing field has no tags, reads as "not
            # a date", and sends perfectly good dates into a numeric parse
            # that then blames the bounds: "2026-08-01 is neither a date
            # nor a number" for a filter whose only fault is the name.
            # An empty description means Engine does not know the field —
            # but `get_field_description` also answers empty when the call
            # itself failed, and a dropped frame must not be reported as a
            # missing field. Asked directly here so the two are told apart.
            try:
                described = self.send_request(
                    "GetFieldDescription", [field], handle=app_handle)
                exists = bool((described.get("qReturn") or {}).get("qName"))
            except QlikEngineError:
                # Engine considered the call and refused it: for a field it
                # does not have, it answers "Invalid parameters". That is
                # an answer.
                exists = False
            except Exception as exc:
                # The question never got through. The filter goes on
                # unchecked rather than being refused on the strength of a
                # failed question; a wrong name still fails later, loudly.
                logger.debug("Could not describe %r: %s", field, exc)
                exists = True
            if not exists:
                return {
                    "error": (
                        f"Filter names a field this app does not have: "
                        f"{escape_qlik_field_name(field)}"
                    ),
                    "error_category": "field_not_found",
                    "next_actions": [
                        "read the field names with get_app_details",
                        "field names are case-sensitive; copy them exactly",
                    ],
                }
            # Which of the set operators this filter uses, if any. More
            # than one at a time is a contradiction, not a combination.
            bounds_named = [key for key in ("from", "to", "greater_than",
                                            "less_than")
                            if entry.get(key) is not None]
            if entry.get("period") is not None and bounds_named:
                return {
                    "error": (
                        f"Filter on {escape_qlik_field_name(field)} states a "
                        f"period and " + ", ".join(sorted(bounds_named))
                        + " at once."),
                    "error_category": "invalid_filter",
                    "hint": ("A period names both ends, and a bound beside "
                             "it replaces one of them. State one or the "
                             "other."),
                }

            # One filter, one kind of condition. Stating several is a
            # contradiction rather than a combination, and answering the
            # first one silently drops the rest — with a plausible number
            # to show for it.
            kinds = {
                "an element set": [k for k in ("matching", "not_matching")
                                   if entry.get(k) is not None],
                "text": [k for k in ("contains", "starts_with", "ends_with")
                         if entry.get(k) is not None],
                "an expression": (["match_expression"]
                                  if entry.get("match_expression") is not None
                                  else []),
                "values": [k for k in SET_OPERATORS if entry.get(k) is not None],
                "a range": [k for k in ("from", "to", "period", "greater_than",
                                        "less_than")
                            if entry.get(k) is not None],
            }
            stated = [name for name, keys in kinds.items() if keys]
            if len(stated) > 1:
                named = sorted(key for keys in kinds.values() for key in keys)
                return {
                    "error": (
                        f"Filter on {escape_qlik_field_name(field)} states "
                        f"{' and '.join(stated)} at once: "
                        + ", ".join(named)
                    ),
                    "error_category": "invalid_filter",
                    "hint": ("One condition per filter. Use several filters "
                             "on the same field to combine them."),
                }
            if len(kinds["text"]) > 1 or len(kinds["values"]) > 1:
                named = sorted(kinds["text"] + kinds["values"])
                return {
                    "error": (
                        f"Filter on {escape_qlik_field_name(field)} states "
                        + " and ".join(named) + " at once."
                    ),
                    "error_category": "invalid_filter",
                }

            has_period = bool(kinds["a range"])
            if entry.get("matching") is not None or entry.get(
                    "not_matching") is not None:
                outcome = self._element_set(app_handle, app_id, field, entry,
                                            outer_set=identifier)
                if outcome.get("error"):
                    return outcome
                parts.append(outcome["modifier"])
                applied.append({k: v for k, v in outcome.items()
                                if k != "modifier"})
                continue

            # Matching by text rather than by exact value.
            pattern = kinds["text"]
            if pattern:
                outcome = self.pattern_modifier(
                    app_handle, field, pattern[0], entry[pattern[0]],
                    base=identifier)
                if outcome.get("error"):
                    return outcome
                parts.append(outcome["modifier"])
                applied.append({k: v for k, v in outcome.items()
                                if k != "modifier"})
                continue

            # A condition the vocabulary cannot state, written by the
            # caller. The server wraps it and asks Qlik whether it holds;
            # it does not read it.
            if entry.get("match_expression") is not None:
                condition = str(entry["match_expression"]).strip()
                if not condition:
                    return {"error": (
                        f"match_expression on "
                        f"{escape_qlik_field_name(field)} is empty."),
                        "error_category": "invalid_filter"}
                # Whether the condition is well formed is Qlik's verdict,
                # not a count of characters here: a quote or a brace inside
                # a string literal is text, and counting them refused
                # conditions Qlik reads without complaint. Measured: inside
                # a set modifier Qlik reads a broken condition as text and
                # answers zero, so the condition is checked on its own,
                # where Qlik does say what is wrong with it.
                # The same two questions the measures go through, asked
                # by the same code: a variable is expanded first, because
                # the name that reaches Qlik is the expanded one, and both
                # halves of the verdict are read - the parse error and the
                # names the model does not have.
                expanded = self.expand_expressions(
                    app_handle, [condition]).get(condition, condition)
                fault = self.check_expressions(
                    app_handle, [expanded]).get(expanded) or {}
                if fault.get("error"):
                    return {"error": (
                        f"match_expression on "
                        f"{escape_qlik_field_name(field)} is not an "
                        f"expression Qlik reads: {fault['error']}"),
                        "error_category": "invalid_expression",
                        "hint": ("It is scored for each value of the field, "
                                 "so it reads like a measure: "
                                 "Sum([Amount]) > 1000.")}
                if fault.get("bad_fields"):
                    return {"error": (
                        f"match_expression on "
                        f"{escape_qlik_field_name(field)} names a field this "
                        f"app does not have: "
                        + ", ".join(escape_qlik_field_name(name)
                                    for name in fault["bad_fields"])),
                        "error_category": "field_not_found",
                        "next_actions": [
                            "call get_app_details(app_id) and read "
                            "`fields[].name`",
                            "field names are case-sensitive; copy them "
                            "exactly",
                        ]}
                name = escape_qlik_field_name(field)
                modifier = f'{name}={{"={condition}"}}'
                counted = self.evaluate_expressions(
                    app_handle,
                    [f"=Count({{{identifier}<{modifier}>}} DISTINCT {name})"])
                complaint = _probe_complaint(counted[0]) if counted else ""
                if complaint:
                    # A double quote inside the condition closes the search
                    # early: measured, Qlik answers the whole modifier with
                    # "Error in set modifier ad hoc element list". The
                    # condition alone reads fine, so only the built
                    # modifier shows it.
                    return {"error": (
                        f"Qlik cannot read match_expression on {name}: "
                        f"{complaint}"),
                        "error_category": "invalid_expression"}
                matched = counted[0].get("number") if counted else None
                if matched is not None and int(matched) == 0:
                    return {"error": (
                        f"No value of {name} satisfies "
                        f"{condition!r}."),
                        "error_category": "value_not_found",
                        "next_actions": [
                            f"read the values with get_app_field on {name}",
                        ]}
                parts.append(modifier)
                record = {"field": field, "match_expression": condition}
                if matched is not None:
                    record["matched"] = int(matched)
                note = _unproven(counted[0] if counted else {},
                                 f"Whether any value of {name} satisfies "
                                 f"{condition!r}")
                if note:
                    record["note"] = note
                applied.append(record)
                continue

            operator = kinds["values"][0] if kinds["values"] else "values"
            values = entry.get(operator)
            if has_period and values is not None:
                return {
                    "error": (
                        f"Filter on {field!r} asks for a period and a value "
                        f"list at once."
                    ),
                    "error_category": "invalid_filter",
                    "hint": "Use two filters, one for each.",
                }
            if has_period:
                period = entry.get("period")
                # `greater_than` and `less_than` exclude the bound they
                # name; `from` and `to` include it. "More than 400" and
                # "from 400" are different questions, and the rows sitting
                # exactly on the bound are the difference.
                if (entry.get("from") is not None
                        and entry.get("greater_than") is not None):
                    return {
                        "error": (
                            f"Filter on {escape_qlik_field_name(field)} states "
                            f"both `from` and `greater_than`."
                        ),
                        "error_category": "invalid_filter",
                        "hint": ("`from` includes the bound, `greater_than` "
                                 "excludes it. State one."),
                    }
                if (entry.get("to") is not None
                        and entry.get("less_than") is not None):
                    return {
                        "error": (
                            f"Filter on {escape_qlik_field_name(field)} states "
                            f"both `to` and `less_than`."
                        ),
                        "error_category": "invalid_filter",
                        "hint": ("`to` includes the bound, `less_than` "
                                 "excludes it. State one."),
                    }
                # The first of these that holds something. A key set to
                # null states nothing, here as everywhere else.
                low = next((entry[k] for k in ("from", "greater_than")
                            if entry.get(k) is not None), period)
                high = next((entry[k] for k in ("to", "less_than")
                             if entry.get(k) is not None), period)
                low_exclusive = (entry.get("greater_than") is not None
                                 and entry.get("from") is None)
                high_exclusive = (entry.get("less_than") is not None
                                  and entry.get("to") is None)
                # The same two keys mean days on a date field and values on
                # any other. Asking Qlik which this is costs one cheap call
                # and is the difference between "more than 400 off" and
                # "some time in 1901".
                try:
                    temporal = self._is_temporal_field(app_handle, field)
                except QlikProbeUnavailable as exc:
                    return {"error": str(exc),
                            "error_category": "engine_api_error",
                            "hint": ("Ask again; without this answer a "
                                     "period and a numeric range cannot be "
                                     "told apart.")}
                if temporal:
                    outcome = self.period_modifier(
                        app_handle, app_id, field, low, high, base=identifier)
                else:
                    outcome = self.range_modifier(
                        app_handle, app_id, field,
                        low if low is not None else None,
                        high if high is not None else None,
                        low_exclusive=low_exclusive,
                        high_exclusive=high_exclusive, base=identifier)
            elif values is not None:
                outcome = self.values_modifier(
                    app_handle, field,
                    values if isinstance(values, list) else [values],
                    operator=operator, base=identifier)
            else:
                return {
                    "error": (
                        f"Filter on {field!r} says neither a period "
                        f"(`from`/`to`) nor `values`."
                    ),
                    "error_category": "invalid_filter",
                }
            if outcome.get("error"):
                return outcome
            parts.append(outcome["modifier"])
            applied.append({k: v for k, v in outcome.items() if k != "modifier"})

        if not parts:
            # An identifier with nothing to modify is still a set: "all the
            # records" or "what this bookmark holds".
            if identifier:
                return {"modifier": "{" + identifier + "}", "applied": [],
                        "scope": identifier}
            return {"modifier": "", "applied": []}
        modifier = "{" + identifier + "<" + ",".join(parts) + ">}"
        refusal = _modifier_too_long(modifier)
        if refusal:
            return refusal
        outcome = {"modifier": modifier, "applied": applied}
        if identifier:
            outcome["scope"] = identifier
        return outcome
