"""Against a real Qlik: the queries that come back wrong instead of failing.

These cannot be faked. The whole point is what Qlik itself does with a
mistake — and what it does is answer. A cube grouped by a field that does
not exist returns the grand total as a single row; a measure filtered on a
value that does not exist returns a full table of zeros. Both are valid
JSON with plausible numbers, and neither says anything is wrong.
"""

import os

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def app(request):
    return os.getenv("QLIK_E2E_APP") or pytest.skip("QLIK_E2E_APP is not set")


@pytest.fixture(scope="module")
def a_real_field(call, app):
    """Any field with data — the tests below need one that exists."""
    details = call("get_app_details", app_id=app)
    fields = [f["name"] for f in details.get("fields", []) if f.get("distinct_values")]
    if not fields:
        pytest.skip("the configured app has no fields with data")
    return fields[0]


class TestUnknownDimensionIsRefused:
    def test_a_misspelled_dimension_does_not_return_a_number(self, raw_call, app):
        result = raw_call("engine_create_hypercube", app_id=app,
                          dimensions=["no_such_field_xyz"],
                          measures=["Count(1)"], limit=5)
        assert result.get("error_category") == "field_not_found", (
            f"Qlik answered instead of failing: {result}")
        assert "no_such_field_xyz" in result.get("unknown_fields", [])




class TestSilentlyEmptyMeasures:
    def test_a_filter_on_a_value_that_does_not_exist_is_flagged(self, call, app, a_real_field):
        cube = call("engine_create_hypercube", app_id=app,
                    dimensions=[a_real_field],
                    measures=["Count({<%s={'nothing_matches_this'}>} 1)" % a_real_field],
                    limit=5)
        assert any("0 or '-'" in w for w in cube.get("warnings", [])), (
            f"a table of zeros passed without comment: {cube.get('rows')}")




class TestValidationCost:
    def test_the_check_is_cheap(self, call, app, a_real_field):
        """It runs on every query, so it has to be one pipelined round-trip."""
        cube = call("engine_create_hypercube", app_id=app,
                    dimensions=[a_real_field], measures=["Count(1)"], limit=5)
        validate_seconds = cube.get("timings", {}).get("validate_seconds")
        assert validate_seconds is not None, "validation step is not timed"
        assert validate_seconds < 1.0, f"validation took {validate_seconds}s"
