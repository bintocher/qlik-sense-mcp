"""Utility functions for the MCP server."""

import re
import json
import random
import string
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timedelta


def format_bytes(size_bytes: int) -> str:
    """Format byte size to a human-readable string."""
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0

    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1

    if i == 0:
        return f"{int(size_bytes)} {size_names[i]}"
    else:
        return f"{size_bytes:.1f} {size_names[i]}"


def format_number(number: Union[int, float], compact: bool = False) -> str:
    """Format numbers into readable string (optionally compact style)."""
    if number is None:
        return "N/A"

    if not compact:
        if isinstance(number, float):
            return f"{number:,.2f}"
        else:
            return f"{number:,}"

    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    elif abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    elif abs(number) >= 1_000:
        return f"{number / 1_000:.1f}K"
    else:
        return str(number)


def format_duration_ms(duration_ms: int) -> str:
    """Format duration in milliseconds to a readable string."""
    if duration_ms == 0:
        return "0ms"

    seconds = duration_ms / 1000

    if seconds < 1:
        return f"{duration_ms}ms"
    elif seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        remaining_seconds = int(seconds % 60)
        return f"{minutes}m {remaining_seconds}s"
    else:
        hours = int(seconds // 3600)
        remaining_minutes = int((seconds % 3600) // 60)
        return f"{hours}h {remaining_minutes}m"


def extract_field_names_from_expression(expression: str) -> List[str]:
    """Extract field names from a Qlik expression."""
    if not expression:
        return []

    bracket_fields = re.findall(r"\[([^\]]+)\]", expression)
    simple_fields = re.findall(r"\b\w+\([^()]*\b(\w+)\b[^()]*\)", expression)

    all_fields = bracket_fields + simple_fields
    return list(set(all_fields))


def clean_field_name(field_name: str) -> str:
    """Clean field name by removing extra characters and brackets."""
    if not field_name:
        return ""

    cleaned = field_name.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]

    return cleaned.strip()


def detect_field_type_from_name(field_name: str) -> str:
    """Heuristically detect field type from its name."""
    field_lower = field_name.lower()

    date_indicators = [
        "date",
        "time",
        "created",
        "modified",
        "year",
        "month",
        "day",
    ]
    if any(indicator in field_lower for indicator in date_indicators):
        return "date"

    key_indicators = ["id", "key", "code", "number"]
    if any(indicator in field_lower for indicator in key_indicators):
        return "key"

    numeric_indicators = ["amount", "sum", "count", "qty", "quantity", "price"]
    if any(indicator in field_lower for indicator in numeric_indicators):
        return "measure"

    return "dimension"


def safe_divide(numerator: Union[int, float], denominator: Union[int, float], default: float = 0.0) -> float:
    """Safe division that handles division by zero returning default value."""
    if denominator == 0:
        return default
    return numerator / denominator


def calculate_percentage(part: Union[int, float], total: Union[int, float], decimal_places: int = 1) -> float:
    """Calculate percentage with rounding and zero-division handling."""
    if total == 0:
        return 0.0
    percentage = (part / total) * 100
    return round(percentage, decimal_places)


def group_objects_by_type(objects: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group Qlik objects by type."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for obj in objects:
        obj_type = obj.get("qInfo", {}).get("qType", "unknown")
        if obj_type not in grouped:
            grouped[obj_type] = []
        grouped[obj_type].append(obj)

    return grouped


def filter_system_fields(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter out system fields."""
    return [field for field in fields if not field.get("is_system", False)]


def filter_system_tables(tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter out system tables."""
    return [table for table in tables if not table.get("is_system", False)]


def summarize_field_types(fields: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count fields by detected type."""
    type_counts: Dict[str, int] = {}
    for field in fields:
        field_type = field.get("data_type", "unknown")
        type_counts[field_type] = type_counts.get(field_type, 0) + 1
    return type_counts


def find_unused_fields(all_fields: List[str], used_fields: List[str]) -> List[str]:
    """Return list of fields that are not used."""
    all_set = set(all_fields)
    used_set = set(used_fields)
    return list(all_set - used_set)


def validate_app_id(app_id: str) -> bool:
    """Validate app ID format (expects GUID-style)."""
    if not app_id:
        return False
    guid_pattern = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    return bool(re.match(guid_pattern, app_id))


def format_qlik_date(qlik_date: Union[str, int, float]) -> str:
    """Format Qlik Sense date (ISO string or timestamp) to readable string."""
    if not qlik_date:
        return "N/A"

    try:
        if isinstance(qlik_date, str):
            if "T" in qlik_date:
                dt = datetime.fromisoformat(qlik_date.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                return qlik_date
        elif isinstance(qlik_date, (int, float)):
            dt = datetime.fromtimestamp(qlik_date)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return str(qlik_date)
    except (ValueError, TypeError):
        return str(qlik_date)


def create_summary_stats(data: List[Union[int, float]]) -> Dict[str, float]:
    """Create summary statistics for numeric data."""
    if not data:
        return {"count": 0, "min": 0, "max": 0, "avg": 0, "sum": 0}

    clean_data = [x for x in data if x is not None and isinstance(x, (int, float))]

    if not clean_data:
        return {"count": 0, "min": 0, "max": 0, "avg": 0, "sum": 0}

    return {
        "count": len(clean_data),
        "min": min(clean_data),
        "max": max(clean_data),
        "avg": sum(clean_data) / len(clean_data),
        "sum": sum(clean_data),
    }


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate text to the specified length with suffix."""
    if not text or len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def escape_qlik_field_name(field_name: str) -> str:
    """Escape field name for Qlik expressions when it contains special characters."""
    if not field_name:
        return ""
    if " " in field_name or any(char in field_name for char in "()[]{}+-*/=<>!@#$%^&"):
        return f"[{field_name}]"
    return field_name


def generate_xrfkey() -> str:
    """Generate a random X-Qlik-Xrfkey with 16 alphanumeric characters."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
