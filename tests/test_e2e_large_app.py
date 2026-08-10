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

    def test_high_cardinality_field_is_reported_honestly(self, big_model):
        by_name = {f["name"]: f for f in big_model["fields"]}
        assert by_name["client_id"]["distinct_values"] == 200_000
        assert by_name["category"]["distinct_values"] == 8

    def test_field_comments_survive_the_round_trip(self, call, big_app):
        """COMMENT FIELD in the load script is what tells a model what a column means."""
        result = call("get_app_field", app_id=big_app, field_name="discount", limit=1)
        comment = result.get("field_comment") or ""
        assert "NULL" in comment.upper(), f"comment lost: {comment!r}"


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

    def test_ranking_by_expression_matches_ranking_by_label(self, call, big_app):
        by_label = call("engine_create_hypercube", app_id=big_app,
                        dimensions=[{"field": "category"}],
                        measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                        sort_by="revenue", sort_order="desc", limit=8)
        by_expression = call("engine_create_hypercube", app_id=big_app,
                             dimensions=[{"field": "category"}],
                             measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                             sort_by="Sum(amount)", sort_order="desc", limit=8)
        assert [r[0] for r in by_label["rows"]] == [r[0] for r in by_expression["rows"]]

    def test_grand_total_covers_every_group_not_just_the_page(self, call, big_app):
        """The total belongs to the whole query; a page-sized total would understate it."""
        page = call("engine_create_hypercube", app_id=big_app,
                    dimensions=[{"field": "client_id"}],
                    measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                    sort_by="revenue", sort_order="desc", limit=10)
        total = page.get("grand_total")
        if not total:
            pytest.skip("no grand total in the reply")
        assert total[0] > sum(row[1] for row in page["rows"]) * 100, (
            "grand total looks like it only covers the returned page")


class TestPagingAndLimits:
    def test_max_allowed_page_is_served(self, call, big_app):
        cube = call("engine_create_hypercube", app_id=big_app,
                    dimensions=[{"field": "client_id"}],
                    measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                    sort_by="revenue", sort_order="desc", limit=4000)
        assert len(cube["rows"]) == 4000, (
            f"asked for 4000 rows, got {len(cube['rows'])} — pages past the initial "
            "fetch are being dropped silently")

    def test_over_the_row_cap_is_refused_not_truncated(self, raw_call, big_app):
        result = raw_call("engine_create_hypercube", app_id=big_app,
                          dimensions=[{"field": "client_id"}],
                          measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                          limit=5001)
        assert result["error_category"] == "limit_exceeded"

    def test_over_the_cell_cap_is_refused(self, raw_call, big_app):
        result = raw_call("engine_create_hypercube", app_id=big_app,
                          dimensions=[{"field": "client_id"}],
                          measures=[{"expression": f"Sum(amount)+{i}", "label": f"m{i}"}
                                    for i in range(9)],
                          limit=2000)
        assert result["error_category"] == "cell_cap_exceeded"

    def test_search_reaches_values_far_past_any_prefetch(self, call, big_app):
        """`client_id` runs C000000..C199999; the match sits at the very end.

        Filtering a prefetched prefix locally — what this used to do — can
        only ever find matches inside that prefix, and reports the rest as
        "no matches" with nothing to say otherwise.
        """
        result = call("get_app_field", app_id=big_app, field_name="client_id",
                      search_string="C19999*")
        values = result["field_values"]
        assert len(values) == 10, f"expected C199990..C199999, got {values}"
        assert all(v.startswith("C19999") for v in values)

    def test_search_reports_how_many_matched(self, call, big_app):
        result = call("get_app_field", app_id=big_app, field_name="client_id",
                      search_string="C1999*", limit=5)
        assert len(result["field_values"]) == 5
        assert result["total_matches"] == 100, "C1999xx is 100 values"

    def test_deep_offset_returns_that_part_of_the_field(self, call, big_app):
        result = call("get_app_field", app_id=big_app, field_name="client_id",
                      limit=3, offset=150_000)
        assert result["field_values"] == ["C150000", "C150001", "C150002"]

    def test_field_values_paginate_over_200k_distinct(self, call, big_app):
        first = call("get_app_field", app_id=big_app, field_name="client_id", limit=5, offset=0)
        second = call("get_app_field", app_id=big_app, field_name="client_id", limit=5, offset=5)
        values_first = first["field_values"]
        values_second = second["field_values"]
        assert len(values_first) == 5 and len(values_second) == 5
        assert not set(values_first) & set(values_second), (
            "offset returned overlapping pages: paging happens over a prefetched "
            "prefix, not over the field")


class TestNullHandling:
    def test_null_group_is_dropped_by_default(self, call, big_app):
        """5% of `discount` is NULL and collapses into Qlik's "-" row."""
        cube = call("engine_create_hypercube", app_id=big_app,
                    dimensions=[{"field": "discount"}],
                    measures=[{"expression": "Count(1)", "label": "cnt"}],
                    sort_by="cnt", sort_order="desc", limit=5)
        assert "-" not in [row[0] for row in cube["rows"]]

    def test_null_group_can_be_kept(self, call, big_app):
        cube = call("engine_create_hypercube", app_id=big_app,
                    dimensions=[{"field": "discount"}],
                    measures=[{"expression": "Count(1)", "label": "cnt"}],
                    sort_by="cnt", sort_order="desc", limit=5,
                    exclude_null_dimensions=False)
        # ~500k NULL rows dwarf any single discount value, so the NULL group leads.
        assert cube["rows"][0][0] == "-"
        assert cube["rows"][0][1] > 100_000


class TestNullStatistics:
    def test_null_share_matches_the_data(self, call, big_app):
        """`discount` is 5% NULL by construction — the number must say so.

        The old expressions counted non-null values twice and divided them
        by each other, so the answer was ~0% however much was missing.
        """
        stats = call("get_app_field_statistics", app_id=big_app, field_name="discount")
        assert stats["null_percentage"] == pytest.approx(5.0, abs=0.2)
        assert stats["completeness_percentage"] == pytest.approx(95.0, abs=0.2)

    def test_counts_add_up_to_the_row_count(self, call, big_app):
        stats = call("get_app_field_statistics", app_id=big_app, field_name="discount")
        non_null = stats["non_null_count"]["numeric"]
        nulls = stats["null_count"]["numeric"]
        assert non_null + nulls == stats["total_count"]["numeric"] == 10_000_000

    def test_a_complete_field_reports_no_nulls(self, call, big_app):
        stats = call("get_app_field_statistics", app_id=big_app, field_name="fact_id")
        assert stats["null_percentage"] == 0
        assert stats["completeness_percentage"] == 100


class TestCostOfHeavyCalls:
    def test_field_range_stays_cheap_on_10m_rows(self, call, big_app):
        """engine_get_field_range is documented as the fast path — hold it to that."""
        started = time.monotonic()
        result = call("engine_get_field_range", app_id=big_app, field_name="amount")
        elapsed = time.monotonic() - started
        assert elapsed < 60, f"field range over 10M rows took {elapsed:.1f}s"
        assert result

    def test_heavy_call_leaves_the_session_usable(self, call, live, big_app,
                                                  can_read_script):
        """One expensive query must not wedge the single shared Engine session."""
        call("engine_create_hypercube", app_id=big_app,
             dimensions=[{"field": "client_id"}],
             measures=[{"expression": "Sum(amount)", "label": "revenue"}],
             sort_by="revenue", sort_order="desc", limit=2000)
        started = time.monotonic()
        script = call("get_app_script", app_id=big_app)
        if can_read_script:
            assert script["script_length"] > 0
        assert time.monotonic() - started < 15, "session is still busy after the heavy call"

    def test_session_objects_do_not_accumulate(self, call, big_app):
        """Each hypercube must destroy its session object; leaks pin memory in Engine."""
        for _ in range(5):
            call("engine_create_hypercube", app_id=big_app,
                 dimensions=[{"field": "category"}],
                 measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                 sort_by="revenue", sort_order="desc", limit=8)
        # A leak shows up as the sixth call slowing down or failing outright.
        started = time.monotonic()
        cube = call("engine_create_hypercube", app_id=big_app,
                    dimensions=[{"field": "category"}],
                    measures=[{"expression": "Sum(amount)", "label": "revenue"}],
                    sort_by="revenue", sort_order="desc", limit=8)
        assert len(cube["rows"]) == 8
        assert time.monotonic() - started < 60
