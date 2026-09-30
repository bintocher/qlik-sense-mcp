"""Filters over master measures: an outer set in front of the measure.

A set written before a whole expression reaches every aggregation in it,
a master measure named by its label included. Measured on a model of 1.16
billion rows, the outer set over a master measure answered the reference
figure exactly, and so did the same set over the measure's expression in
parentheses. What keeps it out is read from the measure itself and said
by name: a field the measure sets (its own condition won, measured), a set
with an identifier, and `$(=...)`.
"""

import pytest

from qlik_sense_mcp_server.engine.outer_set import (
    filter_limits, in_front, limit_warnings, master_reference,
    outer_modifier, place_filter, set_expressions)
from tests.test_typed_queries import _Engine, _query


PERIOD = '{<[OrderDate]={">=40544<40909"}>}'


class TestEightForms:
    """What each shape of measure lets through, and what the caller is told."""

    @pytest.mark.parametrize("expression", [
        "Sum(Amount)",
        "[Revenue] / [Orders]",
        "Sum(Amount) / Sum(TOTAL Amount)",
        "Avg(Aggr(Sum(Amount), Client))",
    ])
    def test_nothing_keeps_the_filter_out(self, expression):
        assert filter_limits(expression) == {}
        assert limit_warnings("M", filter_limits(expression), ["OrderDate"]) == []

    def test_a_set_with_identifier_one_ignores_the_outer_set(self):
        limits = filter_limits("Sum({1<Year={2024}>} Amount)")
        assert limits["own_set"] == ["1"]
        [warning] = limit_warnings("All years", limits, ["OrderDate"])
        assert "'All years'" in warning and "{1<...>}" in warning

    def test_a_set_with_the_dollar_identifier_ignores_it_too(self):
        limits = filter_limits("Sum({$<Region={'North'}>} Amount)")
        assert limits["own_set"] == ["$"]
        assert limits["fields"] == ["Region"]

    def test_a_dollar_equals_is_computed_before_the_filter(self):
        limits = filter_limits("Sum(Amount) / $(=Sum(TOTAL Amount))")
        assert limits == {"computed_first": True}
        [warning] = limit_warnings("Share", limits, ["OrderDate"])
        assert "$(=...)" in warning

    def test_a_field_the_measure_sets_itself_wins(self):
        expression = "Sum({<Flag={1}, [Order Date]={\">=1\"}>} Amount)"
        limits = filter_limits(expression)
        assert limits["fields"] == ["Flag", "Order Date"]
        assert limit_warnings("GGR", limits, ["Region"]) == []
        [warning] = limit_warnings("GGR", limits, ["Flag"])
        assert "[Flag]" in warning and "'GGR'" in warning

    def test_element_lists_and_strings_are_not_sets(self):
        found = set_expressions(
            "Sum({<A={'x{y}'}, B={\"<5\"}>} [C{1}]) & '{<Z={1}>}'")
        assert found == [("", "A={'x{y}'}, B={\"<5\"}")]


class TestPlacement:
    def test_the_outer_set_keeps_an_empty_result_empty(self):
        assert outer_modifier(PERIOD) == '{&' + PERIOD[1:]
        assert outer_modifier("{1<[A]={1}>}") == "{1<[A]={1}>}"

    def test_the_marker_in_front_becomes_an_outer_set(self):
        placed = place_filter("{filter} [Revenue] / [Orders]", PERIOD)
        assert placed == "{&" + PERIOD[1:] + " [Revenue] / [Orders]"

    def test_an_expression_with_its_own_outer_set_is_merged_into_one(self):
        """Two sets side by side are one set too many for Qlik: the filter
        and the author's set become one."""
        placed = place_filter(
            "={filter} {<LT_Flag={1}>} Fractile(Aggr(Sum(d), Id), 0.85)",
            PERIOD)
        assert placed == ('{&<[OrderDate]={">=40544<40909"},LT_Flag={1}>} '
                          'Fractile(Aggr(Sum(d), Id), 0.85)')

    def test_a_marker_inside_an_aggregation_stays_inner(self):
        assert place_filter("Sum({filter} Amount)", PERIOD) == \
            "Sum(" + PERIOD + " Amount)"

    def test_a_master_measure_is_named_by_its_label(self):
        assert master_reference("GGR", "Sum(x)", {"Amount"}) == "[GGR]"

    def test_a_label_that_is_also_a_field_is_written_out(self):
        assert master_reference("Amount", "=Sum(Amount)", {"Amount"}) == "(Sum(Amount))"
        assert master_reference("A]B", "Sum(x)") == "(Sum(x))"

    def test_in_front_drops_the_leading_equals(self):
        assert in_front(PERIOD, "=[GGR]").endswith("} [GGR]")


class _LibraryEngine(_Engine):
    LIBRARY = {"measures": [
        {"id": "m1", "name": "Revenue",
         "expression": "Sum({<Flag={1}>} Amount)"},
        {"id": "m2", "name": "All time", "expression": "Sum({1} Amount)"},
    ]}

    def _library_for(self, app_handle, app_id):
        return self.LIBRARY


class TestEngineQueryMasterMeasures:
    def test_a_filtered_master_measure_takes_an_outer_set(self):
        engine = _LibraryEngine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[], measures=[{"master": "revenue"}],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        measure = plan["measures"][0]
        assert measure["expression"] == "{&<[Region]={'North'}>} [Revenue]"
        assert measure["label"] == "Revenue"
        assert "warnings" not in plan

    def test_the_measure_is_found_by_its_id_under_its_current_name(self):
        engine = _LibraryEngine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[], measures=[{"master": "m1"}],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        assert plan["measures"][0]["expression"].endswith("[Revenue]")

    def test_a_filter_on_a_field_the_measure_sets_is_named(self):
        engine = _LibraryEngine()
        engine.known.add("Flag")
        plan = engine._plan_query(1, "app", _query(
            metrics=[], measures=[{"master": "Revenue"}],
            filters=[{"field": "Flag", "values": ["North"]}]), 0)
        assert any("[Flag]" in w and "'Revenue'" in w for w in plan["warnings"])

    def test_a_measure_over_its_own_set_is_named(self):
        engine = _LibraryEngine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[], measures=[{"master": "All time"}],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        assert any("'All time'" in w for w in plan["warnings"])

    def test_the_warning_reaches_the_reply(self):
        engine = _LibraryEngine()
        # Qlik's own check knows a master measure by its label.
        engine.known.add("All time")
        result = engine.run_queries(1, "app", [_query(
            metrics=[], measures=[{"master": "All time"}],
            filters=[{"field": "Region", "values": ["North"]}])])
        reply = result["results"][0]
        assert any("'All time'" in w for w in reply["warnings"])

    def test_without_filters_the_expression_is_the_librarys(self):
        plan = _LibraryEngine()._plan_query(1, "app", _query(
            metrics=[], measures=[{"master": "Revenue"}]), 0)
        assert plan["measures"][0]["expression"] == "Sum({<Flag={1}>} Amount)"

    def test_a_hand_written_marker_in_front_is_an_outer_set(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[], measures=[{"expression": "{filter} Sum(Amount) / Count(Amount)",
                                   "label": "avg"}],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        assert plan["measures"][0]["expression"] == (
            "{&<[Region]={'North'}>} Sum(Amount) / Count(Amount)")


class TestDetailsCarryTheLimits:
    def test_measure_entries_carry_filter_limits(self):
        from qlik_sense_mcp_server.tools.overview import measure_entries
        entries = measure_entries([
            {"name": "GGR", "expression": "Sum({<Flag={1}>} Amount)"},
            {"name": "Plain", "expression": "Sum(Amount)"},
        ], {"Amount", "Flag"})
        by_name = {e["name"]: e for e in entries}
        assert by_name["GGR"]["filter_limits"] == {"fields": ["Flag"]}
        assert "filter_limits" not in by_name["Plain"]

def test_the_marker_is_not_a_set_of_its_own():
    assert filter_limits("{filter} Sum({<Flag={1}>} Amount)") == {"fields": ["Flag"]}
