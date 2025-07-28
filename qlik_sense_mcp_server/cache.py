"""Система кэширования для MCP сервера."""

import time
import threading
from typing import Any, Dict, Optional, Tuple
import json
import hashlib


class TTLCache:
    """
    Кэш с поддержкой времени жизни (TTL) записей.

    Потокобезопасный кэш для хранения данных с автоматическим истечением срока.
    """

    def __init__(self, default_ttl: int = 3600):
        """
        Инициализация кэша.

        Args:
            default_ttl: Время жизни записей по умолчанию в секундах (1 час)
        """
        self.default_ttl = default_ttl
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.RLock()

    def _generate_key(self, key: str, **kwargs) -> str:
        """Генерирует уникальный ключ на основе параметров."""
        if kwargs:
            # Сортируем kwargs для консистентности
            sorted_kwargs = sorted(kwargs.items())
            key_data = f"{key}:{json.dumps(sorted_kwargs, sort_keys=True)}"
        else:
            key_data = key

        # Используем MD5 для создания короткого ключа
        return hashlib.md5(key_data.encode()).hexdigest()

    def get(self, key: str, **kwargs) -> Optional[Any]:
        """
        Получить значение из кэша.

        Args:
            key: Ключ записи
            **kwargs: Дополнительные параметры для генерации ключа

        Returns:
            Значение или None если запись не найдена/истекла
        """
        cache_key = self._generate_key(key, **kwargs)

        with self._lock:
            if cache_key not in self._cache:
                return None

            value, expiry_time = self._cache[cache_key]

            # Проверяем истечение срока
            if time.time() > expiry_time:
                del self._cache[cache_key]
                return None

            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None, **kwargs) -> None:
        """
        Сохранить значение в кэш.

        Args:
            key: Ключ записи
            value: Значение для сохранения
            ttl: Время жизни в секундах (если None, используется default_ttl)
            **kwargs: Дополнительные параметры для генерации ключа
        """
        if ttl is None:
            ttl = self.default_ttl

        cache_key = self._generate_key(key, **kwargs)
        expiry_time = time.time() + ttl

        with self._lock:
            self._cache[cache_key] = (value, expiry_time)

    def invalidate(self, key: str, **kwargs) -> bool:
        """
        Удалить запись из кэша.

        Args:
            key: Ключ записи
            **kwargs: Дополнительные параметры для генерации ключа

        Returns:
            True если запись была удалена, False если не найдена
        """
        cache_key = self._generate_key(key, **kwargs)

        with self._lock:
            if cache_key in self._cache:
                del self._cache[cache_key]
                return True
            return False

    def clear(self) -> None:
        """Очистить весь кэш."""
        with self._lock:
            self._cache.clear()

    def cleanup_expired(self) -> int:
        """
        Удалить истекшие записи.

        Returns:
            Количество удаленных записей
        """
        current_time = time.time()
        expired_keys = []

        with self._lock:
            for cache_key, (_, expiry_time) in self._cache.items():
                if current_time > expiry_time:
                    expired_keys.append(cache_key)

            for cache_key in expired_keys:
                del self._cache[cache_key]

        return len(expired_keys)

    def size(self) -> int:
        """Получить текущий размер кэша."""
        with self._lock:
            return len(self._cache)

    def stats(self) -> Dict[str, Any]:
        """Получить статистику кэша."""
        current_time = time.time()
        expired_count = 0

        with self._lock:
            total_entries = len(self._cache)

            for _, (_, expiry_time) in self._cache.items():
                if current_time > expiry_time:
                    expired_count += 1

            active_entries = total_entries - expired_count

        return {
            "total_entries": total_entries,
            "active_entries": active_entries,
            "expired_entries": expired_count,
            "default_ttl": self.default_ttl
        }


# Глобальные экземпляры кэша для разных типов данных
app_metadata_cache = TTLCache(default_ttl=3600)  # 1 час для метаданных приложений
field_info_cache = TTLCache(default_ttl=3600)    # 1 час для информации о полях
field_stats_cache = TTLCache(default_ttl=3600)    # 1 час для статистики полей


def cache_key_for_app(app_id: str, operation: str = "metadata") -> str:
    """Генерирует ключ кэша для операций с приложением."""
    return f"app_{operation}_{app_id}"


def cache_key_for_field(app_id: str, field_name: str, operation: str = "info") -> str:
    """Генерирует ключ кэша для операций с полями."""
    return f"field_{operation}_{app_id}_{field_name}"


def get_cached_app_metadata(app_id: str) -> Optional[Dict[str, Any]]:
    """Получить кэшированные метаданные приложения."""
    return app_metadata_cache.get(cache_key_for_app(app_id))


def set_cached_app_metadata(app_id: str, metadata: Dict[str, Any]) -> None:
    """Сохранить метаданные приложения в кэш."""
    app_metadata_cache.set(cache_key_for_app(app_id), metadata)


def get_cached_field_info(app_id: str, field_name: str) -> Optional[Dict[str, Any]]:
    """Получить кэшированную информацию о поле."""
    return field_info_cache.get(cache_key_for_field(app_id, field_name))


def set_cached_field_info(app_id: str, field_name: str, field_info: Dict[str, Any]) -> None:
    """Сохранить информацию о поле в кэш."""
    field_info_cache.set(cache_key_for_field(app_id, field_name), field_info)


def invalidate_app_cache(app_id: str) -> None:
    """Инвалидировать весь кэш для приложения."""
    app_metadata_cache.invalidate(cache_key_for_app(app_id))
    # Можно добавить более сложную логику для удаления всех связанных записей


def get_cache_stats() -> Dict[str, Any]:
    """Получить статистику всех кэшей."""
    return {
        "app_metadata_cache": app_metadata_cache.stats(),
        "field_info_cache": field_info_cache.stats(),
        "field_stats_cache": field_stats_cache.stats()
    }
