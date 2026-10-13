"""`usage` and `report_filters`: how the author's own sheets use the model.

The visualizations are the reference answers of the app. A field grouped by
in 40 objects and a look-alike used in none tell the caller which one the
report means, without the server choosing for it.
"""

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.engine.objects import ObjectParser
from qlik_sense_mcp_server.tools import usage
from qlik_sense_mcp_server.tools.usage import expression_roles

MODEL = {"Date", "ShiftDate", "Region", "Amount", "Status", "IssueId", "Days", "Month",
         "Accumulation"}


def _chart(object_id, title, dims, measures, kind="barchart", **extra):
    props = {"qInfo": {"qId": object_id, "qType": kind}, "title": title,
             "qHyperCubeDef": {
                 "qDimensions": [{"qDef": {"qFieldDefs": [d]}} for d in dims],
                 "qMeasures": [{"qDef": {"qDef": m, "qLabel": label}} for label, m in measures]}}
    props.update(extra)
    return props


def _node(props, children=()):
    return {"qProperty": props, "qChildren": list(children)}


def _sheets():
    parser = ObjectParser()
    tabs = _node({"qInfo": {"qId": "tabs", "qType": "container"},
                  "children": [{"refId": "c1", "label": "Totals", "condition": "$(vView)=1"},
                               {"refId": "c2", "label": "Spread", "condition": "$(vView)=2"}]},
                 [_node(dict(_chart("t1", "Closed issues by month", ["Month"],
                                    [("Closed", "Count({<Status={'Closed'}>} distinct IssueId)")]),
                             containerChildId="c1")),
                  _node(dict(_chart("t2", "85th percentile of days per issue", ["Region"],
                                    [("P85", "{<Status={'Closed'}>} Fractile(Aggr(Sum(Days), IssueId), 0.85)")]),
                             containerChildId="c2"))])
    first = [
        parser.describe(_chart("b1", "Amount by date and region", ["Date", "Region"],
                               [("Amount", "Sum({<Status={'Closed'}>} Amount)")])),
        parser.describe(_chart("b2", "Amount by date", ["Date"], [("Amount", "Sum(Amount)")],
                               kind="linechart", qStateName="Compare")),
        parser.describe_node(tabs),
        parser.describe({"qInfo": {"qId": "v1", "qType": "qlik-variable-input"},
                         "variableName": "vView",
                         "alternatives": [{"value": "1", "label": "Totals"},
                                          {"value": "2", "label": "Spread"}]}),
        parser.describe({"qInfo": {"qId": "p1", "qType": "qlik-date-picker"},
                         "qListObjectDef": {"qDef": {"qFieldDefs": ["Date"]}}}),
        parser.describe({"qInfo": {"qId": "k1", "qType": "action-button"},
                         "style": {"label": "Last month"},
                         "actions": [{"actionType": "applyBookmark", "bookmark": "bm-1"}]}),
    ]
    second = [parser.describe(_chart("b3", "Amount by date", ["Date"], [("Amount", "Sum(Amount)")]))]
    return [{"title": "Overview", "objects": first}, {"title": "Detail", "objects": second}]


def _summary():
    return usage.summarise(_sheets(), MODEL, {"bm-1": "Previous month"})


class TestRoles:
    def test_condition_aggregation_and_aggr_dimension_are_told_apart(self):
        roles = expression_roles(
            "{<Status={'Closed'}>} Fractile(Aggr(Sum(Days), IssueId), 0.85)")
        assert roles == {"Status": {"condition"}, "Days": {"aggregated"},
                         "IssueId": {"Aggr dimension"}}

    def test_a_field_named_only_in_a_set_is_not_aggregated(self):
        roles = expression_roles("Sum({<Region={'North'}, Year={2025}>} Amount)")
        assert roles["Region"] == {"condition"} and roles["Amount"] == {"aggregated"}


class TestUsage:
    def test_field_lines_count_objects_sheets_roles_and_neighbours(self):
        summary, _filters = _summary()
        lines = {line.split(":")[0]: line for line in summary["fields"]}
        assert lines["Date"].startswith("Date: 3 objects, 2 sheets; dimension in 3")
        assert "with measures: Amount" in lines["Date"]
        assert "with fields: Amount, Region" in lines["Date"]
        assert lines["Status"].startswith("Status: 3 objects, 1 sheet; condition in 3")

    def test_a_field_from_a_set_expression_counts_once_per_object(self):
        summary, _filters = _summary()
        status = next(line for line in summary["fields"] if line.startswith("Status:"))
        assert "3 objects" in status

    def test_container_children_are_read(self):
        summary, _filters = _summary()
        assert any(line.startswith('"85th percentile of days per issue"')
                   for line in summary["objects"])
        issue = next(line for line in summary["fields"] if line.startswith("IssueId:"))
        assert "Aggr dimension in 1" in issue and "aggregated in 1" in issue

    def test_titles_come_first_and_duplicates_fold(self):
        summary, _filters = _summary()
        titles = [line.split('"')[1] for line in summary["objects"]]
        assert titles.count("Amount by date") == 1

    def test_formulas_are_deduplicated_templates(self):
        summary, _filters = _summary()
        amount = [f for f in summary["formulas"] if f.startswith('"Amount" = Sum(Amount)')]
        assert amount == ['"Amount" = Sum(Amount) (sheet "Overview", 2 objects)']

    def test_unused_fields_and_special_objects(self):
        summary, _filters = _summary()
        assert summary["unused_fields"] == "Accumulation, ShiftDate"
        assert summary["alternate_state_or_set"] == [
            'linechart "Amount by date" (sheet "Overview"): state Compare']


class TestReportFilters:
    def test_controls_are_listed_with_variable_meaning(self):
        _summary_, filters = _summary()
        assert "date picker on Date: 1 sheet" in filters
        assert "variable input vView: 1=Totals, 2=Spread (1 sheet)" in filters
        assert 'button "Last month": applyBookmark bookmark "Previous month"' in filters
        tab = next(f for f in filters if f.startswith('"Spread"'))
        assert "shown if $(vView)=2" in tab and "[vView: 1=Totals, 2=Spread]" in tab


def test_trim_keeps_the_most_used_fields():
    summary, _filters = _summary()
    reply = {"usage": summary}
    assert usage.trim_usage(reply, fields=1)
    assert len(reply["usage"]["fields"]) == 1


class _Sheets(QlikEngineAPI):
    def __init__(self):
        self.read = []

    def get_sheets(self, app_handle):
        return [{"qInfo": {"qId": "s1"}, "qMeta": {"title": "Base", "approved": True}},
                {"qInfo": {"qId": "s2"}, "qMeta": {"title": "Mine", "approved": False}}]

    def _get_sheet_objects_detailed(self, app_handle, sheet_id):
        self.read.append(sheet_id)
        return []


def test_only_approved_sheets_are_read():
    engine = _Sheets()
    sheets, basis = engine.published_sheets(1)
    assert engine.read == ["s1"] and [s["title"] for s in sheets] == ["Base"]
    assert basis == "approved sheets"


def test_an_unpublished_app_reads_all_its_sheets():
    engine = _Sheets()
    engine.get_sheets = lambda handle: [{"qInfo": {"qId": "s2"}, "qMeta": {"title": "Draft"}}]
    _sheets_read, basis = engine.published_sheets(1)
    assert engine.read == ["s2"] and basis.startswith("all sheets")
