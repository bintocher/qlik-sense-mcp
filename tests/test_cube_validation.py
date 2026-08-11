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
