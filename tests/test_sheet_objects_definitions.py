"""A sheet is described from its definitions; nothing on it is calculated.

`GetLayout` on a chart makes Engine compute it. On a large app a sheet read
that way ran past 300 seconds and the client dropped the MCP session. The
properties carry everything the reply needs - measure expressions,
`qLibraryId`, state, conditions - and the layout carries none of that.
"""

import pytest

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.tools.engine import _sheet_object_entry


def _node(object_id, object_type, children=(), **prop):
    return {"qProperty": {"qInfo": {"qId": object_id, "qType": object_type}, **prop},
            "qChildren": list(children)}


def _cube(dimensions=(), measures=(), **extra):
    return {"qHyperCubeDef": {
        "qDimensions": [{"qDef": {"qFieldDefs": [d]}} for d in dimensions],
        "qMeasures": list(measures), **extra}}


SHEET = [
    _node("kpi1", "kpi", title="Revenue", **_cube(
        measures=[{"qLibraryId": "libM", "qDef": {"qDef": "", "qLabel": "Revenue"}}])),
    _node("fp1", "filterpane", children=[
        _node("lb1", "listbox", qListObjectDef={"qDef": {"qFieldDefs": ["Region"]}}),
        _node("lb2", "listbox", qListObjectDef={"qDef": {"qFieldDefs": ["Year"]}}),
    ]),
    _node("cont1", "container", children=[
        _node("bar1", "barchart", title={"qStringExpression": {"qExpr": "='Sales by ' & vDim"}},
              **_cube(dimensions=["Product"],
                      measures=[{"qDef": {"qDef": "Sum(Sales)", "qLabel": "Sales"}}])),
        _node("linked1", "barchart", qExtendsId="masterViz"),
    ]),
    _node("tbl1", "table", qStateName="Compare", **_cube(
        dimensions=["Customer"],
        measures=[{"qDef": {"qDef": "Count(OrderID)"}}],
        qCalcCondition={"qCond": {"qv": "GetSelectedCount(Year)=1"}},
        qContextSetExpression="{<Channel={'Web'}>}")),
]


class _Engine(QlikEngineAPI):
    def __init__(self):
        self.methods = []

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.methods.append(method)
        if method == "GetObject":
            return {"qReturn": {"qHandle": 10}}
        if method == "GetFullPropertyTree":
            return {"qPropEntry": {"qProperty": {}, "qChildren": SHEET}}
        raise AssertionError(method)

    def send_requests_pipelined(self, requests, timeout=None, raise_on_error=True):
        handles = {"GetObject": 30, "GetMeasure": 40}
        definitions = {
            30: {"qProp": {"qMetaDef": {"title": "Units by month"},
                           **_cube(dimensions=["Month"],
                                   measures=[{"qDef": {"qDef": "Sum(Units)"}}])}},
            40: {"qProp": {"qMeasure": {"qDef": "Sum(Amount)"}}},
        }
        replies = []
        for request in requests:
            method = request["method"]
            self.methods.append(method)
            if method in handles:
                replies.append({"qReturn": {"qHandle": handles[method]}})
            else:
                replies.append(definitions[request["handle"]])
        return replies


@pytest.fixture
def objects():
    engine = _Engine()
    result = engine._get_sheet_objects_detailed(app_handle=1, sheet_id="sheet1")
    return engine, {o["object_id"]: o for o in result}


class TestNoCalculation:
    def test_layout_is_never_asked_for(self, objects):
        engine, _ = objects
        assert "GetLayout" not in engine.methods
        assert engine.methods.count("GetFullPropertyTree") == 1

    def test_top_level_is_the_sheet_objects_only(self, objects):
        _, by_id = objects
        assert list(by_id) == ["kpi1", "fp1", "cont1", "tbl1"]


class TestWhatComesBack:
    def test_master_measure_is_resolved_from_its_definition(self, objects):
        _, by_id = objects
        assert by_id["kpi1"]["measures"][0]["expression"] == "Sum(Amount)"
        assert by_id["kpi1"]["fields_used"] == ["Amount"]

    def test_filter_pane_lists_its_fields(self, objects):
        _, by_id = objects
        pane = by_id["fp1"]
        assert [d["fields"] for d in pane["dimensions"]] == [["Region"], ["Year"]]
        assert "children" not in pane

    def test_container_keeps_its_children_and_their_fields(self, objects):
        _, by_id = objects
        container = by_id["cont1"]
        titles = [c["object_title"] for c in container["children"]]
        assert titles == ["='Sales by ' & vDim", "Units by month"]
        assert container["children"][1]["master_object"] == "masterViz"
        assert container["fields_used"] == ["Month", "Product", "Sales", "Units"]

    def test_state_condition_and_set_are_reported(self, objects):
        _, by_id = objects
        table = by_id["tbl1"]
        assert table["state"] == "Compare"
        assert table["calc_condition"] == "GetSelectedCount(Year)=1"
        assert table["context_set"] == "{<Channel={'Web'}>}"

    def test_a_field_name_with_spaces_stays_one_field(self):
        from qlik_sense_mcp_server.engine.sheets import _fill_fields_used
        obj = {"measures": [], "dimensions": [
            {"fields": ["Sport (detail)", "[Tournament type]", "=Year([Order Date])"]}]}
        assert _fill_fields_used(QlikEngineAPI.__new__(QlikEngineAPI), obj) == [
            "Order Date", "Sport (detail)", "Tournament type"]

    def test_reply_entry_carries_the_optional_keys_only_when_set(self, objects):
        _, by_id = objects
        assert "state" not in _sheet_object_entry(by_id["kpi1"])
        entry = _sheet_object_entry(by_id["cont1"])
        assert entry["children"][1]["master_object"] == "masterViz"
        assert entry["object_description"] == ""
