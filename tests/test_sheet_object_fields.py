"""Which fields a sheet object uses.

Engine describes a chart with a *list* of `qDimensionInfo` and a filter pane
with exactly one — a list object holds a single dimension by definition. The
extractor treated both as lists, so on a filter pane it iterated the dict and
walked its keys: `'str' object has no attribute 'get'`, caught by a
catch-all, logged as a warning, and the object reported no fields at all.

Filter panes are the objects that say which fields a sheet lets you slice
by, so the answer to "what does this sheet work with" was missing exactly
the part a reader would look for first.
"""

import logging

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


@pytest.fixture
def engine():
    return QlikEngineAPI.__new__(QlikEngineAPI)


def _list_object(field_name):
    """A filter pane as Engine actually returns it: one dimension, not a list."""
    return {
        "qListObject": {
            "qSize": {"qcx": 1, "qcy": 12},
            "qDimensionInfo": {
                "qFallbackTitle": field_name,
                "qGroupFieldDefs": [field_name],
            },
        }
    }


class TestListObjects:
    def test_filter_pane_field_is_found(self, engine):
        assert engine._extract_fields_from_object(_list_object("Region")) == ["Region"]





class TestHypercubeObjects:
    def test_dimensions_and_measure_fields(self, engine):
        layout = {"qHyperCube": {
            "qDimensionInfo": [{"qGroupFieldDefs": ["Region"]}],
            "qMeasureInfo": [{"qDef": "Sum(Sales)"}],
        }}
        assert sorted(engine._extract_fields_from_object(layout)) == ["Region", "Sales"]






class TestExpressions:
    """Bare field names matter: most measures are written without brackets."""

    @pytest.mark.parametrize("expression, expected", [
        ("Sum(Sales)", ["Sales"]),
        ("Sum([Net Sales])", ["Net Sales"]),
        ("Sum(Sales) / Count(distinct OrderID)", ["OrderID", "Sales"]),
        ("Sum({<Year={2025}>} Sales)", ["Sales", "Year"]),
        ("If(Region='North', Sum(Sales), 0)", ["Region", "Sales"]),
        ("Sum(Aggr(Sum(Sales), Customer))", ["Customer", "Sales"]),
        ("Count(total <Region> CustomerID)", ["CustomerID", "Region"]),
        ("1 + 2", []),
        ("", []),
    ])
    def test_expression(self, engine, expression, expected):
        assert engine._extract_fields_from_expression(expression) == expected











