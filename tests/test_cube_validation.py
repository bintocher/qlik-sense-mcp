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

    def test_a_name_with_a_space_reaches_qlik_in_brackets(self):
        """Bare, Qlik reads it as two tokens: "Garbage after expression".

        The wrapping happens where the plan is built, so what the check
        sees is what the cube will run.
        """
        from tests.test_hypercube import _PagingEngine

        engine = _PagingEngine(total_rows=1, first_page=1)
        result = engine.create_hypercube(
            1, dimensions=[{"field": "Тип ставки"}],
            measures=[{"expression": "Sum(Sales)"}], max_rows=5)
        assert result["dimensions"][0]["field"] == "[Тип ставки]"

    def test_an_expression_dimension_is_left_alone(self):
        from tests.test_hypercube import _PagingEngine

        engine = _PagingEngine(total_rows=1, first_page=1)
        result = engine.create_hypercube(
            1, dimensions=[{"field": "=Year(OrderDate)"}],
            measures=[{"expression": "Sum(Sales)"}], max_rows=5)
        assert result["dimensions"][0]["field"] == "=Year(OrderDate)"

    def test_no_spelling_is_suggested(self):
        """The model reads the field list itself; a guess from string
        similarity is not a fact about the app."""
        result = _Engine()._validate_cube_inputs(1, _dims("Regionn"), [])
        assert "did_you_mean" not in result
        assert any("get_app_details" in a for a in result["next_actions"])

    def test_known_fields_pass(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), _measures("Sum(Sales)"))
        assert "error" not in result
        assert result["warnings"] == []

    def test_brackets_are_stripped_before_checking(self):
        result = _Engine()._validate_cube_inputs(1, _dims("[Region]"), [])
        assert "error" not in result

    def test_a_calculated_dimension_of_known_fields_passes(self):
        result = _Engine()._validate_cube_inputs(1, _dims("=Year(OrderDate)"), [])
        assert "error" not in result

    def test_no_dimensions_is_fine(self):
        assert "error" not in _Engine()._validate_cube_inputs(1, [], _measures("Sum(Sales)"))

    def test_nothing_to_check_needs_no_engine_call(self):
        engine = _Engine()
        assert engine._validate_cube_inputs(1, [], []) == {"warnings": []}
        assert engine.checked == []


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

    def test_the_refusal_names_the_field_in_brackets(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), _measures("Sum(Salez)"))
        assert "[Salez]" in result["error"]
        assert "did_you_mean" not in result

    def test_a_variable_expansion_is_resolved_before_checking(self):
        """`$(vTarget)` is Qlik's to expand; the name inside is not a field."""
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum(Sales) / $(vTarget)"))
        assert result["warnings"] == []
        assert "error" not in result

    def test_a_bad_name_inside_a_calculated_dimension_is_refused(self):
        result = _Engine()._validate_cube_inputs(1, [{"field": "=Year(OrderDatte)"}], [])
        assert result["error_category"] == "field_not_found"
        assert "OrderDatte" in result["unknown_fields"]


class TestEmptyMeasureDetection:
    COLUMNS = ["Region", "Revenue", "Filtered"]

    def test_an_all_zero_measure_is_reported(self):
        rows = [["North", 100, 0], ["South", 200, 0]]
        assert QlikEngineAPI._measure_columns_are_empty(rows, 1, self.COLUMNS) == ["Filtered"]

    def test_a_measure_of_dashes_is_reported(self):
        """What Qlik returns for an expression it could not evaluate."""
        rows = [["North", 100, "-"], ["South", 200, "-"]]
        assert QlikEngineAPI._measure_columns_are_empty(rows, 1, self.COLUMNS) == ["Filtered"]

    def test_one_real_value_is_enough_to_stay_quiet(self):
        rows = [["North", 100, 0], ["South", 200, 5]]
        assert QlikEngineAPI._measure_columns_are_empty(rows, 1, self.COLUMNS) == []

    def test_dimension_columns_are_not_measures(self):
        """A dimension of zeros is data, not a broken expression."""
        rows = [[0, 100], [0, 200]]
        assert QlikEngineAPI._measure_columns_are_empty(rows, 1, ["Flag", "Revenue"]) == []

    def test_no_rows_means_nothing_to_say(self):
        assert QlikEngineAPI._measure_columns_are_empty([], 1, self.COLUMNS) == []


class TestDimensionShape:
    """A model writes a calculated dimension the way it writes a measure.

    `measures` take `{"expression": ...}`, so `dimensions` got the same
    key — and the code went straight to `dim["field"]`, raising
    `KeyError: 'field'`. Found by an LLM driving the real server: it
    wanted `Year(order_date)` as a dimension and got an opaque crash.
    """

    @staticmethod
    def _cube(**kwargs):
        """Run the argument handling against a stubbed Engine."""
        from tests.test_hypercube import _PagingEngine
        return _PagingEngine(total_rows=2, first_page=2).create_hypercube(1, **kwargs)

    @pytest.mark.parametrize("alias", ["expression", "name", "definition", "qDef"])
    def test_a_dimension_spelled_another_way_is_accepted(self, alias):
        """It must get past argument handling; what follows needs a socket."""
        result = self._cube(dimensions=[{alias: "=Year(OrderDate)"}],
                            measures=[{"expression": "Sum(Sales)"}], max_rows=5)
        assert result.get("error_category") != "invalid_argument", result

    def test_a_dimension_with_no_name_is_refused_clearly(self):
        result = self._cube(dimensions=[{"sort_by": {}}],
                            measures=[{"expression": "Sum(Sales)"}], max_rows=5)
        assert result["error_category"] == "invalid_argument"
        assert "dimensions[0]" in result["error"]
        assert "=Year(OrderDate)" in result["hint"]

    def test_an_empty_field_name_is_refused(self):
        result = self._cube(dimensions=[{"field": "   "}],
                            measures=[{"expression": "Sum(Sales)"}], max_rows=5)
        assert result["error_category"] == "invalid_argument"

    def test_the_position_of_the_bad_dimension_is_named(self):
        result = self._cube(dimensions=[{"field": "Region"}, {"nonsense": 1}],
                            measures=[{"expression": "Sum(Sales)"}], max_rows=5)
        assert "dimensions[1]" in result["error"]


class TestSetModifierFields:
    """What can be said about a hand-written set modifier: nothing.

    Measured on a live Engine, `GetFieldsFromExpression` answers with every
    field of the expression rather than the ones a modifier filters on —
    `Sum([Amount])` comes back as `['Amount']`. So no call distinguishes a
    filter field from an aggregated one, and the warning that used to claim
    the difference fired on every measure that named an existing field.

    A filter that is checked end to end is one stated as `filters`, where
    the server writes the names and proves each one selects something.
    """

    def test_a_modifier_on_a_known_field_passes_quietly(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Category={'Books'}>} Sales)"))
        assert result == {"warnings": []}

    def test_a_plain_measure_passes_quietly(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum(Sales)"))
        assert result == {"warnings": []}

    def test_no_claim_is_made_about_the_modifier(self):
        """The old warning fired on every measure with a known field."""
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Category={'Books'}>} Sales)"))
        assert not any("Set analysis" in w for w in result["warnings"])

    def test_a_bracketed_name_with_a_space_is_accepted(self):
        engine = _Engine(known=("Region", "Sales", "Order Date"))
        result = engine._validate_cube_inputs(
            1, _dims("Region"), _measures('Sum({<[Order Date]={">=1<2"}>} Sales)'))
        assert "error" not in result

    def test_engine_is_not_asked_about_modifier_fields_any_more(self):
        """The call cannot answer the question, so it is not made."""
        asked = []

        class _Recording(_Engine):
            def send_requests_pipelined(self, requests, raise_on_error=True,
                                        timeout=None):
                asked.append(requests[0]["method"])
                return super().send_requests_pipelined(
                    requests, raise_on_error, timeout)

        _Recording()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Category={'Books'}>} Sales)"))
        assert "GetFieldsFromExpression" not in asked


class TestExpressionSyntax:
    """Engine's own parser, asked before anything is built."""

    def test_a_parse_error_is_reported_with_qliks_own_words(self):
        engine = _Engine(syntax_errors={
            "SUM(Sales) AS total": "Garbage after expression: 'AS'"})
        result = engine._validate_cube_inputs(
            1, _dims("Region"), _measures("SUM(Sales) AS total"))
        assert result["error_category"] == "invalid_expression"
        assert "Garbage after expression" in result["error"]

    def test_valid_syntax_passes_through(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), _measures("Sum(Sales)"))
        assert "error" not in result

    def test_a_calculated_dimension_is_checked_too(self):
        engine = _Engine(syntax_errors={
            "=Yearr(OrderDate)": "Yearr is not a valid function"})
        result = engine._validate_cube_inputs(
            1, [{"field": "=Yearr(OrderDate)"}], _measures("Sum(Sales)"))
        assert result["error_category"] == "invalid_expression"

    def test_the_error_names_what_the_caller_wrote(self):
        """Checks run on the expanded text; the reply must quote the
        original, which is what the caller has to fix."""
        engine = _Engine(syntax_errors={"Sum( Sales)": "some parser complaint"})
        result = engine._validate_cube_inputs(
            1, [], _measures("Sum($(vScope) Sales)"))
        assert "Sum($(vScope) Sales)" in result["error"]
