"""Queries Qlik answers with a number instead of an error.

Qlik evaluates an unknown name as an expression worth 0 and never says so.
Measured: against a 10M-row app: a cube grouped by `no_such_field`
came back as one row holding 49,989,556,885.52 — the grand total over all
ten regions, indistinguishable from a real answer. A measure filtered on
`region_name={'Moscow'}` where the data says `Moskva` returned a full,
well-formed table of zeros.

Nothing downstream can recover from that: the rows are valid JSON, the
numbers are plausible, and the model reports them as fact. So the check
happens here, before the query runs — and every judgement in it comes from
Engine, not from reading the expression.

The double below answers the three Engine calls the way the real one does,
measured:

  ExpandExpression         resolves `$(...)` to literal text
  CheckExpression          `qErrorMsg` for syntax, `qBadFieldNames` for
                           names outside a set modifier — measured, it
                           does not look inside one
  GetFieldsFromExpression  the modifier fields it recognised, and only
                           those: `Sum(Amount)` returns an empty list
"""

import re

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI

_MODIFIER = re.compile(r"\{<.*?>\}", re.S)
_NAME = re.compile(r"\[([^\]]+)\]|([A-Za-z_][A-Za-z_0-9]*)")
# Qlik function names the double must not mistake for fields.
_FUNCTIONS = {"sum", "avg", "count", "min", "max", "year", "month", "if",
              "distinct", "aggr", "only", "median", "stdev", "text", "num",
              "date", "and", "or", "not", "total"}


class _Engine(QlikEngineAPI):
    """Answers like Qlik does, for a fixed set of field names."""

    def __init__(self, known=("Region", "Sales", "OrderDate", "Category"),
                 syntax_errors=None):
        self.known = set(known)
        self.syntax_errors = syntax_errors or {}
        self.checked = []

    # -- what the double pretends Qlik knows -------------------------------

    def _bad_names(self, expression):
        """Names outside any set modifier that the model does not have."""
        outside = _MODIFIER.sub(" ", expression)
        spans = []
        for match in _NAME.finditer(outside):
            name = match.group(1) or match.group(2)
            if name.lower() in _FUNCTIONS or name.isdigit():
                continue
            if name in self.known:
                continue
            spans.append({"qFrom": match.start(), "qCount": match.end() - match.start()})
        return outside, spans

    def _modifier_fields(self, expression):
        found = []
        for block in _MODIFIER.findall(expression):
            for match in _NAME.finditer(block):
                name = match.group(1) or match.group(2)
                if name in self.known:
                    found.append(name)
        return list(dict.fromkeys(found))

    # -- the Engine surface ------------------------------------------------

    def send_requests_pipelined(self, requests, raise_on_error=True, timeout=None):
        method = requests[0]["method"]
        if method == "ExpandExpression":
            # Measured: `=Sum($(vAny) call_duration)` came back as
            # `=Sum( call_duration)` — the reference is replaced by nothing.
            return [{"qExpandedExpression":
                     re.sub(r"\$\([^)]*\)", "", r["params"][0])}
                    for r in requests]
        if method == "CheckExpression":
            replies = []
            for request in requests:
                expression = request["params"][0]
                self.checked.append(expression)
                text, spans = self._bad_names(expression)
                replies.append({
                    "qErrorMsg": self.syntax_errors.get(expression, ""),
                    "qBadFieldNames": spans if not self.syntax_errors.get(expression) else [],
                    "_text": text,
                })
            # Engine reports positions into the expression it was given;
            # the double blanked the modifiers, so hand back that same text
            # for the positions to line up.
            return [{k: v for k, v in r.items() if k != "_text"} for r in replies]
        if method == "GetFieldsFromExpression":
            return [{"qFieldNames": self._modifier_fields(r["params"][0])}
                    for r in requests]
        raise AssertionError(f"unexpected Engine call {method}")

    def get_fields(self, app_handle):
        # The Engine layer's own key name — the tool layer is what renames it
        # to `name`. Getting this wrong made every suggestion list empty.
        return {"fields": [{"field_name": n} for n in sorted(self.known)]}


def _dims(*fields):
    return [{"field": f} for f in fields]


def _measures(*expressions):
    return [{"expression": e} for e in expressions]


class TestUnknownDimension:
    def test_unknown_field_is_refused(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Regionn"), _measures("Sum(Sales)"))
        assert result["error_category"] == "field_not_found"
        assert result["unknown_fields"] == ["Regionn"]










class TestUnknownNamesInMeasures:
    """Engine's verdict, so the query stops rather than warns.

    `qBadFieldNames` is Qlik saying the name is not in the data model, and
    Qlik scores such a name as 0 — the measure would come back as a column
    of zeros that reads as a real answer.
    """

    def test_an_unknown_name_in_a_measure_is_refused(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), _measures("Sum(Salez)"))
        assert result["error_category"] == "field_not_found"
        assert result["unknown_fields"] == ["Salez"]





class TestEmptyMeasureDetection:
    COLUMNS = ["Region", "Revenue", "Filtered"]

    def test_an_all_zero_measure_is_reported(self):
        rows = [["North", 100, 0], ["South", 200, 0]]
        assert QlikEngineAPI._measure_columns_are_empty(rows, 1, self.COLUMNS) == ["Filtered"]










