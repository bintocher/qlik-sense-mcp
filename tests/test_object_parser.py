"""Every type of object is read by its own rule, from properties alone.

The fixtures in `fixtures/objects/` are synthetic: their structure follows
what Qlik returns for each type - including the noise around the meaning,
leftovers of earlier chart types, styling keys, defaults - and every name
and expression is invented. Each one states the line a model reads and the
structured fields a later step relies on.
"""

import json
import pathlib

import pytest

from qlik_sense_mcp_server.engine.objects import (
    Library, ObjectParser, clean_expression, condition_text, fields_in_expression,
    fields_used, lexical_paragraphs, literal_text, markdown_text, public, strip_comments,
    variables_in)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "objects"
LIBRARY = json.loads((FIXTURES / "_library.json").read_text(encoding="utf-8"))
CASES = sorted(p for p in FIXTURES.glob("*.json") if not p.name.startswith("_"))


def _parser():
    library = Library(LIBRARY["items"], LIBRARY["visualizations"])
    return ObjectParser(library, sheet_title=LIBRARY["sheets"].get,
                        map_layers=lambda map_id: LIBRARY["map_layers"].get(map_id, []))


def _at(data, path):
    for key in path.split("."):
        data = data[int(key)] if isinstance(data, list) else data[key]
    return data


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_fixture(path):
    case = json.loads(path.read_text(encoding="utf-8"))
    described = _parser().describe_node(case["node"])
    expect = case["expect"]
    if expect.get("skipped"):
        assert described is None
        return
    assert described["summary"] == expect["summary"]
    for key, value in (expect.get("checks") or {}).items():
        assert _at(described, key) == value, key
    if "fields_used" in expect:
        assert fields_used(described) == expect["fields_used"]
    # Nothing meant for the server leaks into a reply.
    assert "_cube" not in json.dumps(public(described))


def test_every_type_of_the_plan_has_a_fixture_family():
    covered = {json.loads(p.read_text(encoding="utf-8"))["node"]["qProperty"]["qInfo"]["qType"]
               for p in CASES}
    assert {"kpi", "table", "barchart", "combochart", "scatterplot", "gauge", "waterfallchart",
            "pivot-table", "filterpane", "container", "action-button", "qlik-variable-input",
            "qlik-date-picker", "VizlibTable", "VizlibKPI", "VizlibAdvancedTextObject",
            "VizlibCalendar", "VizlibContainer", "sn-text", "ideviomap",
            "acme-radial-chart", "adhoc-query"} <= covered


class TestExpressionsForReading:
    def test_comments_go_quotes_stay(self):
        text = "Sum(Amount) // old: Sum(Cost)\n+ Count('a//b') /* note */ + [x//y]"
        assert " ".join(strip_comments(text).split()) == "Sum(Amount) + Count('a//b') + [x//y]"

    def test_commented_out_first_line(self):
        assert clean_expression("=//=Sum(OldPlan)\r\n\nSum(Plan)") == "=Sum(Plan)"

    def test_cut_says_how_much(self):
        assert clean_expression("x" * 310, limit=300).endswith("... (+10 chars)")

    @pytest.mark.parametrize("value", ["", "1", "=1", " = 1 ", -1, True, {"qCond": {}, "qMsg": {}}])
    def test_always_true_is_no_condition(self, value):
        assert condition_text(value) is None

    def test_quoted_constant_is_text(self):
        assert literal_text("='Hour'") == "Hour"
        assert literal_text("=vLabel") == "=vLabel"

    def test_variables_in_order(self):
        assert variables_in("Sum({<Y={$(vYear)}>} X) * $(vRate) + $(=Max(Y)) + $(vYear)") == [
            "vYear", "vRate"]

    def test_function_with_hash_is_a_call(self):
        assert fields_in_expression("Num#(MaxString(zip)) + Date#(Day)") == ["Day", "zip"]


class TestText:
    def test_markdown_markup_and_measures(self):
        text = markdown_text("^[^[**%[Title](3)**](center)](right) =[](c1) \\(x\\)", {"c1": "Sales"})
        assert text == "Title {Sales} (x)"

    def test_lexical_text_is_decoded_twice(self):
        stored = json.dumps({"root": {"children": [
            {"type": "paragraph", "children": [{"type": "text", "text": "A"},
                                               {"type": "text", "text": "B"}]}]}})
        assert lexical_paragraphs(stored) == ["AB"]


class TestContainers:
    def test_children_are_not_repeated_and_depth_is_bounded(self):
        def container(qid, inner):
            return {"qProperty": {"qInfo": {"qId": qid, "qType": "container"}, "children": []},
                    "qChildren": inner}
        leaf = {"qProperty": {"qInfo": {"qId": "leaf", "qType": "kpi"},
                              "qHyperCubeDef": {"qMeasures": [{"qDef": {"qDef": "Sum(A)"}}]}},
                "qChildren": []}
        nested = container("c1", [container("c2", [container("c3", [container("c4", [leaf])])])])
        described = _parser().describe_node(nested)
        fourth = described["children"][0]["children"][0]["children"][0]
        assert fourth["object_id"] == "c4"
        assert fourth["summary"] == "container: 1 objects inside, not expanded"

    def test_an_object_seen_once(self):
        kpi = {"qProperty": {"qInfo": {"qId": "k", "qType": "kpi"}}, "qChildren": []}
        seen = set()
        parser = _parser()
        assert parser.describe_node(kpi, 0, seen) is not None
        assert parser.describe_node(kpi, 0, seen) is None
