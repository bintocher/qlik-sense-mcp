"""One value, one writing, in every reply about it.

A date is stored as a serial day count and displayed as text. Engine hands
back both, and the two halves of this server used to disagree: a hypercube
returned `45292` while the sample values, the field values and the field
range for the same field all said `01.01.2024`. A model reading both had
to decide which was the value — and the wrong choice writes a filter that
selects nothing.

Engine says which columns hold a point in time, in `qNumFormat.qType`, so
the choice is not guessed at.
"""

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


def _cube(*types):
    """A layout whose columns carry the given Qlik format types."""
    return {
        "qDimensionInfo": [{"qNumFormat": {"qType": types[0]}}],
        "qMeasureInfo": [{"qNumFormat": {"qType": t}} for t in types[1:]],
    }


def _pages(*rows):
    return [{"qMatrix": [
        [{"qText": text, "qNum": num} for text, num in row] for row in rows]}]


class TestTemporalColumns:
    @pytest.mark.parametrize("qtype", ["D", "T", "TS", "IV"])
    def test_every_time_format_counts(self, qtype):
        assert QlikEngineAPI._temporal_columns(_cube(qtype)) == {0}

    @pytest.mark.parametrize("qtype", ["A", "I", "R", "F", "M", "U"])
    def test_a_number_or_a_string_does_not(self, qtype):
        assert QlikEngineAPI._temporal_columns(_cube(qtype)) == set()

    def test_measures_are_numbered_after_the_dimensions(self):
        assert QlikEngineAPI._temporal_columns(_cube("A", "F", "D")) == {2}

    def test_a_layout_without_format_information_claims_nothing(self):
        assert QlikEngineAPI._temporal_columns({}) == set()


class TestRowValues:
    def test_a_date_column_reads_as_the_text_qlik_displays(self):
        rows = QlikEngineAPI._matrix_to_rows(
            _pages([("01.01.2024", 45292), ("100", 100)]),
            ["OrderDate", "Revenue"], temporal_columns={0})
        assert rows == [["01.01.2024", 100]]

    def test_without_the_hint_a_date_would_come_back_as_a_serial_number(self):
        """What the two halves used to disagree about."""
        rows = QlikEngineAPI._matrix_to_rows(
            _pages([("01.01.2024", 45292)]), ["OrderDate"])
        assert rows == [[45292]]

    def test_a_measure_stays_a_number_it_can_be_summed(self):
        rows = QlikEngineAPI._matrix_to_rows(
            _pages([("North", "NaN"), ("1 234,50", 1234.5)]),
            ["Region", "Revenue"], temporal_columns=set())
        assert rows == [["North", 1234.5]]

    def test_a_date_cell_with_no_text_falls_back_to_its_number(self):
        rows = QlikEngineAPI._matrix_to_rows(
            _pages([("", 45292)]), ["OrderDate"], temporal_columns={0})
        assert rows == [[45292]]

    def test_a_null_cell_stays_qliks_dash(self):
        rows = QlikEngineAPI._matrix_to_rows(
            _pages([("-", "NaN")]), ["OrderDate"], temporal_columns={0})
        assert rows == [["-"]]
