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

    def test_a_year_ends_in_december_as_an_upper_bound(self):
        """`to: "2024"` meaning the first of January would lose the year."""
        assert _parse_bound("2024", upper=True) == datetime.date(2024, 12, 31)

    def test_a_month_spans_from_its_first_day_to_its_last(self):
        assert _parse_bound("2024-02", upper=False) == datetime.date(2024, 2, 1)
        assert _parse_bound("2024-02", upper=True) == datetime.date(2024, 2, 29)

    def test_december_rolls_the_year_over_correctly(self):
        assert _parse_bound("2024-12", upper=True) == datetime.date(2024, 12, 31)

    @pytest.mark.parametrize("text", ["", None, "last tuesday", "24-1-1"])
    def test_anything_else_is_not_a_date(self, text):
        assert _parse_bound(text, upper=False) is None

    def test_the_serial_epoch_matches_qliks(self):
        assert _to_serial(datetime.date(2024, 1, 1)) == 45292


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

    def test_the_upper_bound_is_the_next_day_not_the_last(self):
        """A value at 31.12 23:59 is larger than 31.12, so `<=` drops it."""
        engine = _Engine()
        result = engine.period_modifier(1, "app", "F", "2011-01-01", "2011-01-01")
        assert result["serial_to_exclusive"] == result["serial_from"] + 1

    def test_the_period_carries_how_many_values_fall_in_it(self):
        engine = _Engine(days=(40544, 40545, 40546))
        result = engine.period_modifier(1, "app", "F", "2011", "2011")
        assert result["distinct_values_in_period"] == 3

    def test_a_period_holding_nothing_is_refused_before_the_query(self):
        engine = _Engine(days=(40544,))
        result = engine.period_modifier(1, "app", "F", "1990", "1991")
        assert result["error_category"] == "empty_period"
        assert "engine_get_field_range" in " ".join(result["next_actions"])

    def test_bounds_the_server_cannot_read_are_refused_with_the_forms(self):
        result = _Engine().period_modifier(1, "app", "F", "last tuesday", None)
        assert result["error_category"] == "invalid_period"
        assert result["accepted_forms"] == list(BOUND_FORMS)

    def test_reversed_bounds_are_refused_rather_than_swapped(self):
        """Which of the two the caller meant is the caller's to say."""
        engine = _Engine()
        result = engine.period_modifier(1, "app", "F", "2011-12-31", "2011-01-01")
        assert result["error_category"] == "invalid_period"
        assert "earlier" in result["error"]

    def test_a_single_day_needs_only_one_bound(self):
        engine = _Engine(days=(40544,))
        result = engine.period_modifier(1, "app", "F", "2011-01-01", None)
        assert result["from"] == result["to"] == "2011-01-01"


class TestFormIsRemembered:
    def test_the_second_period_on_a_field_costs_one_count(self):
        engine = _Engine()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        engine.asked.clear()
        engine.period_modifier(1, "app", "F", "2012", "2012")
        assert len(engine.asked) == 1

    def test_forgetting_puts_the_measurement_back(self):
        engine = _Engine()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        engine.forget_filter_forms("app")
        engine.asked.clear()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        assert len(engine.asked) > 1

    def test_a_remembered_form_that_selects_nothing_is_measured_again(self):
        """A reload can change how a field is stored; the remembered choice
        must not turn that into a zero."""
        engine = _Engine(numeric_text=True)
        engine.period_modifier(1, "app", "F", "2011", "2011")
        engine.numeric_text = False
        engine.asked.clear()
        result = engine.period_modifier(1, "app", "F", "2011", "2011")
        assert result["form"] == "expression"


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

    def test_the_refusal_names_the_field_in_brackets(self):
        """No suggestion: the model can list the values itself, and the
        search behind the old one cost about 2.5 seconds per refusal."""
        engine = _Engine(values=("Moskva",))
        result = engine.values_modifier(1, "Region", ["Moscow"])
        assert "[Region]" in result["error"]
        assert "did_you_mean" not in result
        assert any("get_app_field" in a for a in result["next_actions"])

    def test_an_empty_list_is_refused_rather_than_ignored(self):
        result = _Engine().values_modifier(1, "Region", [])
        assert result["error_category"] == "invalid_filter"

    @pytest.mark.parametrize("value, quoted", [
        ("North", "'North'"),
        ("O'Brien", "'O''Brien'"),
        (42, "'42'"),
    ])
    def test_a_quote_inside_a_value_is_escaped_qliks_way(self, value, quoted):
        assert quote_value(value) == quoted


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

    def test_a_filter_naming_no_field_is_refused(self):
        result = _Engine().build_filters(1, "app", [{"values": ["North"]}])
        assert result["error_category"] == "invalid_filter"

    def test_a_filter_that_is_neither_a_period_nor_values_is_refused(self):
        result = _Engine().build_filters(1, "app", [{"field": "Region"}])
        assert result["error_category"] == "invalid_filter"

    def test_asking_for_both_at_once_is_refused_with_the_way_out(self):
        result = _Engine().build_filters(
            1, "app", [{"field": "F", "period": "2011", "values": ["North"]}])
        assert result["error_category"] == "invalid_filter"
        assert "several filters" in result["hint"]

    def test_a_filter_that_is_not_an_object_is_refused_with_an_example(self):
        result = _Engine().build_filters(1, "app", ["Region=North"])
        assert result["error_category"] == "invalid_filter"
        assert "OrderDate" in result["hint"]

    def test_the_first_failing_filter_stops_the_query(self):
        engine = _Engine(days=(40544,))
        result = engine.build_filters(1, "app", [
            {"field": "F", "period": "1990"},
            {"field": "Region", "values": ["North"]},
        ])
        assert result["error_category"] == "empty_period"


class TestFormMemoryExpires:
    """A field's form follows how Qlik displays it, which a reload changes.

    A form that selects nothing is measured again on the spot. One that
    selects a wrong non-zero count would not be, so the memory expires
    rather than lasting the life of a process built to be long-lived.
    """

    def test_a_stale_memory_is_measured_again(self, monkeypatch):
        import qlik_sense_mcp_server.engine.filters as filters_module

        engine = _Engine()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        engine.asked.clear()
        clock = [filters_module.time.monotonic()
                 + engine.FILTER_FORM_TTL_SECONDS + 1]
        monkeypatch.setattr(filters_module.time, "monotonic", lambda: clock[0])
        engine.period_modifier(1, "app", "F", "2011", "2011")
        assert len(engine.asked) > 1

    def test_within_the_window_it_is_still_reused(self):
        engine = _Engine()
        engine.period_modifier(1, "app", "F", "2011", "2011")
        engine.asked.clear()
        engine.period_modifier(1, "app", "F", "2012", "2012")
        assert len(engine.asked) == 1


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

    def test_a_range_holding_nothing_is_refused(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 0, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(1, "app", [{"field": "price", "from": 1e9}])
        assert result["error_category"] == "empty_range"

    def test_a_whole_number_keeps_no_decimal_tail(self):
        """`400.0` is text Qlik has no value for."""
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 5, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(1, "app", [{"field": "price", "from": 400.0}])
        assert '">=400"' in result["modifier"]

    def test_a_bound_that_is_neither_a_date_nor_a_number_is_refused(self):
        engine = _Engine(date_fields=("F",))
        result = engine.build_filters(1, "app", [{"field": "price", "from": "cheap"}])
        assert result["error_category"] == "invalid_filter"

    def test_a_date_field_still_reads_its_bounds_as_days(self):
        engine = _Engine(date_fields=("F",))
        result = engine.build_filters(1, "app", [{"field": "F", "period": "2011"}])
        assert "40544" in result["modifier"]


class TestStrictBounds:
    """"More than 400" and "from 400" are different questions.

    Measured on a 10M-row app: 184 orders have a discount of exactly 400,
    which is the whole gap between 1 898 591 and 1 898 775 — and both
    numbers look equally plausible.
    """

    @staticmethod
    def _counting():
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 9, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_greater_than_excludes_the_bound(self):
        result = self._counting().build_filters(
            1, "app", [{"field": "price", "greater_than": 400}])
        assert result["modifier"] == '{<[price]={">400"}>}'

    def test_from_includes_it(self):
        result = self._counting().build_filters(
            1, "app", [{"field": "price", "from": 400}])
        assert result["modifier"] == '{<[price]={">=400"}>}'

    def test_less_than_excludes_the_upper_bound(self):
        result = self._counting().build_filters(
            1, "app", [{"field": "price", "less_than": 500}])
        assert result["modifier"] == '{<[price]={"<500"}>}'

    def test_the_two_can_be_combined(self):
        result = self._counting().build_filters(
            1, "app", [{"field": "price", "greater_than": 400, "to": 500}])
        assert result["modifier"] == '{<[price]={">400<=500"}>}'

    def test_which_end_was_excluded_is_reported(self):
        result = self._counting().build_filters(
            1, "app", [{"field": "price", "greater_than": 400}])
        assert result["applied"][0]["from_excluded"] is True


class TestContradictoryBounds:
    """Two bounds for the same end contradict each other; picking one
    silently hides half of what was asked for."""

    @staticmethod
    def _engine():
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_from_and_greater_than_together_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "price", "from": 400, "greater_than": 400}])
        assert result["error_category"] == "invalid_filter"
        assert "greater_than" in result["error"]

    def test_to_and_less_than_together_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "price", "to": 500, "less_than": 500}])
        assert result["error_category"] == "invalid_filter"

    def test_one_of_each_is_fine(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "price", "from": 400, "less_than": 500}])
        assert result["modifier"] == '{<[price]={">=400<500"}>}'


class TestEmptyValueInAList:
    def test_an_empty_value_is_named_rather_than_dropped(self):
        engine = _Engine(values=("North", "South"))
        result = engine.values_modifier(1, "Region", ["North", "", "South"])
        assert result["error_category"] == "invalid_filter"
        assert "position 1" in result["error"]

    def test_a_list_of_real_values_still_works(self):
        engine = _Engine(values=("North", "South"))
        result = engine.values_modifier(1, "Region", ["North", "South"])
        assert result["modifier"] == "[Region]={'North','South'}"


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

    def test_a_bookmark(self):
        result = self._engine().build_filters(1, "app", [], scope={"bookmark": "BM01"})
        assert result["modifier"] == "{BM01}"

    def test_a_bookmark_of_a_state(self):
        result = self._engine().build_filters(
            1, "app", [], scope={"state": "Compare", "bookmark": "BM01"})
        assert result["modifier"] == "{Compare::BM01}"

    def test_steps_back_through_the_selection_history(self):
        result = self._engine().build_filters(1, "app", [], scope={"selection_back": 2})
        assert result["modifier"] == "{$2}"

    def test_steps_forward(self):
        result = self._engine().build_filters(1, "app", [], scope={"selection_forward": 1})
        assert result["modifier"] == "{$_1}"

    def test_two_scopes_at_once_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [], scope={"bookmark": "BM01", "ignore_selections": True})
        assert result["error_category"] == "invalid_argument"

    def test_a_step_count_that_is_not_a_number_is_refused(self):
        result = self._engine().build_filters(
            1, "app", [], scope={"selection_back": "two"})
        assert result["error_category"] == "invalid_argument"

    def test_no_scope_is_the_plain_modifier(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}])
        assert result["modifier"] == "{<[Region]={'North'}>}"


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

    def test_both_together_subtract_one_possible_set_from_the_other(self):
        """P(a) - P(b), not P(a) - E(b): "possible under a minus excluded
        under b" is a different set, and only coincides on some data."""
        result = self._engine().build_filters(1, "app", [
            {"field": "Client",
             "matching": {"filters": [{"field": "Year", "values": ["2023"]}]},
             "not_matching": {"filters": [{"field": "Year", "values": ["2024"]}]}}])
        assert " - " in result["modifier"]
        assert result["modifier"].count("P(") == 2

    def test_the_answer_can_be_carried_to_another_field(self):
        result = self._engine().build_filters(1, "app", [
            {"field": "Customer",
             "matching": {"of_field": "Supplier",
                          "filters": [{"field": "Year", "values": ["2023"]}]}}])
        assert "[Customer]=P({1<[Year]={'2023'}>} [Supplier])" in result["modifier"]

    def test_asking_of_the_current_selection_instead_of_everything(self):
        result = self._engine().build_filters(1, "app", [
            {"field": "Client",
             "matching": {"base": "current",
                          "filters": [{"field": "Year", "values": ["2023"]}]}}])
        assert "P({<[Year]" in result["modifier"]

    def test_matching_without_filters_is_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Client", "matching": {}}])
        assert result["error_category"] == "invalid_filter"

    def test_a_base_that_is_neither_is_refused(self):
        result = self._engine().build_filters(1, "app", [
            {"field": "Client",
             "matching": {"base": "some", "filters": [{"field": "Year",
                                                       "values": ["2023"]}]}}])
        assert result["error_category"] == "invalid_filter"


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

    def test_ends_with(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Name", "ends_with": "Ltd"}])
        assert "Right(" in result["modifier"]

    def test_a_quote_in_the_value_is_escaped(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Name", "contains": "O'Brien"}])
        assert "'O''Brien'" in result["modifier"]

    def test_a_star_in_the_value_is_just_a_star(self):
        """As a wildcard search this would match everything."""
        result = self._engine().build_filters(
            1, "app", [{"field": "Name", "contains": "50%*off"}])
        assert "'50%*off'" in result["modifier"]

    def test_two_text_conditions_at_once_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Name", "contains": "a", "starts_with": "b"}])
        assert result["error_category"] == "invalid_filter"

    def test_an_empty_text_is_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Name", "contains": ""}])
        assert result["error_category"] == "invalid_filter"


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

    def test_a_brace_inside_a_string_is_not_a_broken_condition(self):
        """Counting characters refused this one; Qlik reads it fine."""
        result = self._engine().build_filters(
            1, "app", [{"field": "Year",
                        "match_expression": '[Year] <> "}"'}])
        assert "error" not in result

    def test_an_empty_condition_is_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Year", "match_expression": "   "}])
        assert result["error_category"] == "invalid_filter"


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

    def test_an_expression_and_values_together_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "match_expression": "1=1",
                        "values": ["North"]}])
        assert result["error_category"] == "invalid_filter"

    def test_values_and_exclude_together_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": ["North"],
                        "exclude": ["South"]}])
        assert result["error_category"] == "invalid_filter"

    def test_one_condition_still_works(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}])
        assert "error" not in result


class TestAKeyWithNoValueIsNotACondition:
    """`contains: null` used to search for the text "None"."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_a_null_text_filter_is_not_a_search_for_none(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "contains": None,
                        "values": ["North"]}])
        assert "error" not in result
        assert "None" not in result["modifier"]
        assert "'North'" in result["modifier"]

    def test_a_null_element_set_is_not_a_condition(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "matching": None,
                        "values": ["North"]}])
        assert "error" not in result


class TestBoundsBeyondTheCalendar:
    """A date written as 20240101 lands around the year 57000."""

    @pytest.mark.parametrize("bound", [20240101, 10 ** 12, -10 ** 12])
    def test_a_number_too_large_to_be_a_day_is_refused_as_a_bound(self, bound):
        assert _parse_bound(bound, upper=False) is None

    def test_the_refusal_carries_the_forms_it_reads(self):
        engine = _Engine(date_fields=("F",))
        result = engine.period_modifier(1, "app", "F", 20240101, None)
        assert result["error_category"] == "invalid_period"
        assert result["accepted_forms"]

    def test_an_ordinary_serial_number_still_reads(self):
        assert _parse_bound(45292, upper=False) is not None


class TestAKeyHoldingNothingStatesNothing:
    """The rule already applied to conditions; it applies to scope and to
    bounds as well. A caller that fills every key of its template with
    nulls should not be told it contradicted itself."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_a_null_beside_a_bound_is_not_a_conflict(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "price", "greater_than": 400, "from": None}])
        assert result["modifier"] == '{<[price]={">400"}>}'

    def test_a_null_beside_the_upper_bound_is_not_a_conflict(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "price", "less_than": 500, "to": None}])
        assert result["modifier"] == '{<[price]={"<500"}>}'

    def test_a_real_conflict_is_still_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "price", "from": 400, "greater_than": 400}])
        assert result["error_category"] == "invalid_filter"

    def test_a_null_scope_key_is_not_a_second_scope(self):
        result = self._engine().build_filters(
            1, "app", [], scope={"bookmark": "BM01", "state": None,
                                 "ignore_selections": False})
        assert result["modifier"] == "{BM01}"

    def test_a_state_with_its_bookmark_still_works(self):
        result = self._engine().build_filters(
            1, "app", [], scope={"state": "Compare", "bookmark": "BM01",
                                 "ignore_selections": None})
        assert result["modifier"] == "{Compare::BM01}"


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

    def test_keeping_an_absent_value_is_still_refused(self):
        result = self._engine().values_modifier(1, "Region", ["Nowhere"])
        assert result["error_category"] == "value_not_found"

    def test_intersecting_with_an_absent_value_is_refused(self):
        result = self._engine().values_modifier(
            1, "Region", ["Nowhere"], operator="intersect")
        assert result["error_category"] == "value_not_found"


class TestTheFieldAnElementSetReads:
    """`of_field` restricts another field, so an unknown name there would
    be scored as an expression and quietly select nothing."""

    @staticmethod
    def _engine(known=("Client", "Supplier", "Year")):
        engine = _Engine(values=("2023",), date_fields=("F",))
        engine.known = set(known)
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_an_unknown_of_field_is_refused(self):
        from qlik_sense_mcp_server.exceptions import QlikEngineError

        class _Checking(_Engine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetFieldDescription":
                    name = (params or [""])[0]
                    if name == "Nope":
                        raise QlikEngineError("Invalid parameters")
                    return {"qReturn": {"qName": name}}
                return {}

        engine = _Checking(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(1, "app", [
            {"field": "Client",
             "matching": {"of_field": "Nope",
                          "filters": [{"field": "Year", "values": ["2023"]}]}}])
        assert result["error_category"] == "field_not_found"
        assert "[Nope]" in result["error"]


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

    def test_a_period_is_measured_inside_the_scope(self):
        engine = self._engine()
        engine.build_filters(1, "app", [{"field": "F", "period": "2011"}],
                             scope={"ignore_selections": True})
        assert any("{1<" in probe for probe in engine.seen)


class TestAScopeKeyNobodyReads:
    """Silence here means the query runs over the current selections while
    the caller believes it runs over the history or over a bookmark."""

    @pytest.mark.parametrize("key", ["steps_back", "ignore_selection",
                                     "bookmark_id"])
    def test_an_unknown_key_is_refused(self, key):
        result = _Engine(date_fields=("F",)).build_filters(
            1, "app", [], scope={key: 1})
        assert result["error_category"] == "invalid_argument"
        assert "selection_back" in result["hint"]

    def test_the_keys_it_reads_still_work(self):
        result = _Engine(date_fields=("F",)).build_filters(
            1, "app", [], scope={"selection_back": 2})
        assert result["modifier"] == "{$2}"


class TestAnEmptyHalfOfAPair:
    @pytest.mark.parametrize("scope", [
        {"state": "", "bookmark": "BM01"},
        {"state": "Compare", "bookmark": "  "},
    ])
    def test_an_empty_half_is_refused(self, scope):
        result = _Engine(date_fields=("F",)).build_filters(1, "app", [],
                                                           scope=scope)
        assert result["error_category"] == "invalid_argument"

    def test_a_whole_pair_still_works(self):
        result = _Engine(date_fields=("F",)).build_filters(
            1, "app", [], scope={"state": "Compare", "bookmark": "BM01"})
        assert result["modifier"] == "{Compare::BM01}"


class TestAScopeThatIsNotAnObject:
    @pytest.mark.parametrize("scope", [0, "", False, [], "BM01", 5])
    def test_a_scope_that_is_not_an_object_is_refused(self, scope):
        result = _Engine(date_fields=("F",)).build_filters(1, "app", [],
                                                           scope=scope)
        assert result["error_category"] == "invalid_argument"

    def test_no_scope_at_all_is_still_fine(self):
        result = _Engine(date_fields=("F",)).build_filters(1, "app", [])
        assert "error" not in result


class TestTheFieldCheckReachesEngine:
    """The refusal on an unknown field name runs through `send_request`,
    and the two outcomes it tells apart - Engine saying "no such field" and
    the call itself failing - decide whether a query is refused or run."""

    class _Asked(_Engine):
        def __init__(self, *args, breaks=False, **kwargs):
            super().__init__(*args, **kwargs)
            self.asked = []
            self.breaks = breaks

        def send_request(self, method, params=None, handle=-1, timeout=None):
            if method == "GetFieldDescription":
                from qlik_sense_mcp_server.exceptions import QlikEngineError
                name = (params or [""])[0]
                self.asked.append(name)
                if self.breaks:
                    raise TimeoutError("the socket went away")
                if name != "Region":
                    raise QlikEngineError("Invalid parameters")
                return {"qReturn": {"qName": name}}
            return {}

    def test_an_unknown_name_is_refused(self):
        engine = self._Asked(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 1, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Regionn", "values": ["North"]}])
        assert result["error_category"] == "field_not_found"
        assert engine.asked == ["Regionn"]

    def test_a_failed_call_does_not_read_as_a_missing_field(self):
        engine = self._Asked(values=("North",), date_fields=("F",),
                             breaks=True)
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 1, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}])
        assert "error" not in result


class TestTheConditionNamesFieldsThatExist:
    """Qlik answers CheckExpression with two things, and a name it does not
    know is the second: a misspelled field is read as a value of itself, so
    the condition scores false everywhere and the answer is empty rather
    than wrong."""

    @staticmethod
    def _engine(bad=()):
        class _Checking(_Engine):
            def check_expressions(self, app_handle, expressions):
                faults = {}
                for expression in expressions:
                    named = [n for n in bad if n in expression]
                    if named:
                        faults[expression] = {"error": "",
                                              "bad_fields": named}
                return faults

        engine = _Checking(values=("2023",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_an_unknown_field_inside_the_condition_is_refused(self):
        result = self._engine(bad=("Amountt",)).build_filters(
            1, "app", [{"field": "Client",
                        "match_expression": "Sum([Amountt]) > 20"}])
        assert result["error_category"] == "field_not_found"
        assert "[Amountt]" in result["error"]

    def test_a_condition_on_real_fields_still_works(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Client",
                        "match_expression": "Sum([Amount]) > 20"}])
        assert "error" not in result


class TestTheConditionSelectsSomething:
    """The only kind of condition that used to run unproven."""

    @staticmethod
    def _engine(matched):
        engine = _Engine(values=("2023",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": matched, "is_numeric": True,
             "error": None} for _ in exprs]
        return engine

    def test_a_condition_matching_nothing_is_refused(self):
        result = self._engine(0).build_filters(
            1, "app", [{"field": "Client",
                        "match_expression": "Sum([Amount]) > 1000000"}])
        assert result["error_category"] == "value_not_found"

    def test_a_condition_matching_something_runs(self):
        result = self._engine(3).build_filters(
            1, "app", [{"field": "Client",
                        "match_expression": "Sum([Amount]) > 20"}])
        assert result["modifier"] == (
            '{<[Client]={"=Sum([Amount]) > 20"}>}')


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


class TestAProbeThatQlikRefuses:
    """A probe with no number has two causes. Qlik answering with the text
    of an error is a refusal to pass on; a probe that never ran says
    nothing about the query and must not be read as one."""

    @staticmethod
    def _engine(text):
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": text, "number": None, "is_numeric": False,
             "error": None} for _ in exprs]
        return engine

    def test_a_complaint_is_a_refusal(self):
        result = self._engine(
            "Error: Error in set modifier ad hoc element list: "
            "',' or ')' expected").build_filters(
            1, "app", [{"field": "Region", "contains": "no"}])
        assert result["error_category"] == "invalid_filter"
        assert "expected" in result["error"]

    def test_a_probe_that_did_not_run_is_not_a_refusal(self):
        result = self._engine(None).build_filters(
            1, "app", [{"field": "Region", "contains": "no"}])
        assert "error" not in result


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

    def test_filters_outside_the_combination_are_refused(self):
        """Measured: Qlik answers a modifier written around a combination
        with "'}' expected"."""
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}],
            scope={"combine": "union", "of": [
                {"ignore_selections": True}, {"current_selection": True}]})
        assert result["error_category"] == "invalid_filter"

    def test_one_set_is_not_a_combination(self):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [{"ignore_selections": True}]})
        assert result["error_category"] == "invalid_argument"

    @pytest.mark.parametrize("scope", [
        {"combine": "union"}, {"of": [{"ignore_selections": True}]}])
    def test_one_half_without_the_other_is_refused(self, scope):
        result = self._engine().build_filters(1, "app", [], scope=scope)
        assert result["error_category"] == "invalid_argument"

    def test_an_unknown_operation_is_refused(self):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "nearly", "of": [{"ignore_selections": True},
                                        {"current_selection": True}]})
        assert result["error_category"] == "invalid_argument"
        assert "union" in result["allowed_values"]

    def test_a_broken_set_inside_is_refused(self):
        """A set that cannot be built is not a set, and the combination
        carries its refusal out rather than joining what is left."""
        engine = self._engine()
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 0, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(1, "app", [], scope={
            "combine": "union", "of": [
                {"ignore_selections": True,
                 "filters": [{"field": "Region", "values": ["Nowhere"]}]},
                {"current_selection": True}]})
        assert result["error_category"] == "value_not_found"


class TestAProbeRunsInTheScopeEverywhere:
    """Proving a condition against the current selections both refuses good
    queries and lets empty ones through - the query runs somewhere else."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("2023",), date_fields=("F",))
        engine.seen = []

        def evaluate(handle, exprs):
            engine.seen.extend(exprs)
            return [{"text": None, "number": 3, "is_numeric": True,
                     "error": None} for _ in exprs]

        engine.evaluate_expressions = evaluate
        return engine

    def test_an_element_set_is_proven_inside_the_scope(self):
        engine = self._engine()
        engine.build_filters(1, "app", [
            {"field": "Client", "matching": {
                "filters": [{"field": "Year", "values": ["2023"]}]}}],
            scope={"bookmark": "BM01"})
        assert any(probe.startswith("=Count({BM01<[Client]=")
                   for probe in engine.seen)


class TestAComplaintIsARefusalOnEveryPath:
    """The rule reached four paths and skipped the two most common kinds of
    condition."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": "Error: Error in expression: ')' expected",
             "number": None, "is_numeric": False, "error": None}
            for _ in exprs]
        return engine

    def test_a_range_says_what_qlik_said(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "price", "greater_than": 400}])
        assert result["error_category"] == "invalid_filter"
        assert "expected" in result["error"]

    def test_a_period_says_what_qlik_said(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "F", "period": "2011"}])
        assert result["error_category"] == "invalid_period"
        assert "expected" in result["error"]


class TestWhatASetInACombinationMaySay:
    @staticmethod
    def _engine():
        engine = _Engine(values=("North", "South"), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    @pytest.mark.parametrize("stated", [{}, {"field": "Region"}, "Region", 5])
    def test_filters_that_are_not_a_list_are_refused(self, stated):
        """An object read as "no filters" widened the set to everything and
        the answer came back larger, with nothing to show for it."""
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [
                {"ignore_selections": True, "filters": stated},
                {"current_selection": True}]})
        assert result["error_category"] == "invalid_filter"

    def test_a_bookmark_named_with_a_brace_keeps_its_name(self):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [{"bookmark": "BM{1}"},
                                       {"current_selection": True}]})
        assert result["modifier"] == "{(BM{1}) + ($)}"

    def test_symmetric_difference_joins_two_sets(self):
        """Qlik applies it pairwise, so three sets would answer "in an odd
        number of them" - a different question."""
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "symmetric_difference", "of": [
                {"ignore_selections": True}, {"current_selection": True},
                {"bookmark": "BM01"}]})
        assert result["error_category"] == "invalid_argument"

    def test_the_others_take_more_than_two(self):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [
                {"ignore_selections": True}, {"current_selection": True},
                {"bookmark": "BM01"}]})
        assert result["modifier"] == "{(1) + ($) + (BM01)}"


class TestAQuestionThatNeverArrived:
    """Reading silence as "not a date" turns a period into a numeric range
    and answers a plausible wrong number - 18,774 where the truth was
    1,898,591."""

    def test_a_dropped_call_is_not_an_answer(self):
        from qlik_sense_mcp_server.exceptions import QlikProbeUnavailable

        class _Silent(_Engine):
            def get_field_description(self, app_handle, field):
                raise ConnectionError("WebSocket recv() failed")

        with pytest.raises(QlikProbeUnavailable):
            _Silent()._is_temporal_field(1, "OrderDate")

    def test_the_query_is_refused_rather_than_guessed(self):
        class _Silent(_Engine):
            def get_field_description(self, app_handle, field):
                raise ConnectionError("WebSocket recv() failed")

        result = _Silent().build_filters(
            1, "app", [{"field": "OrderDate", "period": "2011"}])
        assert result["error_category"] == "engine_api_error"

    def test_qlik_saying_no_is_an_answer(self):
        from qlik_sense_mcp_server.exceptions import QlikEngineError

        class _Answering(_Engine):
            def get_field_description(self, app_handle, field):
                return {}

            def send_request(self, method, params=None, handle=-1,
                             timeout=None):
                raise QlikEngineError("Invalid parameters")

        assert _Answering()._is_temporal_field(1, "price") is False


class TestEveryFormRefused:
    """Falling back to a form Qlik has already called unreadable sends out
    a filter that cannot work."""

    def test_a_period_refused_in_every_form_is_refused(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 5, "is_numeric": True, "error": None}
            if index == 0 else
            {"text": "Error: Error in expression: ')' expected",
             "number": None, "is_numeric": False, "error": None}
            for index, _ in enumerate(exprs)]
        result = engine.build_filters(1, "app", [{"field": "F",
                                                  "period": "2011"}])
        assert result["error_category"] == "invalid_period"


class TestAKeyBesideACombination:
    """Accepting a combination together with a bookmark dropped the
    bookmark and answered over a different set."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    @pytest.mark.parametrize("extra", [{"bookmark": "BM01"},
                                       {"ignore_selections": True},
                                       {"selection_back": 1}])
    def test_a_key_beside_it_is_refused(self, extra):
        scope = dict({"combine": "union", "of": [
            {"ignore_selections": True}, {"current_selection": True}]}, **extra)
        result = self._engine().build_filters(1, "app", [], scope=scope)
        assert result["error_category"] == "invalid_argument"

    def test_an_unknown_key_beside_it_is_refused(self):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [{"ignore_selections": True},
                                       {"current_selection": True}],
            "steps_back": 2})
        assert result["error_category"] == "invalid_argument"

    def test_the_combination_alone_still_works(self):
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [{"ignore_selections": True},
                                       {"current_selection": True}]})
        assert result["modifier"] == "{(1) + ($)}"


class TestAFormIsNotChosenOnSilence:
    """A call that failed reports through `error` rather than through the
    text of a value, and a form must not be picked on an answer that never
    came."""

    def test_a_failed_probe_is_not_agreement(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 5, "is_numeric": True, "error": None}
            if index == 0 else
            {"text": None, "number": None, "is_numeric": False,
             "error": "the socket went away"}
            for index, _ in enumerate(exprs)]
        result = engine.build_filters(1, "app", [{"field": "F",
                                                  "period": "2011"}])
        assert result["error_category"] == "invalid_period"


class TestAnEmptyKeyBesideACombination:
    """`{"bookmark": ""}` beside a combination is still a request nobody
    answered."""

    @pytest.mark.parametrize("extra", [{"bookmark": ""},
                                       {"ignore_selections": False},
                                       {"selection_back": 0}])
    def test_it_is_refused_too(self, extra):
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        scope = dict({"combine": "union", "of": [
            {"ignore_selections": True}, {"current_selection": True}]}, **extra)
        result = engine.build_filters(1, "app", [], scope=scope)
        assert result["error_category"] == "invalid_argument"


class TestNoReferenceNoForm:
    """The reference count is what every candidate form is measured
    against; without it a form would be chosen on nothing."""

    def test_a_failed_reference_refuses_the_period(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": None, "is_numeric": False,
             "error": "the socket went away"} for _ in exprs]
        result = engine.build_filters(1, "app", [{"field": "F",
                                                  "period": "2011"}])
        assert result["error_category"] == "invalid_period"


class TestAReferenceThatIsNotANumber:
    def test_a_period_without_a_count_is_refused(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": "something", "number": None, "is_numeric": False,
             "error": None} for _ in exprs]
        result = engine.build_filters(1, "app", [{"field": "F",
                                                  "period": "2011"}])
        assert result["error_category"] == "invalid_period"


class TestAKeyWrittenDownBesideACombination:
    @pytest.mark.parametrize("extra", [{"bookmark": None},
                                       {"selection_back": None}])
    def test_even_a_null_is_read(self, extra):
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 2, "is_numeric": True, "error": None}
            for _ in exprs]
        scope = dict({"combine": "union", "of": [
            {"ignore_selections": True}, {"current_selection": True}]}, **extra)
        result = engine.build_filters(1, "app", [], scope=scope)
        assert result["error_category"] == "invalid_argument"

    def test_a_list_of_sets_without_an_operation_is_named_as_such(self):
        engine = _Engine(values=("North",), date_fields=("F",))
        result = engine.build_filters(1, "app", [], scope={
            "of": [{"ignore_selections": True}], "bookmark": "BM"})
        assert "a list of sets" in result["error"]


class TestEveryKindOfFilterSaysWhenItWasNotChecked:
    """The mark reached one path of five: a text search, a range, an
    element set and a written condition answered as if Qlik had confirmed
    them."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": None, "is_numeric": False, "error": None}
            for _ in exprs]
        return engine

    @pytest.mark.parametrize("stated", [
        {"field": "Region", "contains": "no"},
        {"field": "price", "greater_than": 10},
        {"field": "Client", "matching": {
            "filters": [{"field": "Region", "values": ["North"]}]}},
    ])
    def test_the_note_is_carried(self, stated):
        result = self._engine().build_filters(1, "app", [stated])
        assert "could not be checked" in result["applied"][0]["note"]

    def test_a_checked_filter_carries_no_note(self):
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Region", "contains": "no"}])
        assert "note" not in result["applied"][0]


class TestAPeriodSaysWhenItsFormWasNotProven:
    """The form of a period is chosen by measuring; when no candidate
    agreed with the reference, the reply must not read like a confirmed
    filter."""

    def test_the_note_is_carried(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 5, "is_numeric": True, "error": None}
            if index == 0 else
            {"text": None, "number": 1, "is_numeric": True, "error": None}
            for index, _ in enumerate(exprs)]
        result = engine.build_filters(1, "app", [{"field": "F",
                                                  "period": "2011"}])
        assert "could not be measured" in result["applied"][0]["note"]

    def test_an_agreed_form_carries_no_note(self):
        engine = _Engine(date_fields=("F",))
        result = engine.build_filters(1, "app", [{"field": "F",
                                                  "period": "2011"}])
        assert "note" not in result["applied"][0]


class TestABoundThatShiftsWhenWritten:
    """Not a size limit: 2**54 is written exactly, while 2**53 + 1 comes
    back one less than it went in and the filter takes in a neighbouring
    row."""

    @pytest.mark.parametrize("bound", [2 ** 53 + 1, -(2 ** 53) - 1,
                                       10 ** 300])
    def test_it_is_refused(self, bound):
        result = _Engine(values=("North",)).build_filters(
            1, "app", [{"field": "price", "greater_than": bound}])
        assert result["error_category"] == "invalid_filter"

    def test_an_ordinary_bound_still_works(self):
        engine = _Engine(values=("North",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "price", "greater_than": 400}])
        assert "error" not in result


class TestAFormNobodyMeasured:
    """A candidate Qlik answered with neither a count nor a complaint was
    not measured, and choosing it puts an unproven form into the query."""

    def test_an_unmeasured_candidate_is_not_chosen(self):
        engine = _Engine(date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 5, "is_numeric": True, "error": None}
            if index == 0 else
            {"text": None, "number": None, "is_numeric": False, "error": None}
            for index, _ in enumerate(exprs)]
        result = engine.build_filters(1, "app", [{"field": "F",
                                                  "period": "2011"}])
        assert result["error_category"] == "invalid_period"


class TestABoundWrittenAsText:
    """A bound may be written as text - a documented form - and the number
    inside it shifts exactly the same way."""

    @pytest.mark.parametrize("bound", ["9007199254740993", " 1e300 ",
                                       "9007199254740993.0",
                                       "9.007199254740993e15"])
    def test_it_is_measured_too(self, bound):
        result = _Engine(values=("North",)).build_filters(
            1, "app", [{"field": "price", "greater_than": bound}])
        assert result["error_category"] == "invalid_filter"

    def test_an_ordinary_text_bound_still_works(self):
        engine = _Engine(values=("North",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "price", "greater_than": "400"}])
        assert "error" not in result

    def test_text_that_is_not_a_number_is_left_to_the_rest(self):
        engine = _Engine(values=("North",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "price", "greater_than": "yesterday"}])
        # Refused for what it is, not for how large it is.
        assert "neither dates nor numbers" in result["error"]


class TestAPeriodNamesBothEnds:
    """A bound beside a period replaces one of its ends, and the period
    quietly widens to take in rows nobody asked for."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    @pytest.mark.parametrize("bound", ["from", "to", "greater_than",
                                       "less_than"])
    def test_a_bound_beside_it_is_refused(self, bound):
        result = self._engine().build_filters(
            1, "app", [dict({"field": "F", "period": "2011"},
                            **{bound: "2011-06-01"})])
        assert result["error_category"] == "invalid_filter"

    def test_a_period_alone_still_works(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "F", "period": "2011"}])
        assert "error" not in result


class TestANumberTooLargeForADouble:
    """Past what a double holds, `float()` raises rather than rounds - and
    an unhandled raise is not the refusal this reading promises."""

    @pytest.mark.parametrize("bound", [10 ** 400, -(10 ** 400)])
    def test_it_is_refused_by_name(self, bound):
        result = _Engine(values=("North",)).build_filters(
            1, "app", [{"field": "price", "greater_than": bound}])
        assert result["error_category"] == "invalid_filter"

    def test_the_same_written_as_text(self):
        result = _Engine(values=("North",)).build_filters(
            1, "app", [{"field": "price", "greater_than": "1" + "0" * 400}])
        assert result["error_category"] == "invalid_filter"


class TestANoteFromInsideAnElementSet:
    """A period whose form went unmeasured inside `matching` picks its rows
    just as blindly as one at the top."""

    def test_the_note_is_carried_out(self):
        engine = _Engine(values=("2023",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 5, "is_numeric": True, "error": None}
            if index == 0 else
            {"text": None, "number": 1, "is_numeric": True, "error": None}
            for index, _ in enumerate(exprs)]
        result = engine.build_filters(1, "app", [
            {"field": "Client",
             "matching": {"filters": [{"field": "F", "period": "2011"}]}}])
        assert "could not be measured" in result["applied"][0]["note"]


class TestNoWayPastTheCeilings:
    """Every ceiling here guards the same thing: what Qlik has to read on
    the connection every query shares. A way around one of them is a way
    around all of them."""

    @staticmethod
    def _engine():
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        return engine

    def test_depth_is_not_a_way_around_length(self):
        """Unmeasured depth used to answer "nothing here", so a value of
        any length passed by being wrapped in enough lists."""
        from qlik_sense_mcp_server.engine.filters import MAX_FILTER_DEPTH

        buried = ["x" * 5000]
        for _ in range(MAX_FILTER_DEPTH + 5):
            buried = [buried]
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": buried}])
        assert result["error_category"] == "limit_exceeded"

    def test_a_bookmark_name_is_weighed_too(self):
        """It goes into the same modifier as the values do."""
        result = self._engine().build_filters(
            1, "app", [], scope={"bookmark": "b" * 5000})
        assert result["error_category"] == "limit_exceeded"

    def test_numbers_are_weighed_like_text(self):
        """A number reaches Qlik as the text of that number. Measured on
        its own: few enough values to pass that ceiling, long enough as
        text to pass this one."""
        from qlik_sense_mcp_server.engine.filters import MAX_VALUE_CHARS

        result = self._engine().build_filters(
            1, "app", [{"field": "Region",
                        "values": [int("9" * (MAX_VALUE_CHARS + 1))]}])
        assert result["error_category"] == "limit_exceeded"
        assert "characters" in result["error"]

    def test_a_combination_is_weighed_whole(self):
        heavy = {"ignore_selections": True, "filters": [
            {"field": "Region", "values": ["y" * 1000 for _ in range(15)]}]}
        result = self._engine().build_filters(1, "app", [], scope={
            "combine": "union", "of": [dict(heavy) for _ in range(3)]})
        assert result["error_category"] == "limit_exceeded"

    def test_an_ordinary_filter_passes_all_of_them(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}],
            scope={"bookmark": "BM01"})
        assert "error" not in result


class TestTheCeilingCostsNothingToEnforce:
    """Walking a collection of arbitrary size to find out that it is too
    large costs exactly what the ceiling exists to prevent."""

    def test_a_huge_list_is_refused_without_walking_it(self):
        import time

        engine = _Engine(values=("North",), date_fields=("F",))
        started = time.monotonic()
        result = engine.build_filters(
            1, "app", [{"field": "Region", "values": ["x"] * 3_000_000}])
        assert result["error_category"] == "limit_exceeded"
        assert time.monotonic() - started < 1.0

    def test_the_stated_number_of_values_is_allowed(self):
        """The field name and the scope are not values."""
        from qlik_sense_mcp_server.engine.filters import MAX_FILTER_VALUES

        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Region",
                        "values": ["North"] * MAX_FILTER_VALUES}],
            scope={"bookmark": "BM01"})
        assert "error" not in result


class TestQuotingMakesAValueLonger:
    """A value counted as short can arrive long: the ceiling is about what
    Qlik reads, so the built modifier is measured as well."""

    def test_the_built_modifier_is_measured(self):
        engine = _Engine(values=("North",), date_fields=("F",))
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 4, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(1, "app", [
            {"field": "Region", "values": ["'" * 4000 for _ in range(3)]}])
        assert result["error_category"] == "limit_exceeded"
        assert "modifier" in result["error"]
