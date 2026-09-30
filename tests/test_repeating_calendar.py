"""A filter on a calendar whose dates repeat covers more than it names.

Measured on a 1.16-billion-row app: a calendar of 669 rows over 21 months
holds a row per month and per accumulation (the month, year to date, the
twelve months to date, ...), told apart by one hidden field. A filter
`{"field": "Month", "values": ["2026-08"]}` reached thirteen months of
facts and the month's GGR came back as 60.9 billion against 2.9, with no
warning: the fan-out check ran for periods only. Now any filter on such a
calendar - values, a range, a set written by hand - is checked for key
values that belong to other values of its field, the warning names the
field, the tables and how many key values one value covers, and
`get_app_details` says which field picks one row per date.
"""

from qlik_sense_mcp_server.engine.layers import (
    EngineLayersMixin, layer_text, repeating_calendars, repeating_path)
from tests.test_filter_reaches_measure import _Cube
from tests.test_typed_queries import _Engine, _query


def _field(name, table, rows, distinct, tags=("$numeric",)):
    return {"field_name": name, "table_name": table, "table_rows": rows,
            "rows_count": rows, "distinct_values": distinct, "tags": list(tags)}


DATE = ("$date", "$numeric")

# Facts by month; a cumulative calendar of 8 layers over 21 months; a plain
# calendar of days.
MODEL = [
    _field("MonthKey", "Facts", 1_000_000, 21, DATE),
    _field("DayKey", "Facts", 1_000_000, 600, DATE),
    _field("Amount", "Facts", 1_000_000, 50_000),
    _field("MonthKey", "Accumulated", 669, 21, DATE),
    _field("MonthName", "Accumulated", 669, 21, DATE),
    _field("Month", "Accumulated", 669, 21, ("$text",)),
    _field("Layer", "Accumulated", 669, 8, ("$text", "$hidden")),
    _field("DayKey", "Days", 600, 600, DATE),
    _field("Day", "Days", 600, 600, DATE),
]

PLAIN = [{"table": "Accumulated", "key": "MonthKey", "rows": 669, "dates": 21,
          "plain": [{"fields": ["Layer"], "values": ["Month"],
                     "parts": ["[Layer]={'Month'}"],
                     "condition": "[Layer]={'Month'}"}]}]


class TestStructure:
    def test_the_calendar_repeats_its_dates(self):
        path = repeating_path(MODEL, "Month")
        assert (path["key"], path["table"]) == ("MonthKey", "Facts")
        assert path["repeats"] == [{"table": "Accumulated", "key": "MonthKey",
                                    "rows": 669, "dates": 21}]

    def test_an_ordinary_calendar_does_not(self):
        assert repeating_path(MODEL, "Day")["repeats"] == []

    def test_the_candidates_are_the_few_valued_fields_beside_the_date(self):
        [calendar] = repeating_calendars(MODEL)
        assert calendar["table"] == "Accumulated"
        assert calendar["dates_beside"] == ["MonthName"]
        assert calendar["candidates"] == ["Layer", "Month"]


class _SpreadEngine(_Engine):
    """Month '2026-08' reaches 13 month keys, and on the plain rows one;
    `reached` is how many a filter reaches without the plain layer."""

    def __init__(self, reached=13, plain=1):
        super().__init__(known=("Region", "Amount", "Month", "MonthKey", "Layer",
                                "Year"))
        self.probes = []
        self.counts = (reached, plain)

    def _model_fields(self, app_handle):
        return MODEL

    def plain_layers(self, app_handle, raw_fields):
        return PLAIN

    def _evaluate(self, expression):
        self.probes.append(expression)
        reached, plain_count = self.counts
        plain = "[Layer]={'Month'}" in expression
        if "P(" in expression:
            count = min(reached, plain_count)
        elif "DISTINCT [MonthKey]" in expression:
            count = plain_count if plain else reached
        elif "DISTINCT [Month]" in expression:
            count = 1
        else:
            return super()._evaluate(expression)
        return {"qText": str(count), "qNumber": count, "qIsNumeric": True}


class TestTheWarning:
    def test_a_value_list_on_a_repeating_calendar_is_said_first(self):
        result = _SpreadEngine().run_queries(1, "app", [_query(
            filters=[{"field": "Month", "values": ["2026-08"]}],
            accept_fan_out=True)])
        reply = result["results"][0]
        [spread] = reply["fan_out_check"]
        assert spread == {"field": "Month", "table": "Facts", "key": "MonthKey",
                          "key_values": 13, "per_value": 13.0,
                          "plain_key_values": 1, "repeating_tables": ["Accumulated"]}
        note = reply["warnings"][0]
        assert note.startswith("The filter on [Month] ('2026-08') reaches Facts "
                               "through 13 values of [MonthKey]")
        assert "Accumulated holds 669 rows for 21 dates of MonthKey" in note
        assert "{<[Layer]={'Month'}>}" in " ".join(reply["warnings"])
        assert result["warnings"][0].startswith("q1: The filter on [Month]")
        assert reply["numbers_verified"] is False

    def test_a_filter_reaching_exactly_its_own_period_is_quiet(self):
        """Live, `Year=2025` on the cumulative calendar reached the twelve
        months of 2025 and the total was right, yet the reply said the
        numbers were unverified. The same key values on the plain rows mean
        the period asked for."""
        reply = _SpreadEngine(reached=12, plain=12).run_queries(1, "app", [_query(
            filters=[{"field": "Month", "values": ["2025"]}])])["results"][0]
        assert "fan_out_check" not in reply
        assert "numbers_verified" not in reply

    def test_the_plain_layer_set_beside_it_quiets_it(self):
        reply = _SpreadEngine().run_queries(1, "app", [_query(
            filters=[{"field": "Month", "values": ["2026-08"]},
                     {"field": "Layer", "values": ["Month"]}])])["results"][0]
        assert "fan_out_check" not in reply
        assert "warnings" not in reply

    def test_a_set_written_by_hand_is_checked_too(self):
        engine = _SpreadEngine()
        engine.send_request = lambda method, params=None, handle=-1, timeout=None: \
            _Cube.send_request(engine, method, params, handle, timeout)
        engine.ws = object()
        reply = engine.create_hypercube(1, [{"field": "Region"}], [
            {"expression": "Sum({<Month={'2026-08'}>} Amount)", "label": "GGR"}], 10,
            accept_fan_out=True)
        assert reply["warnings"][0].startswith(
            "The set of measure 'GGR' on [Month] reaches Facts through 13 values")
        assert reply["fan_out_check"][0]["measure"] == "GGR"

    def test_grouping_by_a_repeating_calendar_without_its_plain_layer(self):
        """A row per month of a cumulative calendar sums every layer of that
        month; the filter check alone would say nothing without a filter."""
        reply = _SpreadEngine().run_queries(1, "app", [_query(
            group_by=["Month"], accept_fan_out=True)])["results"][0]
        [spread] = reply["fan_out_check"]
        assert spread["grouped_by"] is True and spread["repeating_tables"] == ["Accumulated"]
        assert reply["warnings"][0].startswith("The rows are grouped by [Month]")
        quiet = _SpreadEngine().run_queries(1, "app", [_query(
            group_by=["Month"], filters=[{"field": "Layer", "values": ["Month"]}])])
        assert "fan_out_check" not in quiet["results"][0]

    def test_a_query_that_fans_out_is_not_computed(self):
        """Live, such a query computed for 156 s before its warning came
        back; the check runs with the object's creation and the layout is
        never asked for."""
        engine = _SpreadEngine()
        result = engine.run_queries(1, "app", [
            _query(filters=[{"field": "Month", "values": ["2026-08"]}]),
            _query(filters=[{"field": "Region", "values": ["North"]}])])
        refused, answered = result["results"]
        assert refused["error_category"] == "fan_out"
        assert "Not computed" in refused["error"]
        assert refused["fan_out_check"][0]["key_values"] == 13
        assert answered["rows"]
        layouts = [b for b in engine.batches if b[0] == "GetLayout"]
        assert layouts == [["GetLayout"]]

    def test_the_refusal_says_each_thing_once(self):
        """Live, a refusal for two measures grouped by month repeated the
        statement about the filter twice and the plain rows three times."""
        engine = _SpreadEngine()
        engine.known.update({"Flag", "Kind"})
        reply = engine.run_queries(1, "app", [_query(
            group_by=["Month"], metrics=[],
            measures=[{"expression": "Sum({filter} {<Flag={1}>} Amount)", "label": "A"},
                      {"expression": "Sum({filter} {<Kind={2}>} Amount)", "label": "B"}],
            filters=[{"field": "Month", "values": ["2026-08"]}])])["results"][0]
        sentences = [s for s in reply["error"].split(". ") if s]
        assert len(sentences) == len(set(sentences))
        assert reply["error"].count("One row per date") == 1

    def test_a_hypercube_that_fans_out_is_not_computed(self):
        engine = _SpreadEngine()
        sent = []

        def send(method, params=None, handle=-1, timeout=None):
            sent.append(method)
            return _Cube.send_request(engine, method, params, handle, timeout)
        engine.send_request = send
        engine.ws = object()
        reply = engine.create_hypercube(1, [{"field": "Region"}], [
            {"expression": "Sum({<Month={'2026-08'}>} Amount)", "label": "GGR"}], 10)
        assert reply["error_category"] == "fan_out"
        assert "CreateSessionObject" not in sent

    def test_a_field_on_an_ordinary_table_is_not_asked(self):
        engine = _SpreadEngine()
        engine.run_queries(1, "app", [_query(
            filters=[{"field": "Region", "values": ["North"]}])])
        assert not any("P(" in probe for probe in engine.probes)


class _LayerEngine(EngineLayersMixin):
    """Rows per value of the candidate fields of the calendar."""

    ROWS = {
        # "Part" holds the plain rows of 12 of the 21 dates only, as a
        # fiscal-year field set on one year's plain rows did live.
        # "To date" holds the very same rows as "Month" on a calendar of
        # months, as a month-to-date layer did live; it is loaded after it.
        ("Layer",): [["Month", 21, 21, 21], ["To date", 21, 21, 21],
                     ["YTD", 120, 21, 21],
                     ["Shift", 21, 21, 0], ["Part", 12, 12, 12]],
        ("Month",): [["2026-08", 8, 8, 1]],
    }

    def __init__(self):
        self.asked = []

    def create_hypercube(self, app_handle, dimensions, measures, max_rows=None, **kw):
        fields = tuple(d["field"] for d in dimensions)
        self.asked.append((fields, [m["expression"] for m in measures]))
        self.sorts = [d.get("sort_by", {}).get("qSortByLoadOrder") for d in dimensions]
        return {"rows": self.ROWS.get(fields, [])}


class TestThePlainLayer:
    def test_one_row_per_date_that_is_the_date_itself(self):
        engine = _LayerEngine()
        [layer] = engine.plain_layers(1, MODEL)
        # One value is named, the first loaded: a cumulative layer that
        # happens to hold the same rows is not offered.
        assert layer["plain"] == [{"fields": ["Layer"], "values": ["Month"],
                                   "parts": ["[Layer]={'Month'}"],
                                   "condition": "[Layer]={'Month'}"}]
        assert engine.sorts == [1]
        fields, measures = engine.asked[0]
        assert measures == ["Count([Layer])", "Count(DISTINCT [MonthKey])",
                            "Sum(If([MonthName]=[MonthKey],1,0))"]

    def test_a_pair_of_fields_when_no_single_one_picks_it(self):
        class _Pairs(_LayerEngine):
            ROWS = {("Year shift", "Month shift"): [[0, 0, 21, 21, 21], [1, 0, 21, 21, 0]]}
        model = [f for f in MODEL if f["field_name"] not in ("Layer", "Month")] + [
            _field("Year shift", "Accumulated", 669, 2),
            _field("Month shift", "Accumulated", 669, 2)]
        [layer] = _Pairs().plain_layers(1, model)
        assert layer["plain"][0]["fields"] == ["Year shift", "Month shift"]
        assert layer["plain"][0]["values"] == ["0", "0"]

    def test_a_dual_value_is_named_by_its_text(self):
        """Live, a fiscal year shown as `24-25` came back as the number 2025,
        and a condition on '2025' would select nothing."""
        class _Dual(_LayerEngine):
            def create_hypercube(self, app_handle, dimensions, measures,
                                 max_rows=None, **kw):
                if dimensions[0]["field"] != "Layer":
                    return {"rows": []}
                return {"rows": [[2025, 21, 21, 21]], "hypercube_data": {
                    "qDataPages": [{"qMatrix": [[{"qText": "24-25", "qNum": 2025}]]}]}}
        [layer] = _Dual().plain_layers(1, MODEL)
        assert layer["plain"][0]["values"] == ["24-25"]

    def test_the_sentence_names_the_condition(self):
        text = layer_text(PLAIN[0])
        assert text == ("Accumulated holds 669 rows for 21 dates of MonthKey: one "
                        "date stands for several rows. One row per date, the date "
                        "itself: [Layer] = 'Month' - set it with every filter on "
                        "this calendar, e.g. {<[Layer]={'Month'}>}.")


CHAIN = [
    _field("FactKey", "Facts", 1_000_000, 33, DATE),
    _field("Amount", "Facts", 1_000_000, 50_000),
    _field("FactKey", "Shift", 63, 33, DATE),
    _field("MonthLink", "Shift", 63, 21, DATE),
    _field("MonthLink", "Cumulative", 669, 21, DATE),
    _field("Month", "Cumulative", 669, 21, ("$text",)),
]


class _ChainEngine(_Engine):
    """Counted back through each key, the field reaches `through`."""

    def __init__(self, through):
        super().__init__(known=("Region", "Amount", "Month", "MonthLink", "FactKey"))
        self.through = through

    def _model_fields(self, app_handle):
        return CHAIN

    def plain_layers(self, app_handle, raw_fields):
        return []

    def _evaluate(self, expression):
        for key, count in self.through.items():
            if f"[{key}]=P(" in expression:
                return {"qText": str(count), "qNumber": count, "qIsNumeric": True}
        if "DISTINCT [FactKey]" in expression:
            return {"qText": "3", "qNumber": 3, "qIsNumeric": True}
        if "DISTINCT [Month]" in expression:
            return {"qText": "1", "qNumber": 1, "qIsNumeric": True}
        return super()._evaluate(expression)


class TestWhichTableRepeats:
    """Live, a month on a cumulative calendar behind a shift calendar was
    blamed on the cumulative one although the shift one did it."""

    def _spread(self, through):
        reply = _ChainEngine(through).run_queries(1, "app", [_query(
            filters=[{"field": "Month", "values": ["2026-08"]}],
            accept_fan_out=True)])["results"][0]
        return reply["fan_out_check"][0], reply["warnings"][0]

    def test_the_shift_calendar_alone(self):
        spread, note = self._spread({"MonthLink": 1, "FactKey": 5})
        assert spread["repeating_tables"] == ["Shift"]
        assert "Shift holds 63 rows" in note and "Cumulative holds" not in note

    def test_the_cumulative_calendar_alone(self):
        spread, _ = self._spread({"MonthLink": 12, "FactKey": 12})
        assert spread["repeating_tables"] == ["Cumulative"]

    def test_both(self):
        spread, _ = self._spread({"MonthLink": 12, "FactKey": 13})
        assert spread["repeating_tables"] == ["Cumulative", "Shift"]
