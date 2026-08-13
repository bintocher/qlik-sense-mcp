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
        assert "two filters" in result["hint"]

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

    def test_an_unbalanced_quote_is_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Year", "match_expression": '[Year]>"2023'}])
        assert result["error_category"] == "invalid_filter"

    def test_unbalanced_braces_are_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Year",
                        "match_expression": "Sum({<A={1}>ance"}])
        assert result["error_category"] == "invalid_filter"

    def test_an_empty_condition_is_refused(self):
        result = self._engine().build_filters(
            1, "app", [{"field": "Year", "match_expression": "   "}])
        assert result["error_category"] == "invalid_filter"
