"""A period that applied, over facts outside it.

Measured on a 1.16-billion-row app: a filter on the date of a year-shift
calendar for August 2026 reached three months of facts through its key -
the month itself, the month a year before and the month before - and the
figure grew 3.2 times while `filter_applied` said true. The period check
now also reads the key the field reaches the facts through: when that key
is a date and holds values outside the period, the reply says so.
"""

from qlik_sense_mcp_server.engine.queries import fan_out_key
from tests.test_typed_queries import _Engine, _query


def _field(name, table, rows, tags=("$numeric",)):
    return {"field_name": name, "table_name": table, "table_rows": rows,
            "rows_count": rows, "tags": list(tags)}


DATE = ("$date", "$numeric")

SHIFT_MODEL = [
    _field("ShiftKey", "Facts", 1_000_000, DATE),
    _field("Amount", "Facts", 1_000_000),
    _field("ShiftKey", "ShiftLink", 63, DATE),
    _field("OrderDate", "ShiftLink", 63, DATE),
    _field("OrderDate", "Months", 21, DATE),
]

PLAIN_MODEL = [
    _field("DateKey", "Facts", 1_000_000, DATE),
    _field("Amount", "Facts", 1_000_000),
    _field("DateKey", "Calendar", 700, DATE),
    _field("OrderDate", "Calendar", 700, DATE),
]


class TestTheKey:
    def test_a_shift_calendar_reaches_the_facts_through_its_key(self):
        assert fan_out_key(SHIFT_MODEL, "OrderDate") == {
            "key": "ShiftKey", "table": "Facts", "temporal": True}

    def test_a_field_of_the_facts_has_no_key(self):
        assert fan_out_key(PLAIN_MODEL, "DateKey") is None

    def test_a_field_linked_to_nothing_has_no_key(self):
        assert fan_out_key(PLAIN_MODEL + [_field("Island", "Alone", 5)],
                           "Island") is None


class _ModelEngine(_Engine):
    """The period's own ends stay inside it; the key's ends are given."""

    def __init__(self, model, key_bounds, key_values=3, **kwargs):
        super().__init__(**kwargs)
        self.model, self.key_bounds, self.key_values = model, key_bounds, key_values

    def _model_fields(self, app_handle):
        return self.model

    def _evaluate(self, expression):
        if "ShiftKey" in expression or "DateKey" in expression:
            if "DISTINCT" in expression:
                return {"qText": str(self.key_values),
                        "qNumber": self.key_values, "qIsNumeric": True}
            value = self.key_bounds[0 if "Min(" in expression else 1]
            if expression.startswith("=Text("):
                return {"qText": f"day-{value}", "qNumber": "NaN"}
            return {"qText": str(value), "qNumber": value, "qIsNumeric": True}
        return super()._evaluate(expression)


def _run(engine):
    return engine.run_queries(1, "app", [_query(
        filters=[{"field": "OrderDate", "period": "2011"}])])["results"][0]


class TestTheReply:
    def test_key_values_outside_the_period_are_reported(self):
        reply = _run(_ModelEngine(SHIFT_MODEL, (40179, 40878)))
        [check] = reply["period_check"]
        assert check["filter_applied"] is True
        fan = check["fan_out"]
        assert (fan["key"], fan["values"]) == ("ShiftKey", 3)
        assert (fan["min"], fan["max"]) == ("day-40179", "day-40878")
        assert any("ShiftKey" in w and "outside the period" in w
                   for w in reply["warnings"])
        assert reply["numbers_verified"] is False

    def test_an_ordinary_calendar_has_no_fan_out(self):
        reply = _run(_ModelEngine(PLAIN_MODEL, (40544, 40908), key_values=365))
        [check] = reply["period_check"]
        assert "fan_out" not in check
        assert "warnings" not in reply

    def test_the_key_is_asked_in_the_same_batch(self):
        engine = _ModelEngine(SHIFT_MODEL, (40179, 40878))
        _run(engine)
        creates = [b for b in engine.batches if "CreateSessionObject" in b]
        assert len(creates) == 1
        assert creates[0].count("EvaluateEx") == 9
