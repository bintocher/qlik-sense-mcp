"""Past the expression cap, the one query that answers the whole batch.

Measured: a model asked twelve months as twelve queries of twenty
measures and hit the 200-expression cap four times in a row. When the
queries differ only in the filter on one field, the refusal now carries
the query that answers all of them - grouped by that field.
"""

import json

from qlik_sense_mcp_server.engine.queries import one_query_instead
from qlik_sense_mcp_server.tools.helpers import _ok
from tests.test_typed_queries import _Engine

METRICS = [{"field": f"M{n}", "agg": "sum"} for n in range(8)]


def _monthly(months=12):
    return [{"id": f"m{month}", "group_by": ["Region"], "metrics": METRICS,
             "filters": [{"field": "OrderDate", "period": f"2025-{month:02d}"},
                         {"field": "Region", "values": ["North"]}]}
            for month in range(1, months + 1)]


class TestTheRefusal:
    def test_twelve_periods_are_one_grouped_query(self):
        result = _Engine().run_queries(1, "app", _monthly())
        assert result["error_category"] == "limit_exceeded"
        assert "differ only in the filter on [OrderDate]" in result["error"]
        one = result["one_query"]
        assert one["group_by"] == ["Region", "OrderDate"]
        assert one["metrics"] == METRICS
        assert {"field": "OrderDate", "from": "2025-01-01",
                "to": "2025-12-31"} in one["filters"]
        assert {"field": "Region", "values": ["North"]} in one["filters"]

    def test_the_example_survives_the_reply(self):
        """Advice keys are stripped from refusals; the query is data."""
        reply = json.loads(_ok(_Engine().run_queries(1, "app", _monthly())))
        assert reply["one_query"]["group_by"][-1] == "OrderDate"


class TestTheComparison:
    def test_value_lists_are_joined(self):
        queries = [{"metrics": METRICS,
                    "filters": [{"field": "Region", "values": [name]}]}
                   for name in ("North", "South")]
        instead = one_query_instead(queries)
        assert instead["field"] == "Region"
        assert instead["query"]["filters"] == [
            {"field": "Region", "values": ["North", "South"]}]

    def test_queries_that_differ_in_more_than_one_filter_get_nothing(self):
        queries = _monthly(2)
        queries[1]["filters"][1]["values"] = ["South"]
        assert one_query_instead(queries) is None

    def test_queries_that_differ_in_their_measures_get_nothing(self):
        queries = _monthly(2)
        queries[1]["metrics"] = METRICS[:3]
        assert one_query_instead(queries) is None
