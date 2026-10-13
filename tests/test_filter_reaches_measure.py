"""A filter marked next to a set of the measure's own reaches the figure.

Measured on a 1.16-billion-row app: `Sum({<Date=...2025...>} {<Flag={1}>}
Stakes)` - the filter as one set, the author's as another, side by side in
one aggregation - answered the sum over two years, while `filters_applied`
reported 365 days and `warnings` was empty. Qlik reads one of two sets
standing side by side and drops the other without a word. The filter is
now merged into the author's set, a shape that cannot be merged is
refused, and the period check reads the field through the measure's own
sets, so a filter that did not reach the figure is said.
"""

import pytest

from qlik_sense_mcp_server.engine.outer_set import (
    FilterPlacementError, element_set_warning, flat_set, has_marker,
    holds_filter, place_filter, set_contexts)
from tests.test_typed_queries import _Engine, _query

PERIOD = '{<[OrderDate]={">=01.01.2011<01.01.2012"}>}'


class TestMerge:
    @pytest.mark.parametrize("written, placed", [
        # The shape of the lost filter: marker, then the author's set.
        ("Sum({filter} {<Flag={1}>} Amount)",
         'Sum({<[OrderDate]={">=01.01.2011<01.01.2012"},Flag={1}>} Amount)'),
        ("sum({filter}{<Flag={1}>} Amount)",
         'sum({<[OrderDate]={">=01.01.2011<01.01.2012"},Flag={1}>} Amount)'),
        # The marker and the author's set in one pair of braces.
        ("Sum({filter <Flag={1},Kind={'A'}>} Amount)",
         'Sum({<[OrderDate]={">=01.01.2011<01.01.2012"},Flag={1},Kind={\'A\'}>} Amount)'),
        # A keyword between them: the set goes first, as Qlik reads it.
        ("Count({filter} DISTINCT {<Flag={1}>} Client)",
         'Count({<[OrderDate]={">=01.01.2011<01.01.2012"},Flag={1}>} DISTINCT Client)'),
        # An identifier of the author's own is kept.
        ("Sum({filter} {1<Flag={1}>} Amount)",
         'Sum({1<[OrderDate]={">=01.01.2011<01.01.2012"},Flag={1}>} Amount)'),
        # Inside P(): the set P() counts over takes it.
        ("Sum({<Client=P({filter} Client)>} Amount)",
         'Sum({<Client=P({<[OrderDate]={">=01.01.2011<01.01.2012"}>} Client)>} Amount)'),
    ])
    def test_the_filter_joins_the_set_beside_it(self, written, placed):
        assert has_marker(written)
        assert place_filter(written, PERIOD) == placed

    def test_an_outer_marker_before_a_set_of_its_own_is_one_outer_set(self):
        assert place_filter("{filter} {<Flag={1}>} [Revenue]", PERIOD) == (
            '{&<[OrderDate]={">=01.01.2011<01.01.2012"},Flag={1}>} [Revenue]')

    @pytest.mark.parametrize("written, said", [
        ("Sum({filter} {<OrderDate={'x'}>} Amount)", "[OrderDate]"),
        ("Sum({filter} {1-$} Amount)", "operator"),
        ("Sum({filter} {<A={1}>} {<B={2}>} Amount)", "2 sets"),
        ("Sum({filter} {BM01<A={1}>} Amount)", "{BM01}"),
    ])
    def test_what_cannot_be_merged_is_refused(self, written, said):
        modifier = PERIOD if said != "{BM01}" else "{1<[Region]={'North'}>}"
        with pytest.raises(FilterPlacementError, match=said.replace("[", r"\[")
                           .replace("]", r"\]").replace("{", r"\{").replace("}", r"\}")):
            place_filter(written, modifier)

    def test_no_filter_takes_the_marker_out_and_keeps_the_set(self):
        assert place_filter("Sum({filter <Flag={1}>} Amount)", "") == (
            "Sum({<Flag={1}>} Amount)")
        assert place_filter("Sum({filter} Amount)", "") == "Sum(Amount)"


class TestContexts:
    def test_each_aggregation_is_read_through_its_own_sets(self):
        placed = place_filter("{filter} Sum({<Flag={1}>} A) / Count(B)", PERIOD)
        outer = '{&<[OrderDate]={">=01.01.2011<01.01.2012"}>}'
        assert set_contexts(placed) == [(outer, "{<Flag={1}>}"), (outer, "")]
        assert all(holds_filter(c, PERIOD) for c in set_contexts(placed))

    def test_a_filter_inside_p_does_not_narrow_the_aggregation(self):
        placed = place_filter("Sum({<Client=P({filter} Client)>} Amount)", PERIOD)
        [context] = set_contexts(placed)
        assert not holds_filter(context, PERIOD)

    def test_the_inner_set_inherits_the_outer_one(self):
        assert flat_set(("{&<A={1},B={2}>}", "{<B={3}>}")) == ("", ["A={1}", "B={3}"])
        assert flat_set(("{&<A={1}>}", "{1<B={3}>}")) == ("1", ["B={3}"])


class _Dropping(_Engine):
    """Answers the period's ends as Qlik did for a measure whose own set
    kept the filter out: the whole range of the field."""

    def __init__(self, drop_when, **kwargs):
        super().__init__(known=("Region", "Amount", "OrderDate", "Flag"), **kwargs)
        self.drop_when = drop_when
        self.probes = []

    def _evaluate(self, expression):
        self.probes.append(expression)
        if self.drop_when in expression and ("Min(" in expression or "Max(" in expression):
            value = 40000 if "Min(" in expression else 41000
            if "Text(" in expression:
                return {"qText": f"day-{value}", "qNumber": "NaN"}
            return {"qText": str(value), "qNumber": value, "qIsNumeric": True}
        return super()._evaluate(expression)


class TestPeriodCheckReadsTheMeasure:
    def test_the_probe_carries_the_measures_own_set(self):
        engine = _Dropping(drop_when="never")
        reply = engine.run_queries(1, "app", [_query(
            metrics=[], measures=[{"expression": "Sum({filter} {<Flag={1}>} Amount)",
                                   "label": "Stakes"}],
            filters=[{"field": "OrderDate", "period": "2011"}])])["results"][0]
        [check] = reply["period_check"]
        assert check["filter_applied"] is True and check["measure"] == "Stakes"
        assert any("Flag={1}" in probe and "Min(" in probe for probe in engine.probes)

    def test_a_filter_the_measure_does_not_read_is_said_first(self):
        engine = _Dropping(drop_when="Flag={1}")
        result = engine.run_queries(1, "app", [_query(
            metrics=[], measures=[{"expression": "Sum({filter} {<Flag={1}>} Amount)",
                                   "label": "Stakes"}],
            filters=[{"field": "OrderDate", "period": "2011"}])])
        reply = result["results"][0]
        assert reply["period_check"][0]["filter_applied"] is False
        assert reply["warnings"][0].startswith("Measure 'Stakes' reads values of OrderDate")
        assert result["warnings"][0].startswith("q1: Measure 'Stakes'")
        assert reply["numbers_verified"] is False

    def test_a_measure_with_no_marker_takes_the_filter_in_front(self):
        """Refused before; a model spent three calls on one question."""
        plan = _Engine(known=("Region", "Amount", "OrderDate", "Flag"))._plan_query(
            1, "app", _query(metrics=[], measures=[
                {"expression": "Sum({<Flag={1}>} Amount)", "label": "Stakes"}],
                filters=[{"field": "Region", "values": ["North"]}]), 0)
        assert plan["measures"][0]["expression"] == (
            "{&<[Region]={'North'}>} Sum({<Flag={1}>} Amount)")
        assert "'Stakes' marks no place for the filters" in plan["notes"][0]
        assert "warnings" not in plan

    def test_a_note_leaves_the_numbers_verified(self):
        """Only a doubt about the figures may say they are unverified; where
        the filter went is a note."""
        reply = _Engine(known=("Region", "Amount", "OrderDate", "Flag")).run_queries(
            1, "app", [_query(metrics=[], measures=[
                {"expression": "Sum({<Flag={1}>} Amount)", "label": "Stakes"}],
                filters=[{"field": "OrderDate", "period": "2011"}])])["results"][0]
        assert "marks no place" in reply["notes"][0]
        assert "numbers_verified" not in reply and "warnings" not in reply

    def test_a_shape_that_cannot_take_the_filter_is_refused(self):
        reply = _Engine().run_queries(1, "app", [_query(
            metrics=[], measures=[{"expression": "Sum({filter} {<OrderDate={'x'}>} Amount)",
                                   "label": "Stakes"}],
            filters=[{"field": "OrderDate", "period": "2011"}])])["results"][0]
        assert reply["error_category"] == "invalid_argument"
        assert "[OrderDate]" in reply["error"]


class _Cube(_Dropping):
    """The same Engine for engine_create_hypercube, which sends its object
    one request at a time."""

    ws = object()

    def send_request(self, method, params=None, handle=-1, timeout=None):
        if method == "GetFieldDescription":
            return {"qReturn": {"qName": params[0]}}
        return self._reply({"method": method, "params": params, "handle": handle})


def _cube(engine, measures, filters):
    return engine.create_hypercube(
        1, [{"field": "Region"}], measures, 10, filters=filters)


class TestHypercube:
    def test_the_merged_set_is_sent_and_checked(self):
        engine = _Cube(drop_when="never")
        reply = _cube(engine, [{"expression": "Sum({filter} {<Flag={1}>} Amount)",
                                "label": "Stakes"}],
                      [{"field": "OrderDate", "period": "2011"}])
        assert "Flag={1}>} Amount)" in reply["measures"][0]["expression"]
        assert "} {<" not in reply["measures"][0]["expression"]
        assert reply["period_check"][0]["filter_applied"] is True

    def test_a_dropped_filter_leads_the_warnings(self):
        engine = _Cube(drop_when="Flag={1}")
        reply = _cube(engine, [{"expression": "Sum({filter} {<Flag={1}>} Amount)",
                                "label": "Stakes"}],
                      [{"field": "OrderDate", "period": "2011"}])
        assert list(reply)[0] == "warnings"
        assert reply["warnings"][0].startswith("Measure 'Stakes' reads values of OrderDate")

    def test_filters_without_a_marker_go_in_front_of_every_measure(self):
        """Seven calls of one evaluation were refused for this; the
        filters now reach every measure as an outer set, as they reach a
        master measure, and the reply says so."""
        engine = _Cube(drop_when="never")
        reply = _cube(engine, [{"expression": "Sum({<Flag={1}>} Amount)", "label": "A"},
                               {"expression": "Count(Amount)", "label": "B"}],
                      [{"field": "Region", "values": ["North"]}])
        assert [m["expression"] for m in reply["measures"]] == [
            "{&<[Region]={'North'}>} Sum({<Flag={1}>} Amount)",
            "{&<[Region]={'North'}>} Count(Amount)"]
        assert "No measure marks where the filters go" in reply["notes"][0]
        assert not reply["warnings"]

    def test_a_marker_on_one_measure_leaves_the_other_alone(self):
        engine = _Cube(drop_when="never")
        reply = _cube(engine, [{"expression": "Sum({filter} Amount)", "label": "A"},
                               {"expression": "Sum(TOTAL Amount)", "label": "All"}],
                      [{"field": "Region", "values": ["North"]}])
        assert reply["measures"][1]["expression"] == "Sum(TOTAL Amount)"


def test_a_filter_that_stops_short_of_p_is_named():
    placed = place_filter(
        "Count({filter} DISTINCT {<Client=P({<Sport={'A'}>} Client)>} Client)", PERIOD)
    warning = element_set_warning("Both", placed, PERIOD)
    assert "'Both'" in warning and "P() or E()" in warning
    assert element_set_warning("One", place_filter(
        "Sum({<Client=P({filter} Client)>} Amount)", PERIOD), PERIOD) is None
