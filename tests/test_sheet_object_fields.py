"""Which fields a sheet object uses.

Engine describes a chart with a *list* of `qDimensionInfo` and a filter pane
with exactly one — a list object holds a single dimension by definition. The
extractor treated both as lists, so on a filter pane it iterated the dict and
walked its keys: `'str' object has no attribute 'get'`, caught by a
catch-all, logged as a warning, and the object reported no fields at all.

Filter panes are the objects that say which fields a sheet lets you slice
by, so the answer to "what does this sheet work with" was missing exactly
the part a reader would look for first.
"""

import logging

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


@pytest.fixture
def engine():
    return QlikEngineAPI.__new__(QlikEngineAPI)


def _list_object(field_name):
    """A filter pane as Engine actually returns it: one dimension, not a list."""
    return {
        "qListObject": {
            "qSize": {"qcx": 1, "qcy": 12},
            "qDimensionInfo": {
                "qFallbackTitle": field_name,
                "qGroupFieldDefs": [field_name],
            },
        }
    }


class TestListObjects:
    def test_filter_pane_field_is_found(self, engine):
        assert engine._extract_fields_from_object(_list_object("Region")) == ["Region"]

    def test_no_warning_is_logged(self, engine, caplog):
        """The defect was visible only as a log line; assert on it directly."""
        with caplog.at_level(logging.WARNING):
            engine._extract_fields_from_object(_list_object("Region"))
        assert not [r for r in caplog.records if "extracting fields" in r.message]

    def test_a_list_of_dimensions_is_tolerated(self, engine):
        layout = {"qListObject": {"qDimensionInfo": [
            {"qGroupFieldDefs": ["Region"]},
            {"qGroupFieldDefs": ["Country"]},
        ]}}
        assert sorted(engine._extract_fields_from_object(layout)) == ["Country", "Region"]

    def test_missing_dimension_info_is_not_an_error(self, engine):
        assert engine._extract_fields_from_object({"qListObject": {}}) == []


class TestHypercubeObjects:
    def test_dimensions_and_measure_fields(self, engine):
        layout = {"qHyperCube": {
            "qDimensionInfo": [{"qGroupFieldDefs": ["Region"]}],
            "qMeasureInfo": [{"qDef": "Sum(Sales)"}],
        }}
        assert sorted(engine._extract_fields_from_object(layout)) == ["Region", "Sales"]

    def test_an_object_with_both_shapes(self, engine):
        """Some objects carry a hypercube and a list object at once."""
        layout = {
            "qHyperCube": {"qDimensionInfo": [{"qGroupFieldDefs": ["Year"]}],
                           "qMeasureInfo": []},
            "qListObject": {"qDimensionInfo": {"qGroupFieldDefs": ["Region"]}},
        }
        assert sorted(engine._extract_fields_from_object(layout)) == ["Region", "Year"]

    def test_a_text_object_uses_no_fields(self, engine):
        assert engine._extract_fields_from_object({"qInfo": {"qType": "text-image"}}) == []

    def test_junk_in_place_of_a_hypercube_is_survived(self, engine):
        assert engine._extract_fields_from_object({"qHyperCube": "unexpected"}) == []

    def test_a_calculated_dimension_reports_the_fields_inside_it(self, engine):
        layout = {"qHyperCube": {
            "qDimensionInfo": [{"qGroupFieldDefs": ["=Year(OrderDate)"]}],
            "qMeasureInfo": [],
        }}
        assert engine._extract_fields_from_object(layout) == ["OrderDate"]


class TestExpressions:
    """Bare field names matter: most measures are written without brackets."""

    @pytest.mark.parametrize("expression, expected", [
        ("Sum(Sales)", ["Sales"]),
        ("Sum([Net Sales])", ["Net Sales"]),
        ("Sum(Sales) / Count(distinct OrderID)", ["OrderID", "Sales"]),
        ("Sum({<Year={2025}>} Sales)", ["Sales", "Year"]),
        ("If(Region='North', Sum(Sales), 0)", ["Region", "Sales"]),
        ("Sum(Aggr(Sum(Sales), Customer))", ["Customer", "Sales"]),
        ("Count(total <Region> CustomerID)", ["CustomerID", "Region"]),
        ("1 + 2", []),
        ("", []),
    ])
    def test_expression(self, engine, expression, expected):
        assert engine._extract_fields_from_expression(expression) == expected

    def test_a_string_literal_is_not_a_field(self, engine):
        """'North' is a value being compared against, not a column."""
        assert "North" not in engine._extract_fields_from_expression(
            "Sum(If(Region='North', Sales))")

    def test_a_double_quoted_search_is_not_a_field(self, engine):
        """In a set modifier, double quotes are a search value, not a name.

        `Country={"New Zealand"}` used to report `New` and `Zealand` as two
        fields of the data model.
        """
        assert engine._extract_fields_from_expression(
            'Sum({<Country={"New Zealand", "C*"}>} Sales)') == ["Country", "Sales"]

    def test_non_latin_field_names_are_found(self, engine):
        """Real dimensions on the stand this was measured against are
        `Год` and `Месяц`; an A-Za-z pattern loses every one of them."""
        assert engine._extract_fields_from_expression(
            "Count(distinct lara_id) / Sum(Год)") == ["lara_id", "Год"]

    @pytest.mark.parametrize("expression", [
        "Count(distinct lara_id) / 1e3",
        "Sum(Sales) * 1.5",
        "Sum(Sales) / 1000",
    ])
    def test_numeric_literals_are_not_fields(self, engine, expression):
        found = engine._extract_fields_from_expression(expression)
        assert all(not f[0].isdigit() for f in found), found

    def test_a_variable_expansion_is_not_a_field(self, engine):
        assert engine._extract_fields_from_expression(
            "Sum(Sales) / $(vTarget)") == ["Sales"]


class TestExpressionsFromProperties:
    """The layout has no measure expressions — the properties do.

    Measured on 31.62: `qMeasureInfo` in a GetLayout reply carries
    qFallbackTitle, formatting and statistics, and no `qDef` at all. Reading
    the expression from the layout therefore found nothing on every real
    object, while the tests passed against a hand-written layout that had a
    `qDef` Qlik never sends.
    """

    def test_inline_measure_and_dimension(self, engine):
        properties = {"qProp": {"qHyperCubeDef": {
            "qMeasures": [{"qDef": {"qDef": "Sum(Sales)", "qLabel": "Revenue"}}],
            "qDimensions": [{"qDef": {"qFieldDefs": ["Region"]}}],
        }}}
        parsed = engine._object_expressions(properties)
        assert parsed["measures"] == [
            {"expression": "Sum(Sales)", "label": "Revenue", "library_id": None}]
        assert parsed["dimensions"][0]["fields"] == ["Region"]

    def test_a_master_measure_reports_its_library_id(self, engine):
        properties = {"qProp": {"qHyperCubeDef": {
            "qMeasures": [{"qLibraryId": "abc-123", "qDef": {"qDef": ""}}],
            "qDimensions": [],
        }}}
        parsed = engine._object_expressions(properties)
        assert parsed["measures"][0]["library_id"] == "abc-123"
        assert parsed["measures"][0]["expression"] == ""

    def test_a_failed_properties_call_is_not_an_object_without_measures(self, engine):
        assert engine._object_expressions(Exception("boom")) == {
            "measures": [], "dimensions": []}

    def test_an_object_with_no_hypercube(self, engine):
        assert engine._object_expressions({"qProp": {}}) == {"measures": [], "dimensions": []}


class TestMasterItemResolution:
    """A chart built from the library stores only an id.

    Without resolving it, the charts a modeller took the trouble to
    standardise are exactly the ones reporting no fields — which is the
    wrong way round.
    """

    class _Engine(QlikEngineAPI):
        def __init__(self):
            self.batches = []

        def send_requests_pipelined(self, requests, raise_on_error=True):
            self.batches.append([r["method"] for r in requests])
            method = requests[0]["method"]
            if method in ("GetMeasure", "GetDimension"):
                return [{"qReturn": {"qHandle": 10 + i}} for i in range(len(requests))]
            if method == "GetLayout":
                # Handles were handed out in request order: measures first.
                return [{"qLayout": {"qMeasure": {"qDef": "Sum(Sales)"}}},
                        {"qLayout": {"qDim": {"qFieldDefs": ["Region"]}}}][:len(requests)]
            raise AssertionError(method)

    def test_library_measure_and_dimension_are_filled_in(self):
        engine = self._Engine()
        entries = {0: {
            "measures": [{"expression": "", "label": "", "library_id": "m-1"}],
            "dimensions": [{"fields": [], "label": "", "library_id": "d-1"}],
        }}
        engine._resolve_library_items(1, entries)
        assert entries[0]["measures"][0]["expression"] == "Sum(Sales)"
        assert entries[0]["dimensions"][0]["fields"] == ["Region"]

    def test_nothing_to_resolve_costs_no_calls(self):
        engine = self._Engine()
        entries = {0: {"measures": [{"expression": "Sum(Sales)", "library_id": None}],
                       "dimensions": []}}
        engine._resolve_library_items(1, entries)
        assert engine.batches == []

    def test_the_same_library_id_is_fetched_once(self):
        engine = self._Engine()
        entries = {
            0: {"measures": [{"expression": "", "library_id": "m-1"}], "dimensions": []},
            1: {"measures": [{"expression": "", "library_id": "m-1"}], "dimensions": []},
        }
        engine._resolve_library_items(1, entries)
        assert engine.batches[0] == ["GetMeasure"]


class TestContainerChildren:
    """A filter pane holds no fields itself; its listbox children do."""

    class _Engine(QlikEngineAPI):
        def __init__(self):
            pass

        def send_requests_pipelined(self, requests, raise_on_error=True):
            if requests[0]["method"] == "GetObject":
                return [{"qReturn": {"qHandle": 50 + i}} for i in range(len(requests))]
            return [
                {"qLayout": {"qListObject": {"qDimensionInfo": {"qGroupFieldDefs": [name]}}}}
                for name in ("Region", "Year")
            ][:len(requests)]

    def test_children_contribute_their_fields_to_the_parent(self):
        engine = self._Engine()
        layouts = {0: {"qLayout": {"qChildList": {"qItems": [
            {"qInfo": {"qId": "lb1"}}, {"qInfo": {"qId": "lb2"}}]}}}}
        assert sorted(engine._nested_object_fields(1, layouts)[0]) == ["Region", "Year"]

    def test_an_object_without_children_asks_nothing(self):
        engine = self._Engine()
        assert engine._nested_object_fields(1, {0: {"qLayout": {"qHyperCube": {}}}}) == {}

    def test_a_failed_layout_is_skipped(self):
        engine = self._Engine()
        assert engine._nested_object_fields(1, {0: Exception("gone")}) == {}
