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
    escape_qlik_field_name,
    generate_xrfkey,
)


class TestFormatBytes:
    def test_zero(self):
        assert format_bytes(0) == "0 B"

    def test_bytes(self):
        assert format_bytes(500) == "500 B"

    def test_kilobytes(self):
        result = format_bytes(1024)
        assert "KB" in result

    def test_megabytes(self):
        result = format_bytes(1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = format_bytes(1024 ** 3)
        assert "GB" in result


class TestFormatNumber:
    def test_integer(self):
        assert format_number(1000) == "1,000"

    def test_float(self):
        assert format_number(1234.5) == "1,234.50"

    def test_none(self):
        assert format_number(None) == "N/A"

    def test_compact_thousands(self):
        assert format_number(1500, compact=True) == "1.5K"

    def test_compact_millions(self):
        assert format_number(2_500_000, compact=True) == "2.5M"

    def test_compact_billions(self):
        assert format_number(3_000_000_000, compact=True) == "3.0B"

    def test_compact_small(self):
        assert format_number(42, compact=True) == "42"


class TestFormatDurationMs:
    def test_zero(self):
        assert format_duration_ms(0) == "0ms"

    def test_milliseconds(self):
        assert format_duration_ms(500) == "500ms"

    def test_seconds(self):
        assert format_duration_ms(2500) == "2.5s"

    def test_minutes(self):
        result = format_duration_ms(90000)
        assert "1m" in result

    def test_hours(self):
        result = format_duration_ms(7200000)
        assert "2h" in result


class TestExtractFieldNames:
    def test_bracket_fields(self):
        result = extract_field_names_from_expression("Sum([Sales Amount])")
        assert "Sales Amount" in result

    def test_empty(self):
        assert extract_field_names_from_expression("") == []

    def test_none(self):
        assert extract_field_names_from_expression(None) == []


class TestCleanFieldName:
    def test_brackets(self):
        assert clean_field_name("[Field Name]") == "Field Name"

    def test_spaces(self):
        assert clean_field_name("  Field  ") == "Field"

    def test_empty(self):
        assert clean_field_name("") == ""

    def test_none_like(self):
        assert clean_field_name("") == ""


class TestDetectFieldType:
    def test_date(self):
        assert detect_field_type_from_name("OrderDate") == "date"

    def test_key(self):
        assert detect_field_type_from_name("CustomerID") == "key"

    def test_measure(self):
        assert detect_field_type_from_name("SalesAmount") == "measure"

    def test_dimension(self):
        assert detect_field_type_from_name("Region") == "dimension"


class TestSafeDivide:
    def test_normal(self):
        assert safe_divide(10, 2) == 5.0

    def test_zero_denominator(self):
        assert safe_divide(10, 0) == 0.0

    def test_custom_default(self):
        assert safe_divide(10, 0, default=-1.0) == -1.0


class TestCalculatePercentage:
    def test_normal(self):
        assert calculate_percentage(25, 100) == 25.0

    def test_zero_total(self):
        assert calculate_percentage(25, 0) == 0.0

    def test_decimal_places(self):
        result = calculate_percentage(1, 3, decimal_places=2)
        assert result == 33.33


class TestGroupObjectsByType:
    def test_grouping(self):
        objects = [
            {"qInfo": {"qType": "chart"}},
            {"qInfo": {"qType": "table"}},
            {"qInfo": {"qType": "chart"}},
        ]
        result = group_objects_by_type(objects)
        assert len(result["chart"]) == 2
        assert len(result["table"]) == 1

    def test_empty(self):
        assert group_objects_by_type([]) == {}


class TestFilterSystemFields:
    def test_filters_system(self):
        fields = [
            {"name": "Normal", "is_system": False},
            {"name": "System", "is_system": True},
        ]
        result = filter_system_fields(fields)
        assert len(result) == 1
        assert result[0]["name"] == "Normal"


class TestFilterSystemTables:
    def test_filters_system(self):
        tables = [
            {"name": "Data", "is_system": False},
            {"name": "SystemTable", "is_system": True},
        ]
        result = filter_system_tables(tables)
        assert len(result) == 1
        assert result[0]["name"] == "Data"


class TestSummarizeFieldTypes:
    def test_counts(self):
        fields = [
            {"data_type": "text"},
            {"data_type": "numeric"},
            {"data_type": "text"},
        ]
        result = summarize_field_types(fields)
        assert result["text"] == 2
        assert result["numeric"] == 1


class TestFindUnusedFields:
    def test_finds_unused(self):
        result = find_unused_fields(["A", "B", "C"], ["A", "C"])
        assert result == ["B"]

    def test_all_used(self):
        result = find_unused_fields(["A", "B"], ["A", "B"])
        assert result == []


class TestValidateAppId:
    def test_valid_guid(self):
        assert validate_app_id("e2958865-2aed-4f8a-b3c7-20e6f21d275c") is True

    def test_invalid(self):
        assert validate_app_id("not-a-guid") is False

    def test_empty(self):
        assert validate_app_id("") is False


class TestFormatQlikDate:
    def test_iso_date(self):
        result = format_qlik_date("2024-01-15T10:30:00Z")
        assert "2024-01-15" in result

    def test_empty(self):
        assert format_qlik_date("") == "N/A"

    def test_none(self):
        assert format_qlik_date(None) == "N/A"

    def test_plain_string(self):
        assert format_qlik_date("2024-01-15") == "2024-01-15"


class TestCreateSummaryStats:
    def test_normal(self):
        result = create_summary_stats([1, 2, 3, 4, 5])
        assert result["count"] == 5
        assert result["min"] == 1
        assert result["max"] == 5
        assert result["avg"] == 3.0
        assert result["sum"] == 15

    def test_empty(self):
        result = create_summary_stats([])
        assert result["count"] == 0

    def test_with_none(self):
        result = create_summary_stats([1, None, 3])
        assert result["count"] == 2


class TestTruncateText:
    def test_short_text(self):
        assert truncate_text("Hello", max_length=10) == "Hello"

    def test_long_text(self):
        result = truncate_text("Hello World!", max_length=8)
        assert result.endswith("...")
        assert len(result) == 8

    def test_none(self):
        assert truncate_text(None) is None


class TestEscapeQlikFieldName:
    def test_simple(self):
        assert escape_qlik_field_name("Region") == "Region"

    def test_with_space(self):
        assert escape_qlik_field_name("Sales Amount") == "[Sales Amount]"

    def test_with_special_chars(self):
        assert escape_qlik_field_name("Price+Tax") == "[Price+Tax]"

    def test_empty(self):
        assert escape_qlik_field_name("") == ""


class TestGenerateXrfkey:
    def test_length(self):
        key = generate_xrfkey()
        assert len(key) == 16

    def test_alphanumeric(self):
        key = generate_xrfkey()
        assert key.isalnum()

    def test_unique(self):
        keys = {generate_xrfkey() for _ in range(100)}
        assert len(keys) > 90  # Very unlikely to have collisions
