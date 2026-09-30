"""get_app_details says which rows of a repeating calendar keep one per date.

A model filtered such a calendar nine times out of nineteen without the
hidden field that picks the plain rows. The calendar now carries the
condition, and the field keeps its values when the reply is trimmed.
"""

from qlik_sense_mcp_server.engine.layers import layer_text
from qlik_sense_mcp_server.tools.overview import apply_layers, trim_values
from tests.test_repeating_calendar import PLAIN


def test_details_keep_the_hidden_field_that_picks_the_layer():
    tables = [{"name": "Accumulated", "fields": [
        {"name": "Layer", "hidden": True, "values": [f"v{i}" for i in range(12)]},
        {"name": "Month", "values": [f"m{i}" for i in range(21)]}]}]
    warnings = apply_layers(tables, PLAIN)
    assert tables[0]["plain_layer"] == "{<[Layer]={'Month'}>}"
    assert tables[0]["fields"][0]["picks_plain_layer"] is True
    assert warnings == [layer_text(PLAIN[0])]
    trim_values({"tables": tables})
    assert len(tables[0]["fields"][0]["values"]) == 12
    assert len(tables[0]["fields"][1]["values"]) == 10
