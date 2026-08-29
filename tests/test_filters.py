"""Filters described by the caller, written as set analysis by the server.

The forms below are not interchangeable, which is the whole reason this
code exists. Measured against a date field carrying a time of day:

    {<[F]={">=40542<40908"}>}                  0
    {<[F]={"=[F]>=40542 and [F]<40908"}>}      correct

and against a date field whose values display as bare numbers, both are
correct — the numeric one sixty times cheaper. Comparison inside a set
modifier runs against the text Qlik displays, so which form works depends
on the field, and the server measures rather than assumes.
"""

import datetime

import pytest

from qlik_sense_mcp_server.engine.filters import (
    BOUND_FORMS,
    _parse_bound,
    _to_serial,
    quote_value,
)
from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _Engine(QlikEngineAPI):
    """Counts values the way a field with a known set of days would.

    `numeric_text` decides whether the field displays as a number, which
    is exactly what decides whether the cheap filter form works.
    """

    def __init__(self, days=(40544, 40545, 40910), numeric_text=True,
                 values=("North", "South"), date_fields=("F",)):
        self.days = set(days)
        self.date_fields = set(date_fields)
        self.numeric_text = numeric_text
        self.values = set(values)
        self.asked = []

    def evaluate_expressions(self, app_handle, expressions):
        answers = []
        for expression in expressions:
            self.asked.append(expression)
            answers.append({"text": None, "number": self._count(expression),
                            "is_numeric": True, "error": None})
        return answers

    def _count(self, expression):
        if "If(" in expression:
            # The reference: a numeric comparison, always right.
            low, high = self._bounds(expression)
            return len([d for d in self.days if low <= d < high])
        if '{">=' in expression:
            if not self.numeric_text:
                return 0
            low, high = self._bounds(expression)
            return len([d for d in self.days if low <= d < high])
        if '{"=' in expression:
            low, high = self._bounds(expression)
            return len([d for d in self.days if low <= d < high])
        # A value filter: `{<[F]={'North'}>}`.
        for value in self.values:
            if f"'{value}'" in expression:
                return 1
        return 0

    @staticmethod
    def _bounds(expression):
        import re
        numbers = [int(n) for n in re.findall(r"\d+", expression)]
        return numbers[-2], numbers[-1]

    def get_field_description(self, app_handle, field_name):
        """Qlik's tags decide whether a bound is a day or a value."""
        tags = ["$numeric", "$date"] if field_name in self.date_fields else ["$text"]
        return {"name": field_name, "tags": tags}

    def search_app(self, app_handle, term, fields=None, max_fields=8,
                   max_values=5):
        hits = [v for v in sorted(self.values) if v.lower().startswith(term.lower())]
        return {"matches": [{"field": fields[0], "values": hits}] if hits else []}


class TestBoundParsing:
    @pytest.mark.parametrize("text, expected", [
        ("2024-01-31", datetime.date(2024, 1, 31)),
        ("31.01.2024", datetime.date(2024, 1, 31)),
        (45292, datetime.date(2024, 1, 1)),
    ])
    def test_a_day_is_read_the_same_whichever_way_it_is_written(self, text, expected):
        assert _parse_bound(text, upper=False) == expected

    def test_a_year_starts_in_january_as_a_lower_bound(self):
        assert _parse_bound("2024", upper=False) == datetime.date(2024, 1, 1)







class TestPeriodForm:
    def test_the_cheap_form_is_used_when_it_agrees_with_the_reference(self):
        engine = _Engine(numeric_text=True)
        result = engine.period_modifier(1, "app", "F", "2011", "2011")
        assert result["form"] == "numeric"
        assert result["modifier"] == '[F]={">=40544<40909"}'

    def test_the_expression_form_is_used_when_the_cheap_one_selects_nothing(self):
        engine = _Engine(numeric_text=False)
        result = engine.period_modifier(1, "app", "F", "2011", "2011")
        assert result["form"] == "expression"
        assert result["modifier"] == '[F]={"=[F]>=40544 and [F]<40909"}'








class TestFormIsRemembered:
    def test_the_second_period_on_a_field_costs_two_counts(self):
        """The remembered form still saves the other candidates, but it is
        measured against the reference like any other: remembering that a
        form worked once is not knowing it counts the right days for this
        period."""
        engine = _Engine()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        first = len(engine.asked)
        engine.asked.clear()
        engine.period_modifier(1, "app", "F", "2012", "2012")
        assert len(engine.asked) == 2 < first

    def test_forgetting_puts_the_measurement_back(self):
        engine = _Engine()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        engine.forget_filter_forms("app")
        engine.asked.clear()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        assert len(engine.asked) > 1



class TestValueFilters:
    def test_known_values_become_an_exact_match_list(self):
        engine = _Engine(values=("North", "South"))
        result = engine.values_modifier(1, "Region", ["North", "South"])
        assert result["modifier"] == "[Region]={'North','South'}"

    def test_a_value_the_field_does_not_hold_is_refused(self):
        engine = _Engine(values=("Moskva",))
        result = engine.values_modifier(1, "Region", ["Moscow"])
        assert result["error_category"] == "value_not_found"
        assert result["unknown_values"] == ["Moscow"]





class TestCombining:
    def test_two_filters_narrow_together(self):
        engine = _Engine(values=("North",))
        result = engine.build_filters(1, "app", [
            {"field": "F", "period": "2011"},
            {"field": "Region", "values": ["North"]},
        ])
        assert result["modifier"].startswith("{<")
        assert result["modifier"].endswith(">}")
        assert result["modifier"].count(",") >= 1

    def test_no_filters_is_no_modifier(self):
        assert _Engine().build_filters(1, "app", [])["modifier"] == ""









class TestImpossibleBounds:
    @pytest.mark.parametrize("text", ["2024-02-30", "2024-13", "31.04.2024",
                                      "2024-00", "0000-00-00"])
    def test_a_date_that_does_not_exist_is_not_a_bound(self, text):
        assert _parse_bound(text, upper=False) is None

    @pytest.mark.parametrize("text", ["2024-02-29", "29.02.2024", "2024-12"])
    def test_a_date_that_does_exist_is_read(self, text):
        assert _parse_bound(text, upper=False) is not None


class TestNumericRanges:
    """The same two keys mean days on a date field and values elsewhere.

    Measured against a live model: `{"field": "discount", "from": 400}`
    read as a date turned "more than 400 off" into "some time in 1901" and
    answered 18,774 where the truth was 1,898,591.
    """

    def test_a_bound_on_a_field_that_is_not_a_date_is_a_value(self):
        engine = _Engine(values=(), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 12, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(1, "app", [
            {"field": "discount", "from": 400, "to": 500}])
        assert result["modifier"] == '{<[discount]={">=400<=500"}>}'

    def test_one_open_end_is_allowed(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 7, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(1, "app", [{"field": "price", "from": 400}])
        assert result["modifier"] == '{<[price]={">=400"}>}'












class TestSetIdentifier:
    """Which set the filters narrow. Verified against a live app of four
    rows worth 100: all of it ignoring selections gave 100, and the same
    with a year filter gave 40."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North", "South"), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_ignoring_selections(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}],
            scope={"ignore_selections": True})
        assert result["modifier"] == "{1<[Region]={'North'}>}"

    def test_the_current_selection_stated_plainly(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}],
            scope={"current_selection": True})
        assert result["modifier"] == "{$<[Region]={'North'}>}"









class TestElementSets:
    """Values of one field that satisfy a condition on another — what P()
    and E() answer. Verified live: clients who bought in 2023 summed to 60
    across all years, those who did not to 40, and "in 2023 but not 2024"
    to 30."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("2023", "2024", "Shoe"), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_matching_becomes_p(self):
        result = self._engine().build_filters(1, "app", [
            {"field": "Client",
             "matching": {"filters": [{"field": "Year", "values": ["2023"]}]}}])
        assert result["modifier"] == (
            "{<[Client]=P({1<[Year]={'2023'}>} [Client])>}")

    def test_not_matching_becomes_e(self):
        result = self._engine().build_filters(1, "app", [
            {"field": "Client",
             "not_matching": {"filters": [{"field": "Year", "values": ["2023"]}]}}])
        assert "E({1<[Year]={'2023'}>} [Client])" in result["modifier"]







class TestTextMatching:
    """Matching by text, written as a string comparison rather than as a
    Qlik wildcard search: there is no escape for `*`, `?` or a quote
    inside a search, so a value carrying one would become a different
    search."""

    @staticmethod
    def _engine():
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_contains(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Name", "contains": "smith"}])
        assert result["modifier"] == (
            '{<[Name]={"=Index(Upper([Name]), Upper(\'smith\'))>0"}>}')

    def test_starts_with(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Name", "starts_with": "A"}])
        assert "=1" in result["modifier"]







class TestConditionByExpression:
    """The escape hatch: a condition the vocabulary cannot state. The
    server wraps it and lets Qlik judge it; it does not read it."""

    @staticmethod
    def _engine():
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_the_condition_is_wrapped_as_a_search(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Year", "match_expression": "[Year]>2023"}])
        assert result["modifier"] == '{<[Year]={"=[Year]>2023"}>}'

    def test_a_condition_qlik_refuses_is_refused(self):
        """Measured on the server: inside a set modifier Qlik reads a
        broken condition as text and answers zero, so the condition is
        checked on its own, where Qlik does say what is wrong with it -
        "Sum(amount > 20" came back as "')' or ',' expected"."""
        class _Checking(_Engine):
            def check_expressions(self, app_handle, expressions):
                return {e: {"error": "')' or ',' expected", "bad_fields": []}
                        for e in expressions if "Sum([Amount] > 20" in e}

        engine = _Checking(values=("2023",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Year",
                        "match_expression": "Sum([Amount] > 20"}])
        assert result["error_category"] == "invalid_expression"
        assert "')'" in result["error"]




class TestOneConditionPerFilter:
    """Several kinds of condition in one filter is a contradiction, not a
    combination. Taking the first and dropping the rest answered a
    question nobody asked, with a plausible number to show for it."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North", "South"), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_text_and_values_together_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "contains": "North",
                        "values": ["South"]}])
        assert result["error_category"] == "invalid_filter"
        assert "contains" in result["error"] and "values" in result["error"]

    def test_an_element_set_and_a_range_together_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "F", "period": "2011",
                        "matching": {"filters": [{"field": "Region",
                                                  "values": ["North"]}]}}])
        assert result["error_category"] == "invalid_filter"











class TestExcludingWhatIsNotThere:
    """Excluding a value the field does not hold changes nothing and is
    not a mistake. Only the operators that keep what they name need the
    value to exist."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North", "South"), date_fields=("F",))
        return engine

    def test_excluding_an_absent_value_is_allowed(self):
        result = self._engine().values_modifier(
            1, "Region", ["Nowhere"], operator="exclude")
        assert "error" not in result

    def test_adding_an_absent_value_is_allowed(self):
        result = self._engine().values_modifier(
            1, "Region", ["Nowhere"], operator="add")
        assert "error" not in result






class TestAProbeRunsWhereTheQueryRuns:
    """A probe with no identifier is scored against the current selections,
    so a value a bookmark holds but the selections hide read as "the field
    has no such value" - a refusal of a query that would have answered."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.seen = []

        def evaluate(handle, exprs):
            engine.seen.extend(exprs)
            return [{"text": None, "number": 4, "is_numeric": True,
                     "error": None} for _ in exprs]

        engine.evaluate_expressions = evaluate
        return engine

    def test_a_value_is_checked_inside_the_scope(self):
        engine = self._engine()
        engine.build_filters(1, "app", [{"field": "Region",
                                         "values": ["North"]}],
                             scope={"bookmark": "BM01"})
        assert any("{BM01<" in probe for probe in engine.seen)

    def test_without_a_scope_the_probe_is_unchanged(self):
        engine = self._engine()
        engine.build_filters(1, "app", [{"field": "Region",
                                         "values": ["North"]}])
        assert engine.seen and all("{<" in p for p in engine.seen)















class TestAnElementSetSelectsSomething:
    """"The clients who bought in a year nobody bought in" narrows the
    field to nothing, and an empty answer reads as "no such data" rather
    than as "no such client"."""

    @staticmethod
    def _engine(matched):
        engine = _Engine(values=("2023",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": matched, "is_numeric": True,
             "error": None} for _ in exprs]
        return engine

    def test_a_condition_selecting_nothing_is_refused(self):
        result = self._engine(0).build_filters(1, "app", [
            {"field": "Client", "matching": {
                "filters": [{"field": "Year", "values": ["2023"]}]}}])
        assert result["error_category"] == "value_not_found"

    def test_a_condition_selecting_something_runs(self):
        result = self._engine(4).build_filters(1, "app", [
            {"field": "Client", "matching": {
                "filters": [{"field": "Year", "values": ["2023"]}]}}])
        assert "error" not in result
        assert "P(" in result["modifier"]




class TestSetsCombineWithEachOther:
    """"Bought in 2023 or lives in the South" is the union of two sets, and
    no modifier on one field says it. Measured on the server: two sets
    holding 40 and 60 answer 100 under union, 0 under intersect where they
    do not overlap, 40 under exclude and 100 under symmetric difference."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North", "South", "2023"), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    @pytest.mark.parametrize("operation, sign", [
        ("union", " + "), ("intersect", " * "),
        ("exclude", " - "), ("symmetric_difference", " / ")])
    def test_each_operation_is_written(self, operation, sign):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": operation, "of": [
                {"ignore_selections": True,
                 "filters": [{"field": "Region", "values": ["North"]}]},
                {"ignore_selections": True,
                 "filters": [{"field": "Region", "values": ["South"]}]}]})
        assert result["modifier"] == (
            "{(1<[Region]={'North'}>)" + sign + "(1<[Region]={'South'}>)}")

    def test_the_reply_names_what_each_set_narrowed(self):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [
                {"ignore_selections": True,
                 "filters": [{"field": "Region", "values": ["North"]}]},
                {"current_selection": True}]})
        assert [item["set"] for item in result["applied"]] == [0, 1]











































































