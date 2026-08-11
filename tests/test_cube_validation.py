"""Queries Qlik answers with a number instead of an error.

Qlik evaluates an unknown name as an expression worth 0 and never says so.
Measured on 31.62 against a 10M-row app: a cube grouped by `no_such_field`
came back as one row holding 49,989,556,885.52 — the grand total over all
ten regions, indistinguishable from a real answer. A measure filtered on
`region_name={'Moscow'}` where the data says `Moskva` returned a full,
well-formed table of zeros.

Nothing downstream can recover from that: the rows are valid JSON, the
numbers are plausible, and the model reports them as fact. So the check
happens here, before the query runs.
"""

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _Engine(QlikEngineAPI):
    """Knows a fixed set of field names; records what was asked."""

    def __init__(self, known=("Region", "Sales", "OrderDate", "Category")):
        self.known = set(known)
        self.checked = []

    def send_requests_pipelined(self, requests, raise_on_error=True):
        if requests[0]["method"] == "CheckExpression":
            # The double is not a Qlik parser; treat every expression as
            # syntactically fine and let the field checks do the work.
            return [{"qErrorMsg": ""} for _ in requests]
        outcomes = []
        for request in requests:
            name = request["params"][0]
            self.checked.append(name)
            if name in self.known:
                outcomes.append({"qReturn": {"qName": name, "qCardinal": 10}})
            else:
                outcomes.append(Exception("Invalid parameters"))
        return outcomes

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

    def test_a_near_miss_is_suggested(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Regionn"), [])
        assert result["did_you_mean"]["Regionn"] == ["Region"]

    def test_known_fields_pass(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), _measures("Sum(Sales)"))
        assert "error" not in result
        assert result["warnings"] == []

    def test_brackets_are_stripped_before_checking(self):
        result = _Engine()._validate_cube_inputs(1, _dims("[Region]"), [])
        assert "error" not in result

    def test_a_calculated_dimension_is_not_a_field(self):
        """`=Year(OrderDate)` is an expression; checking it as a name would
        refuse a query that works."""
        engine = _Engine()
        result = engine._validate_cube_inputs(1, _dims("=Year(OrderDate)"), [])
        assert "error" not in result
        assert "=Year(OrderDate)" not in engine.checked

    def test_no_dimensions_is_fine(self):
        assert "error" not in _Engine()._validate_cube_inputs(1, [], _measures("Sum(Sales)"))


class TestMeasureWarnings:
    def test_unknown_name_in_a_measure_warns_but_does_not_block(self):
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), _measures("Sum(Salez)"))
        assert "error" not in result
        assert any("Salez" in w for w in result["warnings"])

    def test_a_variable_expansion_is_not_treated_as_a_field(self):
        """`$(vTarget)` is resolved by Qlik; the name inside is not a field."""
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum(Sales) / $(vTarget)"))
        assert result["warnings"] == []

    @pytest.mark.parametrize("expression, fragment", [
        ("SUM(Sales) AS total", "AS <alias>"),
        ("SELECT Sum(Sales)", "SELECT"),
        ("Sum(Sales) GROUP BY Region", "GROUP BY"),
        ("Sum(Sales) FROM Orders", "FROM"),
        ("Sum(Sales) WHERE Region='North'", "WHERE"),
    ])
    def test_sql_syntax_is_named(self, expression, fragment):
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), [{"expression": expression}])
        assert any(fragment in w for w in result["warnings"]), result["warnings"]

    def test_set_analysis_is_not_mistaken_for_sql(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Category={'Books'}>} Sales)"))
        assert result["warnings"] == []

    def test_each_field_is_checked_once(self):
        engine = _Engine()
        engine._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum(Sales)", "Avg(Sales)", "Count(Sales)"))
        assert engine.checked.count("Sales") == 1


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


class TestSuggestions:
    """The wrong case is the most common way to miss a field name.

    Qlik field names are case-sensitive, so `REGION_NAME` is a genuine
    miss — but answering it with no suggestion at all leaves the caller
    with nothing to act on, when the field is right there.
    """

    def test_a_different_case_is_suggested(self):
        engine = _Engine(known=("region_name", "region_code"))
        result = engine._validate_cube_inputs(1, _dims("REGION_NAME"), [])
        assert result["did_you_mean"]["REGION_NAME"][0] == "region_name"

    def test_the_suggestion_keeps_the_real_spelling(self):
        engine = _Engine(known=("OrderDate",))
        result = engine._validate_cube_inputs(1, _dims("orderdate"), [])
        assert result["did_you_mean"]["orderdate"] == ["OrderDate"]

    def test_a_join_key_is_suggested_once(self):
        """The same field in two tables came back three times in the list."""
        class _Duplicated(_Engine):
            def get_fields(self, app_handle):
                return {"fields": [{"field_name": "region_code"},
                                   {"field_name": "region_code"},
                                   {"field_name": "region_name"}]}

        result = _Duplicated()._validate_cube_inputs(1, _dims("regioncode"), [])
        assert result["did_you_mean"]["regioncode"].count("region_code") == 1

    def test_nothing_similar_means_no_key_at_all(self):
        engine = _Engine(known=("Region",))
        result = engine._validate_cube_inputs(1, _dims("zzzzzz"), [])
        assert "zzzzzz" not in result.get("did_you_mean", {})


class TestCalculatedDimensions:
    """`=Year(no_such_field)` used to skip the check entirely.

    A calculated dimension is an expression, so it is not a field name —
    but the names inside it are, and passing it through unexamined left
    open the exact hole the check exists to close.
    """

    def test_a_bad_name_inside_a_calculated_dimension_is_reported(self):
        result = _Engine()._validate_cube_inputs(
            1, [{"field": "=Year(OrderDatte)"}], [])
        assert any("OrderDatte" in w for w in result["warnings"]), result

    def test_a_good_calculated_dimension_is_quiet(self):
        result = _Engine()._validate_cube_inputs(
            1, [{"field": "=Year(OrderDate)"}], [])
        assert result["warnings"] == []

    def test_it_warns_rather_than_refuses(self):
        """Lexical, so it must not block a query that works."""
        result = _Engine()._validate_cube_inputs(
            1, [{"field": "=Year(OrderDatte)"}], [])
        assert "error" not in result


class TestSqlDetectionPrecision:
    def test_a_field_name_containing_a_keyword_is_not_sql(self):
        """`[Cost as planned]` is a legal field name, not an alias."""
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), [{"expression": "Sum([Cost as planned])"}])
        assert not any("SQL" in w for w in result["warnings"]), result["warnings"]

    def test_a_string_literal_containing_a_keyword_is_not_sql(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), [{"expression": "Sum(If(Region='North as usual', Sales))"}])
        assert not any("SQL" in w for w in result["warnings"]), result["warnings"]

    @pytest.mark.parametrize("expression", [
        'SUM(Sales) AS "total"',
        "SUM(Sales) AS [total]",
        "SUM(Sales) AS total",
    ])
    def test_every_alias_form_is_caught(self, expression):
        """A quoted alias is the form a model writes most often."""
        result = _Engine()._validate_cube_inputs(1, _dims("Region"), [{"expression": expression}])
        assert any("AS <alias>" in w for w in result["warnings"]), (expression, result["warnings"])


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
    """A set modifier on a field that does not exist is the worst case.

    Qlik does not reject it — it drops the condition. The measure then
    returns the unfiltered total: a number LARGER than the truth, which
    the all-zero detector cannot see and a reader has no reason to
    doubt.
    """

    def test_an_unknown_modifier_field_is_refused(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Regionn={'North'}>} Sales)"))
        assert result["error_category"] == "field_not_found"
        assert "Regionn" in result["unknown_fields"]

    def test_the_refusal_explains_why_it_matters(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Regionn={'North'}>} Sales)"))
        assert "UNFILTERED" in result["hint"]

    def test_a_known_modifier_field_passes(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Category={'Books'}>} Sales)"))
        assert "error" not in result

    def test_a_bracketed_name_with_a_space(self):
        engine = _Engine(known=("Region", "Sales", "Order Date"))
        result = engine._validate_cube_inputs(
            1, _dims("Region"), _measures('Sum({<[Order Date]={">=1<2"}>} Sales)'))
        assert "error" not in result, result

    def test_the_selection_operator_is_not_part_of_the_name(self):
        """`Year*=` means "add to the current selection"."""
        engine = _Engine(known=("Region", "Sales", "Year"))
        result = engine._validate_cube_inputs(
            1, _dims("Region"), _measures('Sum({<Year*={">2020"}>} Sales)'))
        assert "error" not in result, result


class TestQuotingTraps:
    def test_a_comparison_in_single_quotes_is_flagged(self):
        """'>=100' is a literal; it matches nothing and returns 0."""
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures("Sum({<Sales={'>=100'}>} Sales)"))
        assert any("single quotes" in w for w in result["warnings"]), result

    def test_a_spaced_range_is_flagged(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures('Sum({<Sales={">=100 <200"}>} Sales)'))
        assert any("space inside a range" in w for w in result["warnings"]), result

    def test_a_correct_range_is_quiet(self):
        result = _Engine()._validate_cube_inputs(
            1, _dims("Region"), _measures('Sum({<Sales={">=100<200"}>} Sales)'))
        assert result["warnings"] == [], result


class TestExpressionSyntax:
    """Engine's own parser, asked before anything is built."""

    class _Parser(_Engine):
        def __init__(self, errors=None, **kwargs):
            super().__init__(**kwargs)
            self.errors = errors or {}

        def send_requests_pipelined(self, requests, raise_on_error=True):
            if requests[0]["method"] == "CheckExpression":
                return [{"qErrorMsg": self.errors.get(r["params"][0], "")}
                        for r in requests]
            return super().send_requests_pipelined(requests, raise_on_error)

    def test_a_parse_error_is_reported_with_qliks_own_words(self):
        engine = self._Parser({"SUM(Sales) AS total": "Garbage after expression: 'AS'"})
        result = engine._validate_cube_inputs(
            1, _dims("Region"), _measures("SUM(Sales) AS total"))
        assert result["error_category"] == "invalid_expression"
        assert "Garbage after expression" in result["error"]

    def test_valid_syntax_passes_through(self):
        engine = self._Parser()
        result = engine._validate_cube_inputs(1, _dims("Region"), _measures("Sum(Sales)"))
        assert "error" not in result

    def test_a_calculated_dimension_is_checked_too(self):
        engine = self._Parser({"=Yearr(OrderDate)": "Yearr is not a valid function"})
        result = engine._validate_cube_inputs(
            1, [{"field": "=Yearr(OrderDate)"}], _measures("Sum(Sales)"))
        assert result["error_category"] == "invalid_expression"
