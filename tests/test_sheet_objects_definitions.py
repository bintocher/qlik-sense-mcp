"""A sheet is described from its definitions; nothing on it is calculated.

`GetLayout` on a chart makes Engine compute it. On a large app a sheet read
that way ran past 300 seconds and the client dropped the MCP session. The
properties carry everything the reply needs - measure expressions,
`qLibraryId`, state, conditions - and the layout carries none of that.
"""

import pytest

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.engine.objects import fields_used
from qlik_sense_mcp_server.tools.engine import _sheet_object_entry


def _node(object_id, object_type, kids=(), **prop):
    return {"qProperty": {"qInfo": {"qId": object_id, "qType": object_type}, **prop},
            "qChildren": list(kids)}


def _cube(dimensions=(), measures=(), **extra):
    return {"qHyperCubeDef": {
        "qDimensions": [{"qDef": {"qFieldDefs": [d]}} for d in dimensions],
        "qMeasures": list(measures), **extra}}


SHEET = [
    _node("kpi1", "kpi", title="Revenue", **_cube(
        measures=[{"qLibraryId": "libM", "qDef": {"qDef": "", "qLabel": "Revenue"}}])),
    _node("fp1", "filterpane", kids=[
        _node("lb1", "listbox", qListObjectDef={"qDef": {"qFieldDefs": ["Region"]}}),
        _node("lb2", "listbox", qListObjectDef={"qDef": {"qFieldDefs": ["Year"]}}),
    ]),
    _node("cont1", "container", kids=[
        _node("bar1", "barchart", title={"qStringExpression": {"qExpr": "='Sales by ' & vDim"}},
              containerChildId="t1",
              **_cube(dimensions=["Product"],
                      measures=[{"qDef": {"qDef": "Sum(Sales)", "qLabel": "Sales"}}])),
        _node("linked1", "barchart", qExtendsId="masterViz"),
    ], children=[{"refId": "t1", "label": "Sales",
                  "condition": {"qStringExpression": {"qExpr": "=$(vTab)=1"}}}]),
    _node("tbl1", "table", qStateName="Compare", **_cube(
        dimensions=["Customer"],
        measures=[{"qDef": {"qDef": "Count(OrderID)"}}],
        qCalcCondition={"qCond": {"qv": "GetSelectedCount(Year)=1"}},
        qContextSetExpression="{<Channel={'Web'}>}")),
    # Left over from an earlier edit: in the tree, not in the cells.
    _node("orphan", "kpi", **_cube(measures=[{"qDef": {"qDef": "Sum(Hidden)"}}])),
]
CELLS = [{"name": "tbl1", "row": 10, "col": 0}, {"name": "kpi1", "row": 0, "col": 0},
         {"name": "fp1", "row": 0, "col": 12}, {"name": "cont1", "row": 5, "col": 0}]


class _Engine(QlikEngineAPI):
    def __init__(self):
        self.methods = []

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.methods.append(method)
        if method == "GetObject":
            return {"qReturn": {"qHandle": 10}}
        if method == "GetFullPropertyTree":
            return {"qPropEntry": {"qProperty": {"cells": CELLS}, "qChildren": SHEET}}
        raise AssertionError(method)

    def send_requests_pipelined(self, requests, timeout=None, raise_on_error=True):
        handles = {"GetObject": 30, "GetMeasure": 40}
        definitions = {
            30: {"qProp": {"qMetaDef": {"title": "Units by month"},
                           **_cube(dimensions=["Month"],
                                   measures=[{"qDef": {"qDef": "Sum(Units)"}}])}},
            40: {"qProp": {"qMetaDef": {"title": "Revenue"},
                           "qMeasure": {"qDef": "Sum(Amount)"}}},
        }
        replies = []
        for request in requests:
            method = request["method"]
            self.methods.append(method)
            if method in handles:
                replies.append({"qReturn": {"qHandle": handles[method]}})
            elif method == "GetProperties":
                replies.append(definitions[request["handle"]])
            else:
                replies.append(RuntimeError(method))
        return replies


@pytest.fixture
def objects():
    engine = _Engine()
    result = engine._get_sheet_objects_detailed(app_handle=1, sheet_id="sheet1")
    return engine, {o["object_id"]: o for o in result}, result


class TestNoCalculation:
    def test_layout_is_never_asked_for(self, objects):
        engine, _, _ = objects
        assert "GetLayout" not in engine.methods
        assert engine.methods.count("GetFullPropertyTree") == 1

    def test_top_level_is_the_cells_in_reading_order(self, objects):
        _, _, result = objects
        assert [o["object_id"] for o in result] == ["kpi1", "fp1", "cont1", "tbl1"]


class TestWhatComesBack:
    def test_master_measure_is_resolved_from_its_definition(self, objects):
        _, by_id, _ = objects
        measure = by_id["kpi1"]["measures"][0]
        assert (measure["master_id"], measure["expression"]) == ("libM", "Sum(Amount)")
        assert by_id["kpi1"]["fields_used"] == ["Amount"]
        assert by_id["kpi1"]["summary"] == \
            'kpi "Revenue": measure master libM "Revenue" = Sum(Amount)'

    def test_filter_pane_lists_its_fields(self, objects):
        _, by_id, _ = objects
        pane = by_id["fp1"]
        assert [d["field"] for d in pane["dimensions"]] == ["Region", "Year"]
        assert "children" not in pane
        assert pane["summary"] == "filterpane: fields Region, Year"

    def test_container_keeps_its_children_and_their_fields(self, objects):
        _, by_id, _ = objects
        container = by_id["cont1"]
        titles = [c.get("title") for c in container["children"]]
        assert titles == ["='Sales by ' & vDim", "Units by month"]
        assert container["children"][0]["tab"] == "Sales"
        assert container["children"][0]["shown_if"] == "=$(vTab)=1"
        assert container["children"][1]["master_object"]["id"] == "masterViz"
        assert container["fields_used"] == ["Month", "Product", "Sales", "Units"]

    def test_state_condition_and_set_are_reported(self, objects):
        _, by_id, _ = objects
        table = by_id["tbl1"]
        assert table["state"] == "Compare"
        assert table["conditions"] == [{"on": "object", "condition": "GetSelectedCount(Year)=1"}]
        assert table["context_set"] == "{<Channel={'Web'}>}"
        assert "calculated if GetSelectedCount(Year)=1" in table["summary"]
        assert "state Compare" in table["summary"]

    def test_a_field_name_with_spaces_stays_one_field(self):
        obj = {"measures": [], "dimensions": [
            {"field": "Sport (detail)"}, {"drill_down": ["[Tournament type]"]},
            {"expression": "=Year([Order Date])"}]}
        assert fields_used(obj) == ["Order Date", "Sport (detail)", "Tournament type"]

    def test_reply_entry_is_the_line_and_the_fields(self, objects):
        _, by_id, _ = objects
        entry = _sheet_object_entry(by_id["kpi1"])
        assert set(entry) == {"object_id", "object_type", "title", "summary", "fields_used"}
        entry = _sheet_object_entry(by_id["cont1"])
        assert entry["children"][1]["summary"].endswith('from master "Units by month" masterViz')
        assert "fields_used" not in entry["children"][0]
