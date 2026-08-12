"""Expression checks, performed by Qlik itself.

Qlik answers a malformed query with a number rather than an error, so a
query is checked before it is run. Every check here is an Engine call:
Qlik's own parser decides what is valid, which names exist and what a
filter selects. Nothing in this module parses Qlik syntax.

Measured: per call on an open app: `ExpandExpression` 2ms,
`CheckExpression` 2ms, `GetFieldsFromExpression` 2ms, `EvaluateEx` 2-10ms.
All of them in one pipelined batch: 4ms, against 75ms for the smallest
hypercube. The checks cost a twentieth of the query they guard.
"""

from typing import Any, Dict, List
import logging

logger = logging.getLogger(__name__)


class EngineExpressionsMixin:
    """Ask Engine whether an expression is sound, before running it."""

    def expand_expressions(self, app_handle: int,
                           expressions: List[str]) -> Dict[str, str]:
        """Resolve `$(...)` variable expansion to literal text.

        A variable holds arbitrary text — a whole set modifier, a field
        name, a comparison — and until Qlik expands it, no check applied to
        the expression sees what will actually run.

        Returns {original: expanded}; an expression Engine could not expand
        maps to itself.
        """
        wanted = [e for e in dict.fromkeys(expressions) if e]
        if not wanted:
            return {}
        try:
            outcomes = self.send_requests_pipelined(
                [{"method": "ExpandExpression", "params": [expr], "handle": app_handle}
                 for expr in wanted],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.debug("ExpandExpression unavailable: %s", exc)
            return {expr: expr for expr in wanted}
        expanded = {}
        for expr, outcome in zip(wanted, outcomes):
            if isinstance(outcome, Exception):
                expanded[expr] = expr
                continue
            expanded[expr] = (outcome or {}).get("qExpandedExpression") or expr
        return expanded

    def check_expressions(self, app_handle: int,
                          expressions: List[str]) -> Dict[str, Dict[str, Any]]:
        """Parse each expression and name what is wrong with it.

        `CheckExpression` reports two separate things, and both matter:

        `qErrorMsg` — the expression does not parse. `Sum(x) AS total` comes
        back as "Garbage after expression: 'AS'", `COUNTX(x)` as "COUNTX is
        not a valid function", `Sum(Sum(x))` as "Nested aggregation not
        allowed".

        `qBadFieldNames` — character ranges of names the data model does not
        have. Measured: it covers names in the aggregation and in a
        calculated dimension, and it stops at the set modifier —
        `Sum({<no_such={'x'}>} Amount)` reports nothing. Names inside a
        modifier are the job of `fields_in_expressions`.

        Returns {expression: {"error": str, "bad_fields": [str, ...]}} for
        expressions with something wrong; sound ones are absent.
        """
        wanted = [e for e in dict.fromkeys(expressions) if e]
        if not wanted:
            return {}
        try:
            outcomes = self.send_requests_pipelined(
                [{"method": "CheckExpression", "params": [expr], "handle": app_handle}
                 for expr in wanted],
                raise_on_error=False,
            )
        except Exception as exc:
            # A guard that cannot run must not become an outage: the query
            # goes through unchecked, and the result-side warnings still
            # apply.
            logger.debug("CheckExpression unavailable: %s", exc)
            return {}

        faults: Dict[str, Dict[str, Any]] = {}
        for expr, outcome in zip(wanted, outcomes):
            if isinstance(outcome, Exception):
                continue
            reply = outcome or {}
            message = " ".join((reply.get("qErrorMsg") or "").split())
            bad = self._names_at(expr, reply.get("qBadFieldNames") or [])
            if message or bad:
                faults[expr] = {"error": message, "bad_fields": bad}
        return faults

    @staticmethod
    def _names_at(text: str, ranges: List[Dict[str, int]]) -> List[str]:
        """Cut the names Engine flagged out of the expression it read.

        Engine reports position and length rather than the text, so the
        caller is told `no_such_field` instead of "characters 28 to 41".
        """
        names = []
        for span in ranges:
            start = span.get("qFrom")
            count = span.get("qCount")
            if not isinstance(start, int) or not isinstance(count, int):
                continue
            name = text[start:start + count].strip().strip("[]'\"")
            if name:
                names.append(name)
        return list(dict.fromkeys(names))

    def fields_in_expressions(self, app_handle: int,
                              expressions: List[str]) -> Dict[str, List[str]]:
        """Field names Engine recognises inside each expression's set modifiers.

        `GetFieldsFromExpression` answers with the fields a set modifier
        filters on, and only those — measured: `Sum(Amount)`
        returns an empty list and `Sum({<Year={2024}>} Amount)` returns
        `["Year"]`. It is the only Engine call that sees inside a modifier,
        which is where a wrong name does the most damage: Qlik drops the
        condition and answers with the unfiltered total, a number larger
        than the truth that reads as a real answer.
        """
        wanted = [e for e in dict.fromkeys(expressions) if e]
        if not wanted:
            return {}
        try:
            outcomes = self.send_requests_pipelined(
                [{"method": "GetFieldsFromExpression", "params": [expr],
                  "handle": app_handle} for expr in wanted],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.debug("GetFieldsFromExpression unavailable: %s", exc)
            return {}
        found = {}
        for expr, outcome in zip(wanted, outcomes):
            if isinstance(outcome, Exception):
                continue
            found[expr] = list((outcome or {}).get("qFieldNames") or [])
        return found

    def evaluate_expressions(self, app_handle: int,
                             expressions: List[str]) -> List[Dict[str, Any]]:
        """Evaluate expressions without creating an object for them.

        `EvaluateEx` returns the value and whether it is numeric, in a few
        milliseconds. It is how this server learns what a filter selects
        before spending a hypercube on it.

        Returns one {"text", "number", "is_numeric", "error"} per input, in
        order.
        """
        if not expressions:
            return []
        try:
            outcomes = self.send_requests_pipelined(
                [{"method": "EvaluateEx", "params": [expr], "handle": app_handle}
                 for expr in expressions],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.debug("EvaluateEx unavailable: %s", exc)
            return [{"text": None, "number": None, "is_numeric": False,
                     "error": str(exc)} for _ in expressions]
        values = []
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                values.append({"text": None, "number": None,
                               "is_numeric": False, "error": str(outcome)})
                continue
            value = (outcome or {}).get("qValue") or {}
            # Engine writes "no number here" as the string "NaN" — for a
            # Null result, for text, for an expression it could not
            # evaluate. Letting that through as a number reached a
            # comparison as a string and raised TypeError; None is what
            # every caller already handles.
            number = value.get("qNumber")
            if isinstance(number, str):
                try:
                    number = float(number)
                except ValueError:
                    number = None
                else:
                    number = None if number != number else number
            values.append({
                "text": value.get("qText"),
                "number": number,
                "is_numeric": bool(value.get("qIsNumeric")),
                "error": None,
            })
        return values
