"""Master dimensions are described by what they are built from.

A dimension has no `qDef` expression: its body is `qFieldDefs`, `qFieldLabels`
and `qGrouping`. Reading `/qDim/qDef` gave every dimension an empty
expression, so a model saw "Trader" and could not tell it stands for the
field `TraderName`.
"""

from contextlib import contextmanager

import pytest

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.engine.sheets import (
    MASTER_DIMENSION_EXPRESSION_CHARS,
    describe_master_dimension,
)


class TestDescribe:
    def test_single_field(self):
        out = describe_master_dimension("Trader", ["TraderName"], [""], "N")
        assert out == {"definition": "Trader = TraderName", "fields": ["TraderName"]}

    def test_drill_down(self):
        out = describe_master_dimension(
            "Event hierarchy", ["Sport", "Tournament", "Event"], [], "H")
        assert out["definition"] == "Event hierarchy = Sport > Tournament > Event (drill-down)"
        assert out["fields"] == ["Sport", "Tournament", "Event"]

    def test_cyclic_group_keeps_differing_labels(self):
        out = describe_master_dimension(
            "Period", ["Year", "Month", "Day"], ["", "", "Calendar day"], "C")
        assert out["definition"] == "Period = Year | Month | [Calendar day]=Day (cyclic)"

    def test_calculated_is_cut_and_flagged(self):
        expression = "=If(Odds < 1.5, 'low', " + "'x' & " * 60 + "'high')"
        out = describe_master_dimension("Odds band", [expression], [], "N")
        body = out["definition"][len("Odds band = "):-len(" (calculated)")]
        assert out["definition"].startswith("Odds band = =If(Odds < 1.5")
        assert out["definition"].endswith("... (calculated)")
        assert len(body) == MASTER_DIMENSION_EXPRESSION_CHARS + 3
        assert out["expression_truncated"] is True
        assert "fields" not in out

    def test_short_calculated_is_whole(self):
        out = describe_master_dimension("Year", ["=Year(OrderDate)"], [], "N")
        assert out == {"definition": "Year = =Year(OrderDate) (calculated)"}


class _Engine(QlikEngineAPI):
    """Answers the two library list objects the way Engine does."""

    def __init__(self):
        self.definitions = []

    @contextmanager
    def session_object(self, app_handle, definition):
        self.definitions.append(definition)
        yield definition["qInfo"]["qId"]

    def send_request(self, method, params=None, handle=None):
        if handle == "library-measures":
            return {"qLayout": {"qMeasureList": {"qItems": [
                {"qInfo": {"qId": "m1"}, "qData": {"title": "Revenue",
                                                   "expression": "Sum(Amount)"}},
            ]}}}
        return {"qLayout": {"qDimensionList": {"qItems": [
            {"qInfo": {"qId": "d1"}, "qData": {
                "title": "Trader", "field_defs": ["TraderName"],
                "field_labels": [""], "grouping": "N"}},
            {"qInfo": {"qId": "d2"}, "qData": {
                "title": "Geography", "field_defs": ["Country", "City"],
                "field_labels": ["", ""], "grouping": "H"}},
        ]}}}


class TestLibrary:
    def test_dimension_list_asks_for_field_defs_not_qdef(self):
        engine = _Engine()
        engine.get_master_items(1)
        dimension_def = engine.definitions[1]["qDimensionListDef"]["qData"]
        assert dimension_def["field_defs"] == "/qDim/qFieldDefs"
        assert dimension_def["grouping"] == "/qDim/qGrouping"
        assert "/qDim/qDef" not in dimension_def.values()

    def test_dimensions_come_back_as_definitions(self):
        library = _Engine().get_master_items(1)
        assert library["measures"] == [
            {"name": "Revenue", "id": "m1", "expression": "Sum(Amount)"}]
        assert [d["definition"] for d in library["dimensions"]] == [
            "Trader = TraderName", "Geography = Country > City (drill-down)"]
        assert all("expression" not in d for d in library["dimensions"])


class _FlatteningEngine(_Engine):
    """Engine as it really answers: a list's qData flattens an array to ""."""

    def send_request(self, method, params=None, handle=None):
        if handle == "library-measures":
            return super().send_request(method, params, handle)
        return {"qLayout": {"qDimensionList": {"qItems": [
            {"qInfo": {"qId": "d1"}, "qData": {"title": "Trader", "field_defs": "",
                                               "field_labels": "", "grouping": "N"}},
            {"qInfo": {"qId": "d2"}, "qData": {"title": "Geography", "field_defs": "",
                                               "field_labels": "", "grouping": "H"}},
        ]}}}

    def send_requests_pipelined(self, requests, raise_on_error=True):
        if requests[0]["method"] == "GetDimension":
            self.opened = [r["params"]["qId"] for r in requests]
            return [{"qReturn": {"qHandle": i}} for i in range(len(requests))]
        bodies = {"d1": {"qFieldDefs": ["TraderName"], "qGrouping": "N"},
                  "d2": {"qFieldDefs": ["Country", "City"], "qGrouping": "H"}}
        return [{"qProp": {"qMetaDef": {"title": {"d1": "Trader", "d2": "Geography"}[i]},
                           "qDim": bodies[i]}} for i in self.opened]


def test_dimensions_are_read_from_properties_when_the_list_flattens_them():
    library = _FlatteningEngine().get_master_items(1)
    assert [d["definition"] for d in library["dimensions"]] == [
        "Trader = TraderName", "Geography = Country > City (drill-down)"]
    assert library["dimensions"][1]["fields"] == ["Country", "City"]
    assert all("_unread" not in d for d in library["dimensions"])
