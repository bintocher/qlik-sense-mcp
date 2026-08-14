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




class TestDataModel:
    def test_details_describe_the_real_model(self, data_model):
        assert data_model["tables_count"] >= 1, "a reloaded app has tables"
        assert data_model["fields_count"] >= 1
        assert len(data_model["tables"]) == data_model["tables_count"]
        for table in data_model["tables"]:
            assert table["name"]
            assert table["rows"] >= 0







class TestFields:
    def test_field_values_come_from_the_field(self, call, app_id, text_field):
        result = call("get_app_field", app_id=app_id, field_name=text_field["name"], limit=5)
        values = result.get("field_values") or []
        assert values, f"field {text_field['name']} has {text_field['distinct_values']} distinct values"
        assert len(values) <= 5
        assert len(values) <= text_field["distinct_values"]









