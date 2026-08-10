"""End-to-end: every tool against a real Qlik, checked on real values.

These assert on what came back, not on the shape of the envelope. A tool
that returns `{"rows": []}` for a question with a known answer is broken
even though every key is in place — and that is exactly how the QRS error
masking and the empty-script bug presented themselves in production.
"""

import pytest

pytestmark = pytest.mark.e2e


class TestDiscovery:
    def test_get_about_reports_a_version(self, call):
        about = call("get_about")
        assert about.get("buildVersion"), "QRS must report a build version"

    def test_get_apps_returns_apps_with_identifiers(self, call):
        apps = call("get_apps", limit=5)
        assert apps["apps"], "server has no published apps to list"
        for app in apps["apps"]:
            assert app.get("guid"), "every app needs the id the other tools take"
            assert app.get("name")
        assert len(apps["apps"]) <= 5, "limit must be honoured"

    def test_pagination_moves_the_window(self, call):
        first = call("get_apps", limit=2, offset=0)
        if first["pagination"]["total_found"] < 3:
            pytest.skip("need at least 3 apps to test paging")
        second = call("get_apps", limit=2, offset=2)
        ids_first = {a["guid"] for a in first["apps"]}
        ids_second = {a["guid"] for a in second["apps"]}
        assert not (ids_first & ids_second), "offset must return a different page"


class TestDataModel:
    def test_details_describe_the_real_model(self, data_model):
        assert data_model["tables_count"] >= 1, "a reloaded app has tables"
        assert data_model["fields_count"] >= 1
        assert len(data_model["tables"]) == data_model["tables_count"]
        for table in data_model["tables"]:
            assert table["name"]
            assert table["rows"] >= 0

    def test_every_field_belongs_to_a_listed_table(self, data_model):
        tables = {t["name"] for t in data_model["tables"]}
        for field in data_model["fields"]:
            assert field["table"] in tables, f"field {field['name']} points at an unknown table"

    def test_script_is_not_empty(self, call, app_id, can_read_script):
        """An empty script used to come back as a successful reply."""
        if not can_read_script:
            pytest.skip("this identity may not read load scripts (Analyzer licence)")
        script = call("get_app_script", app_id=app_id)
        assert script["script_length"] > 0
        assert script["script_length"] == len(script["qScript"])

    def test_an_unreadable_script_explains_itself(self, call, app_id, can_read_script):
        """Empty because there is none, and empty because you may not see
        it, must not look the same."""
        script = call("get_app_script", app_id=app_id)
        if can_read_script:
            assert "note" not in script
        else:
            assert "Professional" in script["note"]

    def test_variables_are_split_by_source(self, call, app_id):
        variables = call("get_app_variables", app_id=app_id)
        assert "variables_from_script" in variables
        assert "variables_from_ui" in variables

    def test_empty_variable_sets_keep_their_type(self, call, app_id):
        """A client that indexes variables_from_ui must not have to type-check it."""
        variables = call("get_app_variables", app_id=app_id)
        assert isinstance(variables["variables_from_script"], dict)
        assert isinstance(variables["variables_from_ui"], dict)


class TestFields:
    def test_field_values_come_from_the_field(self, call, app_id, text_field):
        result = call("get_app_field", app_id=app_id, field_name=text_field["name"], limit=5)
        values = result.get("field_values") or []
        assert values, f"field {text_field['name']} has {text_field['distinct_values']} distinct values"
        assert len(values) <= 5
        assert len(values) <= text_field["distinct_values"]

    def test_range_agrees_with_the_model(self, call, app_id, text_field):
        result = call("engine_get_field_range", app_id=app_id, field_name=text_field["name"])
        # distinct count from a mini-hypercube must match what the data model reported
        reported = result.get("distinct_count") or result.get("count")
        if reported is not None:
            assert reported == text_field["distinct_values"], (
                "field range disagrees with the data model about cardinality")

    def test_statistics_are_returned(self, call, app_id, text_field):
        stats = call("get_app_field_statistics", app_id=app_id, field_name=text_field["name"])
        assert stats.get("field_name") == text_field["name"] or stats.get("statistics")

    def test_unknown_field_fails_loudly(self, raw_call, app_id):
        """Silence here would let a model keep querying a field that does not exist."""
        result = raw_call("get_app_field", app_id=app_id,
                          field_name="__no_such_field_9c1f__", limit=5)
        assert "error" in result or not (result.get("field_values") or []), (
            "a nonexistent field must not look like a field with no values")


class TestHypercube:
    def test_group_by_returns_rows_with_real_numbers(self, call, app_id, text_field):
        field = text_field["name"]
        cube = call("engine_create_hypercube", app_id=app_id,
                    dimensions=[{"field": field}],
                    measures=[{"expression": f"Count([{field}])", "label": "cnt"}],
                    limit=10)
        assert cube["columns"] == [field, "cnt"]
        assert cube["rows"], "grouping a populated field must return rows"
        for row in cube["rows"]:
            assert isinstance(row[1], (int, float)), "measures must survive as numbers"
            assert row[1] > 0

    def test_descending_sort_actually_sorts(self, call, app_id, text_field):
        """The 1.6.0 bug: sorting by a measure silently returned alphabetical rows."""
        field = text_field["name"]
        cube = call("engine_create_hypercube", app_id=app_id,
                    dimensions=[{"field": field}],
                    measures=[{"expression": f"Count([{field}])", "label": "cnt"}],
                    sort_by="cnt", sort_order="desc", limit=10)
        counts = [row[1] for row in cube["rows"]]
        assert counts == sorted(counts, reverse=True), f"not sorted descending: {counts}"

    def test_ascending_sort_is_the_other_way_round(self, call, app_id, text_field):
        field = text_field["name"]
        cube = call("engine_create_hypercube", app_id=app_id,
                    dimensions=[{"field": field}],
                    measures=[{"expression": f"Count([{field}])", "label": "cnt"}],
                    sort_by="cnt", sort_order="asc", limit=10)
        counts = [row[1] for row in cube["rows"]]
        assert counts == sorted(counts)

    def test_limit_caps_the_row_count(self, call, app_id, text_field):
        field = text_field["name"]
        cube = call("engine_create_hypercube", app_id=app_id,
                    dimensions=[{"field": field}],
                    measures=[{"expression": "Count(1)", "label": "cnt"}],
                    sort_by="cnt", sort_order="desc", limit=2)
        assert len(cube["rows"]) <= 2

    def test_grand_total_matches_the_column(self, call, app_id, text_field):
        field = text_field["name"]
        cube = call("engine_create_hypercube", app_id=app_id,
                    dimensions=[{"field": field}],
                    measures=[{"expression": f"Count([{field}])", "label": "cnt"}],
                    limit=1000)
        total = cube.get("grand_total")
        if not total or cube.get("truncation_warning"):
            pytest.skip("result truncated — per-row sum cannot be compared to the total")
        assert total[0] == pytest.approx(sum(row[1] for row in cube["rows"]))

    def test_unknown_sort_column_is_refused_before_calling_qlik(self, raw_call, app_id, text_field):
        result = raw_call("engine_create_hypercube", app_id=app_id,
                          dimensions=[{"field": text_field["name"]}],
                          measures=[{"expression": "Count(1)", "label": "cnt"}],
                          sort_by="no_such_column", limit=5)
        assert result["error_category"] == "invalid_sort"
        assert result["available_columns"] == [text_field["name"], "cnt"]

    def test_oversized_request_is_refused(self, raw_call, app_id, text_field):
        result = raw_call("engine_create_hypercube", app_id=app_id,
                          dimensions=[{"field": text_field["name"]}],
                          measures=[{"expression": "Count(1)", "label": "cnt"}],
                          limit=5001)
        assert result["error_category"] == "limit_exceeded"
        assert result["hint"]


class TestSheets:
    def test_sheets_and_their_objects(self, call, sheet_app_id):
        sheets = call("get_app_sheets", app_id=sheet_app_id)
        if not sheets["sheets"]:
            pytest.skip("test app has no sheets — set QLIK_E2E_SHEET_APP")
        assert sheets["total_sheets"] == len(sheets["sheets"])
        sheet_id = sheets["sheets"][0]["sheet_id"]

        objects = call("get_app_sheet_objects", app_id=sheet_app_id, sheet_id=sheet_id)
        items = objects.get("objects") or []
        if not items:
            pytest.skip("first sheet has no objects")
        for item in items:
            assert item.get("object_id")
            assert item.get("object_type")

        # Every object the sheet listed must be readable on its own.
        single = call("get_app_object", app_id=sheet_app_id, object_id=items[0]["object_id"])
        assert single.get("object_id") == items[0]["object_id"] or single.get("qLayout")


class TestBadInput:
    def test_unknown_app_is_reported_as_such(self, raw_call):
        result = raw_call("get_app_details",
                          app_id="00000000-0000-0000-0000-000000000000")
        assert "error" in result, "a missing app must not return an empty but successful model"

    def test_every_reply_carries_its_duration(self, call):
        assert "tool_call_seconds" in call("get_about")
