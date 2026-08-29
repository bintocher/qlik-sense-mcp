"""Tests for utility functions."""

import pytest
from qlik_sense_mcp_server.utils import (
    format_bytes,
    format_number,
    format_duration_ms,
    extract_field_names_from_expression,
    clean_field_name,
    detect_field_type_from_name,
    safe_divide,
    calculate_percentage,
    group_objects_by_type,
    filter_system_fields,
    filter_system_tables,
    summarize_field_types,
    find_unused_fields,
    validate_app_id,
    format_qlik_date,
    create_summary_stats,
    truncate_text,
    bare_field_name,
    escape_qlik_field_name,
    generate_xrfkey,
)


class TestFormatBytes:
    def test_zero(self):
        assert format_bytes(0) == "0 B"






class TestFormatNumber:
    def test_integer(self):
        assert format_number(1000) == "1,000"








class TestFormatDurationMs:
    def test_zero(self):
        assert format_duration_ms(0) == "0ms"






































