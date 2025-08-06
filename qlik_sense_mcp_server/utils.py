"""Вспомогательные функции для MCP сервера."""

import re
import json
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timedelta


def format_bytes(size_bytes: int) -> str:
    """
    Форматирует размер в байтах в человеко-читаемый формат.

    Args:
        size_bytes: Размер в байтах

    Returns:
        Отформатированная строка (например: "1.5 MB", "245 KB")
    """
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
    """
    Форматирует числа в удобочитаемый формат.

    Args:
        number: Число для форматирования
        compact: Использовать компактный формат (1.2K, 3.4M)

    Returns:
        Отформатированная строка
    """
    if number is None:
        return "N/A"

    if not compact:
        if isinstance(number, float):
            return f"{number:,.2f}"
        else:
            return f"{number:,}"

    # Компактный формат
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    elif abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    elif abs(number) >= 1_000:
        return f"{number / 1_000:.1f}K"
    else:
        return str(number)


def format_duration_ms(duration_ms: int) -> str:
    """
    Форматирует длительность в миллисекундах в читаемый формат.

    Args:
        duration_ms: Длительность в миллисекундах

    Returns:
        Отформатированная строка (например: "2m 30s", "1.5s")
    """
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
    """
    Извлекает имена полей из Qlik-выражения.

    Args:
        expression: Qlik-выражение (например: "Sum([Sales Amount])")

    Returns:
        Список имен полей
    """
    if not expression:
        return []

    # Ищем поля в квадратных скобках [FieldName]
    bracket_fields = re.findall(r'\[([^\]]+)\]', expression)

    # Ищем простые имена полей (без скобок) в функциях
    # Например: Sum(Sales) -> Sales
    simple_fields = re.findall(r'\b\w+\([^()]*\b(\w+)\b[^()]*\)', expression)

    all_fields = bracket_fields + simple_fields
    return list(set(all_fields))  # Убираем дубликаты


def clean_field_name(field_name: str) -> str:
    """
    Очищает имя поля от лишних символов.

    Args:
        field_name: Имя поля

    Returns:
        Очищенное имя поля
    """
    if not field_name:
        return ""

    # Убираем квадратные скобки
    cleaned = field_name.strip()
    if cleaned.startswith('[') and cleaned.endswith(']'):
        cleaned = cleaned[1:-1]

    return cleaned.strip()


def detect_field_type_from_name(field_name: str) -> str:
    """
    Пытается определить тип поля по его имени.

    Args:
        field_name: Имя поля

    Returns:
        Предполагаемый тип поля
    """
    field_lower = field_name.lower()

    # Даты
    date_indicators = ['date', 'время', 'time', 'created', 'modified', 'год', 'year', 'month', 'день', 'day']
    if any(indicator in field_lower for indicator in date_indicators):
        return "date"

    # ID и ключи
    key_indicators = ['id', 'key', 'код', 'code', 'номер', 'number']
    if any(indicator in field_lower for indicator in key_indicators):
        return "key"

    # Количества и суммы
    numeric_indicators = ['amount', 'sum', 'count', 'qty', 'quantity', 'price', 'сумма', 'количество', 'цена']
    if any(indicator in field_lower for indicator in numeric_indicators):
        return "measure"

    # По умолчанию - измерение
    return "dimension"


def safe_divide(numerator: Union[int, float], denominator: Union[int, float], default: float = 0.0) -> float:
    """
    Безопасное деление с обработкой деления на ноль.

    Args:
        numerator: Числитель
        denominator: Знаменатель
        default: Значение по умолчанию при делении на ноль

    Returns:
        Результат деления или значение по умолчанию
    """
    if denominator == 0:
        return default
    return numerator / denominator


def calculate_percentage(part: Union[int, float], total: Union[int, float], decimal_places: int = 1) -> float:
    """
    Вычисляет процент от общего числа.

    Args:
        part: Часть
        total: Целое
        decimal_places: Количество знаков после запятой

    Returns:
        Процент
    """
    if total == 0:
        return 0.0

    percentage = (part / total) * 100
    return round(percentage, decimal_places)


def group_objects_by_type(objects: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Группирует объекты по типу.

    Args:
        objects: Список объектов

    Returns:
        Словарь с объектами, сгруппированными по типу
    """
    grouped = {}

    for obj in objects:
        obj_type = obj.get("qInfo", {}).get("qType", "unknown")

        if obj_type not in grouped:
            grouped[obj_type] = []

        grouped[obj_type].append(obj)

    return grouped


def filter_system_fields(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Фильтрует системные поля.

    Args:
        fields: Список полей

    Returns:
        Список пользовательских полей
    """
    return [field for field in fields if not field.get("is_system", False)]


def filter_system_tables(tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Фильтрует системные таблицы.

    Args:
        tables: Список таблиц

    Returns:
        Список пользовательских таблиц
    """
    return [table for table in tables if not table.get("is_system", False)]


def summarize_field_types(fields: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Подсчитывает количество полей по типам.

    Args:
        fields: Список полей

    Returns:
        Словарь с количеством полей по типам
    """
    type_counts = {}

    for field in fields:
        field_type = field.get("data_type", "unknown")
        type_counts[field_type] = type_counts.get(field_type, 0) + 1

    return type_counts


def find_unused_fields(all_fields: List[str], used_fields: List[str]) -> List[str]:
    """
    Находит неиспользуемые поля.

    Args:
        all_fields: Все доступные поля
        used_fields: Используемые поля

    Returns:
        Список неиспользуемых полей
    """
    all_set = set(all_fields)
    used_set = set(used_fields)
    return list(all_set - used_set)


def validate_app_id(app_id: str) -> bool:
    """
    Проверяет корректность ID приложения.

    Args:
        app_id: ID приложения

    Returns:
        True если ID корректный
    """
    if not app_id:
        return False

    # Qlik Sense app ID обычно GUID формат
    guid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

    return bool(re.match(guid_pattern, app_id))


def format_qlik_date(qlik_date: Union[str, int, float]) -> str:
    """
    Форматирует дату из Qlik Sense в читаемый формат.

    Args:
        qlik_date: Дата в формате Qlik Sense

    Returns:
        Отформатированная дата
    """
    if not qlik_date:
        return "N/A"

    try:
        # Если это строка ISO формата
        if isinstance(qlik_date, str):
            if 'T' in qlik_date:
                dt = datetime.fromisoformat(qlik_date.replace('Z', '+00:00'))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                return qlik_date

        # Если это timestamp
        elif isinstance(qlik_date, (int, float)):
            dt = datetime.fromtimestamp(qlik_date)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        return str(qlik_date)

    except (ValueError, TypeError):
        return str(qlik_date)


def create_summary_stats(data: List[Union[int, float]]) -> Dict[str, float]:
    """
    Создает сводную статистику для числовых данных.

    Args:
        data: Список числовых значений

    Returns:
        Словарь со статистикой
    """
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
        "sum": sum(clean_data)
    }


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """
    Обрезает текст до указанной длины.

    Args:
        text: Исходный текст
        max_length: Максимальная длина
        suffix: Суффикс для обрезанного текста

    Returns:
        Обрезанный текст
    """
    if not text or len(text) <= max_length:
        return text

    return text[:max_length - len(suffix)] + suffix


def escape_qlik_field_name(field_name: str) -> str:
    """
    Экранирует имя поля для использования в Qlik выражениях.

    Args:
        field_name: Имя поля

    Returns:
        Экранированное имя поля
    """
    if not field_name:
        return ""

    # Если содержит пробелы или специальные символы, заключаем в квадратные скобки
    if ' ' in field_name or any(char in field_name for char in '()[]{}+-*/=<>!@#$%^&'):
        return f"[{field_name}]"

    return field_name
