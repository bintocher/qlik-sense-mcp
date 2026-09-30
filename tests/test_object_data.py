"""An object's data is computed as one flat table from its own definition.

Its layout is not: a stacked chart or a pivot answers with a tree of nodes,
a map computes each layer twice, and a date picker sends every date of its
field. The server sends the object's dimensions and measures to Engine as a
straight hypercube instead, keeping the row whose dimension is empty.
"""

import json
import pathlib

import pytest

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.tools.engine import get_app_object

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "objects"
LIBRARY = json.loads((FIXTURES / "_library.json").read_text(encoding="utf-8"))


def _node(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["node"]


class _Engine(QlikEngineAPI):
    """Answers definitions for one object; computes nothing but what it is asked."""

    def __init__(self, node, conditions=None, effective=None):
        self.node, self.conditions, self.effective = node, conditions or {}, effective
        self.methods, self.cubes = [], []

    def ensure_app(self, app_id, no_data=False):
        return 1

    def _library_for(self, app_handle, app_id=None):
        return LIBRARY["items"]

    def _get_user_variables(self, app_handle):
        return [{"name": "vDetail", "text_value": "1"}]

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.methods.append(method)
        if method == "GetObject":
            return {"qReturn": {"qHandle": 50}}
        if method == "GetProperties":
            return {"qProp": self.node["qProperty"]}
        if method == "GetFullPropertyTree":
            return {"qPropEntry": self.node}
        if method == "GetEffectiveProperties":
            return {"qProp": self.effective}
        raise AssertionError(method)

    def send_requests_pipelined(self, requests, timeout=None, raise_on_error=True):
        replies = []
        for request in requests:
            self.methods.append(request["method"])
            if request["method"] == "EvaluateEx":
                value = self.conditions.get(request["params"]["qExpression"], 1)
                replies.append({"qValue": {"qIsNumeric": True, "qNumber": value}})
            elif request["method"] == "GetObject":
                master = request["params"]["qId"]
                replies.append({"qReturn": {"qHandle": master}}
                               if master in LIBRARY["visualizations"] else RuntimeError(master))
            elif request["method"] == "GetProperties":
                replies.append({"qProp": LIBRARY["visualizations"][request["handle"]]})
            else:
                replies.append(RuntimeError(request["method"]))
        return replies

    def create_hypercube(self, app_handle, dimensions=None, measures=None, max_rows=1000, **kw):
        self.cubes.append({"dimensions": dimensions, "measures": measures,
                           "max_rows": max_rows, **kw})
        if [m["label"] for m in measures] == ["min", "max"]:
            return {"rows": [[1, 101]]}
        columns = [d["label"] for d in dimensions] + [m["label"] for m in measures]
        return {"columns": columns, "rows": [["-"] * len(columns)], "total_rows": 1,
                "returned_rows": 1, "has_more": False, "dimensions": dimensions,
                "measures": measures, "hard_max_rows": 5000, "timings": {}}


def _data(name, **engine_args):
    engine = _Engine(_node(name), **engine_args)
    described = engine.describe_app_object(1, engine.node["qProperty"]["qInfo"]["qId"])
    return engine, described, engine.object_data(1, described, limit=20, offset=0)


class TestFlatCube:
    def test_pivot_is_flat_and_a_hidden_column_is_left_out(self):
        engine, _, data = _data("pivot-table", conditions={"$(vDetail)": 0})
        cube = engine.cubes[0]
        assert [d["field"] for d in cube["dimensions"]] == ["Region", "Year"]
        assert [m["expression"] for m in cube["measures"]] == ["Sum(Amount)"]
        assert cube["exclude_null_dimensions"] is False
        # The object's expressions run as the object runs them.
        assert cube["strict_fields"] is False
        assert any("left out" in note for note in data["notes"])
        assert "GetLayout" not in engine.methods
        assert "dimensions" not in data and "timings" not in data

    def test_master_items_are_resolved_for_the_cube(self):
        engine, _, data = _data("barchart")
        cube = engine.cubes[0]
        assert [d["field"] for d in cube["dimensions"]] == ["Year", "Channel"]
        assert cube["measures"] == [{"expression": "Sum(Amount)", "label": "Revenue"}]
        assert any("group" in note for note in data["notes"])

    def test_the_objects_own_sort_is_kept(self):
        engine, _, _ = _data("table")
        assert (engine.cubes[0]["sort_by"], engine.cubes[0]["sort_order"]) == ("Revenue", "desc")

    def test_bullet_target_is_a_column(self):
        engine, _, _ = _data("bulletchart")
        assert [m["label"] for m in engine.cubes[0]["measures"]] == ["Sales", "Sales target"]

    def test_histogram_counts_per_class(self):
        engine, _, data = _data("histogram")
        cube = engine.cubes[-1]
        assert cube["dimensions"] == [{"field": "=Class([Quantity], 5)", "label": "bin"}]
        assert cube["measures"] == [{"expression": "Count([Quantity])", "label": "count"}]
        assert "Class([Quantity], 5)" in data["notes"][0]

    def test_map_counts_its_first_layer(self):
        engine, _, _ = _data("map")
        cube = engine.cubes[0]
        assert [d["field"] for d in cube["dimensions"]] == ["=StoreName"]
        assert [m["expression"] for m in cube["measures"]] == ["=Sum(Amount)"]

    def test_a_filter_computes_nothing(self):
        engine, _, data = _data("filterpane")
        assert engine.cubes == []
        assert "get_app_field" in data["note"]


class TestDefinition:
    def test_linked_object_is_read_with_effective_properties(self):
        master = LIBRARY["visualizations"]["vUnitsByMonth"]
        engine = _Engine(_node("linked-master"), effective=master)
        described = engine.describe_app_object(1, "lk1")
        assert "GetEffectiveProperties" in engine.methods
        assert described["measures"][0]["expression"] == "Sum(Units)"
        assert described["master_object"]["id"] == "vUnitsByMonth"


class TestTool:
    @pytest.fixture
    def tool(self, monkeypatch):
        def call(name, **engine_args):
            engine = _Engine(_node(name), **engine_args)
            monkeypatch.setattr(context, "engine_api", engine)
            monkeypatch.setattr(context, "repo_api", object())
            object_id = engine.node["qProperty"]["qInfo"]["qId"]
            return engine, json.loads(get_app_object("app", object_id, limit=20))
        return call

    def test_reply_is_definition_variables_and_data(self, tool):
        engine, reply = tool("pivot-table")
        assert reply["summary"].startswith('pivot-table "Revenue matrix"')
        assert reply["variables"] == {"vDetail": "1"}
        assert reply["data"]["columns"] == ["Region", "Product", "Year", "Revenue"]
        assert "qLayout" not in reply and "_cube" not in json.dumps(reply)
        assert "GetLayout" not in engine.methods

    def test_bad_limit_is_refused_before_engine(self, tool, monkeypatch):
        engine, reply = tool("kpi")
        assert json.loads(get_app_object("app", "kpi1", limit=0))["error_category"] == \
            "invalid_argument"
