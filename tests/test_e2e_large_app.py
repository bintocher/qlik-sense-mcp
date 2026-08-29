"""End-to-end against a large app (10M+ rows).

Small apps answer everything instantly, which hides the whole class of
defects this server exists to prevent: a query that returns the wrong rows
because sorting was pushed to the client, a page that silently truncates, a
guard rail that only triggers past a threshold no small app reaches, a call
that ties up the single shared Engine session for minutes. Those only show
up on a fact table big enough that the Engine has to work.

Configure with `QLIK_E2E_BIG_APP` (plus the usual QLIK_E2E_* connection
variables — see conftest). On qlik1 the app is "ZZ MCP Load Test 10M":
10,000,000 rows over `Fact` plus a 10-row `Region_Dim`, with deliberately
mixed cardinalities — `client_id` 200k, `category` 8, `fact_id` 10M — and
5% NULL in `discount`.
"""

import os
import time

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


@pytest.fixture(scope="module")
def big_app():
    app = os.getenv("QLIK_E2E_BIG_APP")
    if not app:
        pytest.skip("QLIK_E2E_BIG_APP is not set")
    return app


@pytest.fixture(scope="module")
def big_model(call, big_app):
    return call("get_app_details", app_id=big_app)


class TestScale:
    def test_the_app_really_is_large(self, big_model):
        rows = max(t["rows"] for t in big_model["tables"])
        assert rows >= 10_000_000, (
            f"largest table has {rows:,} rows — this suite needs 10M+ to be meaningful")




class TestRankingOnRealVolume:
    def test_top_clients_are_actually_the_top(self, call, big_app):
        """200k groups: if ranking were done client-side this returns the wrong ones."""
        started = time.monotonic()
        cube = call("engine_create_hypercube", app_id=big_app,
                    dimensions=[{"field": "client_id"}],
                    measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                    sort_by="revenue", sort_order="desc", limit=10)
        elapsed = time.monotonic() - started

        revenues = [row[1] for row in cube["rows"]]
        assert len(revenues) == 10
        assert revenues == sorted(revenues, reverse=True)
        assert elapsed < 120, f"top-10 over 10M rows took {elapsed:.1f}s"

        # And they must beat an arbitrary client, not merely be sorted among themselves.
        sample = call("engine_create_hypercube", app_id=big_app,
                      dimensions=[{"field": "client_id"}],
                      measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                      sort_by="revenue", sort_order="asc", limit=1)
        assert revenues[-1] > sample["rows"][0][1]




class TestPagingAndLimits:
    def test_max_allowed_page_is_served(self, call, big_app):
        cube = call("engine_create_hypercube", app_id=big_app,
                    dimensions=[{"field": "client_id"}],
                    measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                    sort_by="revenue", sort_order="desc", limit=4000)
        assert len(cube["rows"]) == 4000, (
            f"asked for 4000 rows, got {len(cube['rows'])} — pages past the initial "
            "fetch are being dropped silently")












