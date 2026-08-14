"""A filter that is reported as applied must actually be applied.

The failure this covers is the worst kind this server exists to prevent:
a reply that carries both a wrong number and a control value saying the
number is right. It happened for a measure the caller wrote by hand — the
filter went into the control probes but never into the expression, so the
query aggregated every row while `period_check` reported the period.
"""

import pytest

from tests.test_typed_queries import _Engine, _query


class TestHandWrittenMeasures:
    def test_a_marked_measure_gets_the_filter(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[],
            measures=[{"expression": "Sum({filter} [Amount])", "label": "Revenue"}],
            filters=[{"field": "OrderDate", "period": "2011"}]), 0)
        assert "{<" in plan["measures"][0]["expression"]
        assert "{filter}" not in plan["measures"][0]["expression"]






class TestBatchIsolation:
    def test_a_malformed_offset_only_fails_its_own_query(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [
            _query(), _query(offset="next"), _query()])
        assert result["queries_run"] == 2
        assert result["results"][1]["error"]



class TestValidationIsOneBatch:
    def test_five_queries_are_checked_together(self):
        """Checking each query separately turned a three-round-trip batch
        into thirteen."""
        engine = _Engine()
        engine.run_queries(1, "app", [_query() for _ in range(5)])
        expands = [b for b in engine.batches if b[0] == "ExpandExpression"]
        checks = [b for b in engine.batches if b[0] == "CheckExpression"]
        assert len(expands) == 1
        assert len(checks) == 1



























