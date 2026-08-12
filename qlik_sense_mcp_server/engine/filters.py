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
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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
        return _EPOCH + datetime.timedelta(days=int(value))
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


def quote_value(value: Any) -> str:
    """One literal value inside a set modifier.

    Single quotes make it an exact value rather than a search: a double
    quoted string is a search expression, where `>` and `*` carry meaning
    and a value containing either would filter something else. A quote
    inside the value is doubled, Qlik's own escape.
    """
    return "'" + str(value).replace("'", "''") + "'"


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

    def period_modifier(self, app_handle: int, app_id: str, field: str,
                        start: Any, end: Any) -> Dict[str, Any]:
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
            low, high = high, low

        # The upper bound is exclusive at the next day. A date field that
        # carries a time of day holds 31.12.2024 23:59 as a value larger
        # than 31.12.2024, so `<=` on the last day drops it — and the answer
        # is short by one day with nothing to show for it.
        serial_from = _to_serial(low)
        serial_to = _to_serial(high) + 1

        form = self._resolve_period_form(
            app_handle, app_id, field, serial_from, serial_to)
        if form.get("error"):
            return form

        return {
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
        name = f"[{field}]"
        return [
            ("numeric", f'{name}={{">={serial_from}<{serial_to}"}}'),
            ("expression",
             f'{name}={{"={name}>={serial_from} and {name}<{serial_to}"}}'),
        ]

    def _resolve_period_form(self, app_handle: int, app_id: str, field: str,
                             serial_from: int, serial_to: int) -> Dict[str, Any]:
        """Pick the working form by measuring, not by inspecting the field.

        The reference is `Count(DISTINCT If(...))`, which compares numbers
        and therefore cannot be fooled by how the field is displayed. A
        candidate is accepted only when it counts the same values. Both
        counts go out in one pipelined batch.
        """
        name = f"[{field}]"
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
                    app_handle, [f"=Count({{<{modifier}>}} DISTINCT {name})"])
                matched = counted[0].get("number") if counted else None
                if matched is not None and int(matched) > 0:
                    return {"modifier": modifier, "form": label,
                            "matched": int(matched)}
                # Nothing selected: fall through and measure again rather
                # than answer a zero built on a remembered choice.

        reference_expr = (
            f"=Count(DISTINCT If({name}>={serial_from} and {name}<{serial_to}, "
            f"{name}))")
        probes = [reference_expr] + [
            f"=Count({{<{modifier}>}} DISTINCT {name})"
            for _, modifier in candidates
        ]
        values = self.evaluate_expressions(app_handle, probes)
        if not values or values[0].get("number") is None:
            # The probe itself did not run. Use the form that holds
            # everywhere rather than refusing a query over a failed check.
            label, modifier = candidates[-1]
            return {"modifier": modifier, "form": label, "matched": None}

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

        for (label, modifier), value in zip(candidates, values[1:]):
            number = value.get("number")
            if number is not None and int(number) == reference:
                self._remember_form(app_id, field, label)
                return {"modifier": modifier, "form": label,
                        "matched": reference}

        # Every candidate disagreed with the reference. The expression form
        # is the one built from the same comparison the reference uses, so
        # it is what goes out, with the disagreement recorded.
        label, modifier = candidates[-1]
        logger.warning(
            "No filter form matched the reference count on %r (reference=%d)",
            field, reference)
        return {"modifier": modifier, "form": label, "matched": reference}

    def values_modifier(self, app_handle: int, field: str,
                        values: List[Any]) -> Dict[str, Any]:
        """A set modifier selecting named values of a field.

        Each value is checked against the field before the query runs. Qlik
        answers a filter on a value that does not exist with zeros, so
        `Moscow` against a field holding `Moskva` produces a clean, wrong
        table; here it produces a refusal naming the values that are
        missing and what the field holds instead.
        """
        wanted = [v for v in (values or []) if v is not None and str(v) != ""]
        if not wanted:
            return {
                "error": f"Filter on {field!r} lists no values.",
                "error_category": "invalid_filter",
            }
        name = f"[{field}]"
        probes = [
            f"=Count({{<{name}={{{quote_value(v)}}}>}} DISTINCT {name})"
            for v in wanted
        ]
        counts = self.evaluate_expressions(app_handle, probes)
        missing = [
            value for value, result in zip(wanted, counts)
            if result.get("number") is not None and int(result["number"]) == 0
        ]
        if missing:
            return {
                "error": (
                    f"Field {field!r} holds none of these values: "
                    + ", ".join(repr(v) for v in missing)
                ),
                "error_category": "value_not_found",
                "unknown_values": missing,
                "did_you_mean": self._nearby_values(app_handle, field, missing),
                "next_actions": [
                    f"read the values with get_app_field on {field!r}",
                    "copy a value exactly as Qlik stores it",
                ],
            }
        listed = ",".join(quote_value(v) for v in wanted)
        return {"modifier": f"{name}={{{listed}}}", "field": field,
                "values": wanted}

    def _nearby_values(self, app_handle: int, field: str,
                       missing: List[Any]) -> Dict[str, List[str]]:
        """What the field holds that resembles a value it does not have.

        Qlik's own search decides the resemblance — it matches a prefix,
        case-insensitively, which is how `Moscow` finds `Moskva`. One search
        per missing value and no more: measured on a field with 295k values,
        each search costs about two and a half seconds, and three retries
        per value turned a refusal into a seven-second wait.
        """
        suggestions: Dict[str, List[str]] = {}
        for value in missing[:2]:
            text = str(value)
            probe = text[:3]
            if not probe:
                continue
            try:
                found = self.search_app(app_handle, probe, fields=[field],
                                        max_fields=1, max_values=5)
            except Exception as exc:
                logger.debug("Value suggestion search failed: %s", exc)
                break
            matches = found.get("matches") or []
            if matches and matches[0].get("values"):
                suggestions[text] = matches[0]["values"]
        return suggestions

    def build_filters(self, app_handle: int, app_id: str,
                      filters: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Turn a list of filter descriptions into one set modifier.

        Every filter narrows the result further — they combine with AND,
        which is what "revenue in 2024 for the North region" means.
        """
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
            field = str(entry.get("field") or "").strip().strip("[]")
            if not field:
                return {
                    "error": f"Filter {entry!r} names no field.",
                    "error_category": "invalid_filter",
                }
            has_period = any(k in entry for k in ("from", "to", "period"))
            values = entry.get("values")
            if has_period and values:
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
                outcome = self.period_modifier(
                    app_handle, app_id, field,
                    entry.get("from", period), entry.get("to", period))
            elif values is not None:
                outcome = self.values_modifier(
                    app_handle, field,
                    values if isinstance(values, list) else [values])
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
            return {"modifier": "", "applied": []}
        return {"modifier": "{<" + ",".join(parts) + ">}", "applied": applied}
